import os
import re
import sqlite3
import random
import requests
import logging
import sys
import asyncio
import time
import html
import unicodedata
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import RPCError, FloodWait, ChannelInvalid, ChannelPrivate
from telegram import Bot
from telegram.error import TelegramError
from bs4 import BeautifulSoup
import urllib.parse
import json
import shutil
from glob import glob

# ======== System Configuration ==========
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ======== Load Configuration ==========
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
GP_API_KEY = os.getenv("GP_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
SOURCE_CHANNELS = [c.strip() for c in os.getenv("SOURCE_CHANNELS", "").split(",") if c.strip()]
AMZN_AFFILIATE_ID = os.getenv("AMZN_AFFILIATE_ID", "")

# ======== USER-CONFIGURABLE VARIABLES (Edit these as needed) ==========
# Minimum discount percent for a deal to be posted
MIN_DISCOUNT_PERCENT = float(os.getenv("MIN_DISCOUNT_PERCENT", "60"))  # e.g. 60% off
# Maximum number of deals to scrape per run (per site)
MAX_DEALS_PER_RUN = int(os.getenv("MAX_DEALS_PER_RUN", "5"))
# Number of top deals (by popularity/discount) to send per batch
TOP_DEALS_COUNT = int(os.getenv("TOP_DEALS_COUNT", "3"))
# Seconds to wait between sending deals to Telegram (rate limit)
SEND_INTERVAL = int(os.getenv("SEND_INTERVAL", "10"))
# Seconds to wait between website scraping runs
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "1800"))  # 30 min
# Price change threshold for re-posting (fraction, e.g. 0.3 = 30%)
PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.3"))
# Telegram FloodWait fallback (seconds)
TELEGRAM_FLOODWAIT_FALLBACK = int(os.getenv("TELEGRAM_FLOODWAIT_FALLBACK", "60"))
# GPLinks max retries
GPLINKS_MAX_RETRIES = int(os.getenv("GPLINKS_MAX_RETRIES", "4"))
# Network timeout (seconds)
NETWORK_TIMEOUT = int(os.getenv("NETWORK_TIMEOUT", "20"))
# DRY_RUN mode (True = don't send to Telegram)
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
# ======== END USER-CONFIGURABLE VARIABLES ==========

# ======== Paths & Setup ==========
DB_PATH = "deals.db"
MEDIA_FOLDER = "media"
LOG_FILE = "deal_poster_debug.log"
os.makedirs(MEDIA_FOLDER, exist_ok=True)

# ======== Advanced Logging ==========
class UnicodeSafeFormatter(logging.Formatter):
    def format(self, record):
        message = super().format(record)
        if sys.platform == "win32":
            return message.encode('utf-8', 'replace').decode('utf-8')
        return message

# ======== Log File Rotation & Cleanup ==========
def cleanup_old_logs_and_media(log_file=LOG_FILE, media_folder=MEDIA_FOLDER, max_log_size_mb=5, max_media_files=200):
    # Truncate log file if too large
    if os.path.exists(log_file):
        if os.path.getsize(log_file) > max_log_size_mb * 1024 * 1024:
            with open(log_file, 'r+', encoding='utf-8') as f:
                f.seek(-max_log_size_mb * 512 * 1024, os.SEEK_END)
                data = f.read()
                f.seek(0)
                f.write(data)
                f.truncate()
    # Delete old media files if too many
    media_files = sorted(glob(os.path.join(media_folder, '*')), key=os.path.getmtime)
    if len(media_files) > max_media_files:
        for f in media_files[:-max_media_files]:
            try:
                os.remove(f)
            except Exception:
                pass
    # Delete unwanted files
    for unwanted in glob('test_*.py'):
        try:
            os.remove(unwanted)
        except Exception:
            pass

cleanup_old_logs_and_media()

logger = logging.getLogger("DealAutoPoster")
logger.setLevel(logging.INFO)

# Console handler with Unicode safety
console_handler = logging.StreamHandler()
console_handler.setFormatter(UnicodeSafeFormatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(console_handler)

# File handler with UTF-8 encoding
file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(file_handler)

# ======== Global Error Handler ==========
def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_exception

# ======== User-Agent Pool ==========
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Mobile Safari/537.36',
]

# ======== Advanced Database Manager ==========
class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()
        self._create_state_table()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            # Create deals table with advanced features
            c.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_message TEXT,
                edited_message TEXT,
                product_url TEXT,
                gplink TEXT,
                channel_id TEXT,
                message_id TEXT,
                source_type TEXT DEFAULT 'telegram',
                source_name TEXT,
                sent INTEGER DEFAULT 0,
                price TEXT,
                last_sent_price REAL,
                last_sent_time DATETIME,
                last_sent_source TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel_id, message_id)
            )
            """)
            # Migration for old DBs
            try:
                c.execute("ALTER TABLE deals ADD COLUMN price TEXT")
            except Exception: pass
            try:
                c.execute("ALTER TABLE deals ADD COLUMN last_sent_price REAL")
            except Exception: pass
            try:
                c.execute("ALTER TABLE deals ADD COLUMN last_sent_time DATETIME")
            except Exception: pass
            try:
                c.execute("ALTER TABLE deals ADD COLUMN last_sent_source TEXT")
            except Exception: pass
            
            # Create message tracking table for better sync
            c.execute("""
            CREATE TABLE IF NOT EXISTS message_tracking (
                channel_id TEXT,
                last_message_id INTEGER DEFAULT 0,
                last_sync_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (channel_id)
            )
            """)
            
            # Create indexes for performance
            c.execute("CREATE INDEX IF NOT EXISTS idx_url_timestamp ON deals (product_url, timestamp)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_channel_message ON deals (channel_id, message_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_source_type ON deals (source_type, source_name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tracking ON message_tracking (channel_id)")
            
            conn.commit()
    
    def _create_state_table(self):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """)
            conn.commit()
    
    def save_state(self, key, value):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
            INSERT OR REPLACE INTO app_state (key, value)
            VALUES (?, ?)
            """, (key, value))
            conn.commit()
    
    def load_state(self, key, default=None):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT value FROM app_state WHERE key = ?
            """, (key,))
            row = c.fetchone()
            return row[0] if row else default
    
    def get_last_message_id(self, channel_id):
        """Get the last processed message ID for a channel"""
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT last_message_id FROM message_tracking 
            WHERE channel_id = ?
            """, (channel_id,))
            row = c.fetchone()
            return row[0] if row else 0
    
    def update_last_message_id(self, channel_id, message_id):
        """Update the last processed message ID for a channel"""
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
            INSERT OR REPLACE INTO message_tracking (channel_id, last_message_id, last_sync_time)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """, (channel_id, message_id))
            conn.commit()
    
    def is_duplicate(self, url):
        """Check for duplicate URLs with advanced logic"""
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            # Check exact URL duplicates in last 24 hours
            c.execute("""
            SELECT COUNT(*) FROM deals
            WHERE product_url = ? AND timestamp >= datetime('now', '-24 hours')
            """, (url,))
            return c.fetchone()[0] > 0
    
    def message_exists(self, channel_id, message_id):
        """Check if a specific message has been processed"""
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
            SELECT COUNT(*) FROM deals
            WHERE channel_id = ? AND message_id = ?
            """, (channel_id, message_id))
            return c.fetchone()[0] > 0

    def save_deal(self, original, edited, url, gplink, channel_id=None, message_id=None, source_type='telegram', source_name=None, price=None):
        """Save deal with conflict resolution"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                c = conn.cursor()
                c.execute("""
                INSERT OR IGNORE INTO deals (
                original_message, edited_message, product_url, 
                    gplink, channel_id, message_id, source_type, source_name, sent, price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """, (original, edited, url, gplink, channel_id, message_id, source_type, source_name, price))
                conn.commit()
                return c.rowcount > 0
        except Exception as e:
            logger.error(f"❌ Database save error: {e}")
            return False
    
    def get_last_sent_info(self, url, source):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT last_sent_price, last_sent_time FROM deals
                WHERE product_url = ? AND last_sent_source = ?
                ORDER BY last_sent_time DESC LIMIT 1
            """, (url, source))
            return c.fetchone()

    def update_last_sent_info(self, url, source, price, sent_time):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE deals SET last_sent_price = ?, last_sent_time = ?, last_sent_source = ?
                WHERE product_url = ? AND last_sent_source = ?
            """, (price, sent_time, source, url, source))
            conn.commit()

    def should_send_deal(self, url, source, price, now, price_change_threshold=PRICE_CHANGE_THRESHOLD, window_hours=6):
        info = self.get_last_sent_info(url, source)
        if not info:
            return True  # Never sent before
        last_price, last_time = info
        if not last_time:
            return True
        try:
            last_time_dt = datetime.fromisoformat(last_time)
        except Exception:
            return True
        if (now - last_time_dt).total_seconds() > window_hours * 3600:
            return True  # Last sent more than window ago
        try:
            last_price = float(last_price)
            price = float(price)
        except Exception:
            return True  # If price can't be parsed, allow
        if abs(price - last_price) / last_price >= price_change_threshold:
            return True  # Price changed enough
        return False

# ======== Advanced Link Shortener ==========
class LinkShortener:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://gplinks.in/api"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })
    
    async def shorten(self, url):
        if not self.api_key:
            logger.warning("⚠️ No GPLinks API key provided, using original URL")
            return url
        
        # Try multiple API endpoints for better compatibility
        api_endpoints = [
            f"{self.base_url}?api={self.api_key}&url={urllib.parse.quote(url)}",
            f"{self.base_url}",
            "https://gplinks.in/api",
            "https://gplinks.co/api"
        ]
        
        for endpoint in api_endpoints:
            for attempt in range(GPLINKS_MAX_RETRIES):
                try:
                    if endpoint == f"{self.base_url}" or "gplinks.in" in endpoint or "gplinks.co" in endpoint:
                        # POST method
                        payload = {
                            "api": self.api_key,
                            "url": url,
                            "format": "text"
                        }
                        response = self.session.post(
                            endpoint, 
                            data=json.dumps(payload),
                            timeout=NETWORK_TIMEOUT
                        )
                    else:
                        # GET method
                        response = self.session.get(endpoint, timeout=NETWORK_TIMEOUT)
                    
                    response.raise_for_status()
                    
                    # Try to parse as JSON first
                    try:
                        data = response.json()
                        if data.get("status") == "success":
                            shortened_url = data.get("shortenedUrl", "")
                            if shortened_url:
                                logger.info(f"✅ URL shortened successfully: {shortened_url[:50]}...")
                                return shortened_url
                        else:
                            logger.error(f"❌ GPLinks returned error: {data.get('message', 'Unknown error')}")
                    except json.JSONDecodeError:
                        # If not JSON, treat as text
                        shortened_url = response.text.strip()
                        if shortened_url.startswith('http'):
                            logger.info(f"✅ URL shortened successfully: {shortened_url[:50]}...")
                            return shortened_url
                        else:
                            logger.error(f"❌ GPLinks returned invalid response: {shortened_url[:100]}")
                    
                    # If we get here, try next endpoint
                    break
                    
                except (requests.exceptions.RequestException, requests.exceptions.Timeout) as e:
                    logger.error(f"❌ GPLinks API error (attempt {attempt+1}/{GPLINKS_MAX_RETRIES}): {e}")
                    if attempt < GPLINKS_MAX_RETRIES - 1:
                        sleep_time = min(2 ** attempt, 30)  # Cap sleep at 30 seconds
                        logger.info(f"🔁 Retrying GPLinks in {sleep_time}s...")
                        await asyncio.sleep(sleep_time)
        
        logger.error(f"❌ GPLinks failed after trying all endpoints. Using original URL.")
        
        # Try to create a simple short URL as fallback
        try:
            # Use a simple URL shortener as backup
            backup_url = f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(url)}"
            response = self.session.get(backup_url, timeout=10)
            if response.status_code == 200 and response.text.startswith('http'):
                logger.info(f"✅ Used TinyURL fallback: {response.text[:50]}...")
                return response.text
        except Exception as e:
            logger.error(f"❌ TinyURL fallback also failed: {e}")
        
        return url

# ======== Advanced Website Deal Scraper ==========
class WebsiteDealScraper:
    def __init__(self, db_manager, shortener, beautifier, bot, target_channel, amzn_affiliate_id):
        self.db = db_manager
        self.shortener = shortener
        self.beautifier = beautifier
        self.bot = bot
        self.target_channel = target_channel
        self.amzn_affiliate_id = amzn_affiliate_id
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': random.choice(USER_AGENTS)})
        
        # Enhanced website sources
        self.website_sources = {
            'amazon': {
                'url': 'https://www.amazon.in/gp/goldbox',
                'name': 'Amazon',
                'enabled': True
            },
            'flipkart': {
                'url': 'https://www.flipkart.com/offers-store',
                'name': 'Flipkart',
                'enabled': True
            }
        }
    
    def _random_headers(self):
        return {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Referer': 'https://www.google.com/',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'DNT': '1',
            'Sec-GPC': '1'
        }
    
    def _get_session_with_retry(self):
        """Create session with retry and anti-bot protection"""
        session = requests.Session()
        session.headers.update(self._random_headers())
        
        # Add more realistic browser behavior
        session.headers.update({
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'sec-ch-ua': '"Not.A/Brand";v="8", "Chromium";v="114", "Google Chrome";v="114"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"'
        })
        
        return session
    
    def add_amazon_affiliate(self, url):
        if not self.amzn_affiliate_id or 'amazon' not in url.lower():
            return url
        try:
            parsed = urllib.parse.urlparse(url)
            query_params = urllib.parse.parse_qs(parsed.query)
            query_params['tag'] = [self.amzn_affiliate_id]
            new_query = urllib.parse.urlencode(query_params, doseq=True)
            new_url = urllib.parse.urlunparse((
                parsed.scheme, parsed.netloc, parsed.path,
                parsed.params, new_query, parsed.fragment
            ))
            logger.info(f"🔗 Added Amazon affiliate ID to URL")
            return new_url
        except Exception as e:
            logger.error(f"❌ Error adding affiliate ID: {e}")
            return url

    def _extract_price(self, price_text):
        """Extract numeric price from text"""
        if not price_text:
            return 0.0
        try:
            # Remove currency symbols and commas
            clean_text = re.sub(r'[^\d.]', '', price_text)
            return float(clean_text)
        except:
            return 0.0

    def _calculate_discount(self, original_price, current_price):
        """Calculate discount percentage"""
        if original_price <= 0 or current_price <= 0:
            return 0
        return ((original_price - current_price) / original_price) * 100

    async def scrape_amazon_deals(self):
        """Enhanced Amazon scraping with discount calculation"""
        try:
            logger.info("🛒 Scraping Amazon Today's Deals...")
            
            # Try multiple Amazon URLs
            amazon_urls = [
                "https://www.amazon.in/gp/goldbox",
                "https://www.amazon.in/deals",
                "https://www.amazon.in/s?i=specialty-aps&bbn=1389401031&rh=n%3A1389401031%2Cn%3A1389402031",
                "https://www.amazon.in/s?i=specialty-aps&bbn=1389401031&rh=n%3A1389401031%2Cn%3A1389402031%2Cn%3A1389403031"
            ]
            
            all_deals = []
            for url in amazon_urls:
                try:
                    # Use enhanced session
                    session = self._get_session_with_retry()
                    
                    # Add longer delay to avoid rate limiting (429 errors)
                    await asyncio.sleep(random.uniform(2, 5))
                    
                    try:
                        response = session.get(url, timeout=NETWORK_TIMEOUT)
                        response.raise_for_status()
                    except requests.exceptions.HTTPError as e:
                        if e.response.status_code == 429:
                            logger.warning(f"⚠️ Rate limited (429) for {url}, skipping...")
                            continue
                        else:
                            raise e
                    
                    soup = BeautifulSoup(response.content, 'html5lib')
                    deals = []
                    
                    # Multiple selectors for different page layouts
                    selectors = [
                        '.DealContent', '.a-section.a-spacing-none.aok-relative', 
                        '[data-testid="deal-card"]', '.a-section.a-spacing-none', 
                        '.a-section.a-spacing-base', '.a-section.a-spacing-mini',
                        '.a-section.a-spacing-none.aok-relative', '.a-section.a-spacing-base.aok-relative',
                        '.a-section.a-spacing-none.aok-relative.s-result-item', '.a-section.a-spacing-base.s-result-item',
                        '.s-result-item', '.a-section', '.a-spacing-base', '.a-spacing-mini',
                        '[data-component-type="s-search-result"]', '.sg-col-inner',
                        '.a-section.a-spacing-none.s-result-item', '.a-section.a-spacing-base.s-result-item'
                    ]
                    
                    deal_elements = []
                    for selector in selectors:
                        elements = soup.select(selector)
                        if elements and len(elements) > 2:  # Need at least 3 elements to be valid
                            deal_elements = elements
                            logger.info(f"✅ Found {len(elements)} Amazon deals with selector: {selector}")
                            break
                    
                    # If no deals found with selectors, try alternative approach
                    if not deal_elements:
                        # Look for any product links
                        product_links = soup.select('a[href*="/dp/"]')
                        if product_links:
                            logger.info(f"✅ Found {len(product_links)} Amazon product links")
                            # Create simple deals from links
                            for link in product_links[:MAX_DEALS_PER_RUN]:
                                try:
                                    title = link.get_text(strip=True)
                                    href = link.get('href', '')
                                    if title and href and len(title) > 5:
                                        if href.startswith('/'):
                                            href = f"https://www.amazon.in{href}"
                                        deals.append({
                                            'title': title,
                                            'url': href,
                                            'image_url': '',
                                            'price': '',
                                            'original_price': '',
                                            'discount': '',
                                            'source': 'Amazon',
                                            'popularity': random.randint(1000, 10000)
                                        })
                                except Exception as e:
                                    continue
                    
                    for element in deal_elements[:MAX_DEALS_PER_RUN]:  # Limit per URL
                        try:
                            title = link = price = image_url = original_price = discount_percent = ""
                            
                            # Title selectors
                            for sel in ['h2', '.a-text-bold', '.a-size-base-plus', '.a-size-medium', '.a-link-normal', '.a-size-mini', '.a-size-small']:
                                t = element.select_one(sel)
                                if t:
                                    title = t.get_text(strip=True)
                                    if title and len(title) > 10: break
                            
                            # Link selectors
                            for sel in ['a[href*="/dp/"]', 'a[href*="/gp/product/"]', 'a[href*="/d/"]', 'a[href*="/s?"]']:
                                l = element.select_one(sel)
                                if l:
                                    link = l.get('href', '')
                                    if link and ('/dp/' in link or '/gp/product/' in link): break
                            
                            # Enhanced price selectors for Amazon
                            for sel in ['.a-price-whole', '.a-price .a-offscreen', '.a-price-current .a-offscreen', '.a-price', '.a-price-range', '.a-price-current', '.a-price-symbol', '.a-price-fraction']:
                                p = element.select_one(sel)
                                if p:
                                    price_text = p.get_text(strip=True)
                                    if price_text and any(char.isdigit() for char in price_text):
                                        price = price_text
                                        break
                            
                            # Try to get full price if we only got part
                            if price and not any(char in price for char in ['₹', 'Rs', '$', '€', '£']):
                                # Look for currency symbol
                                for sel in ['.a-price-symbol', '.a-price-current .a-offscreen']:
                                    symbol_elem = element.select_one(sel)
                                    if symbol_elem:
                                        symbol = symbol_elem.get_text(strip=True)
                                        if symbol and symbol in ['₹', 'Rs', '$', '€', '£']:
                                            price = f"{symbol}{price}"
                                            break
                            
                            # Get original price for discount calculation
                            for sel in ['.a-text-price', '.a-price.a-text-price']:
                                op = element.select_one(sel)
                                if op:
                                    original_price = op.get_text(strip=True)
                            
                            # Calculate discount
                            current_price_val = self._extract_price(price)
                            original_price_val = self._extract_price(original_price)
                            discount_percent = self._calculate_discount(original_price_val, current_price_val)
                            
                            # Skip if discount is below threshold
                            if discount_percent < MIN_DISCOUNT_PERCENT:
                                continue
                            
                            # Enhanced image selectors for Amazon
                            for sel in ['img', '.a-image-container img', '.a-image img', 'img[src*="amazon"]', 'img[data-src*="amazon"]', 'img[src*="images"]', 'img[data-src*="images"]']:
                                img = element.select_one(sel)
                                if img:
                                    image_url = img.get('src', '') or img.get('data-src', '')
                                    if image_url and image_url.startswith('http'): break
                            
                            if title and link:
                                if link.startswith('/'):
                                    link = f"https://www.amazon.in{link}"
                                link = self.add_amazon_affiliate(link)
                                deals.append({
                                    'title': title, 
                                    'url': link, 
                                    'image_url': image_url, 
                                    'price': price, 
                                    'original_price': original_price,
                                    'discount': f"{discount_percent:.0f}%",
                                    'source': 'Amazon',
                                    'popularity': random.randint(1000, 10000)  # Simulated popularity
                                })
                        except Exception as e:
                            logger.error(f"❌ Error parsing Amazon deal element: {e}")
                            continue
                    
                    all_deals.extend(deals)
                    logger.info(f"✅ Scraped {len(deals)} deals from {url}")
                    
                except Exception as e:
                    logger.error(f"❌ Error scraping Amazon URL {url}: {e}")
                    continue
            
            # Remove duplicates based on URL
            unique_deals = []
            seen_urls = set()
            for deal in all_deals:
                if deal['url'] not in seen_urls:
                    unique_deals.append(deal)
                    seen_urls.add(deal['url'])
            
            logger.info(f"✅ Total unique Amazon deals: {len(unique_deals)}")
            return unique_deals
            
        except Exception as e:
            logger.error(f"❌ Error scraping Amazon deals: {e}")
            return []
    
    async def scrape_flipkart_deals(self):
        """Enhanced Flipkart scraping with discount calculation"""
        try:
            logger.info("🛒 Scraping Flipkart deals...")
            
            # Try multiple Flipkart URLs with better structure
            flipkart_urls = [
                "https://www.flipkart.com/offers-store",
                "https://www.flipkart.com/offers-store?otracker=nmenu_offer-zone",
                "https://www.flipkart.com/offers-store?otracker=nmenu_offer-zone&fm=neo%2Fmerchandising&iid=M_",
                "https://www.flipkart.com/offers-store?otracker=nmenu_offer-zone&fm=neo%2Fmerchandising&iid=M_&otracker=hp_rich_navigation_1_1.navigationCard.RICH_NAVIGATION_Electronics~All-Electronics~Deals-of-the-Day_HP_NAVIGATION_MENU"
            ]
            
            all_deals = []
            for url in flipkart_urls:
                try:
                    # Use enhanced session
                    session = self._get_session_with_retry()
                    
                    # Add longer delay to avoid rate limiting (429 errors)
                    await asyncio.sleep(random.uniform(3, 6))
                    
                    try:
                        response = session.get(url, timeout=NETWORK_TIMEOUT)
                        response.raise_for_status()
                    except requests.exceptions.HTTPError as e:
                        if e.response.status_code == 429:
                            logger.warning(f"⚠️ Rate limited (429) for {url}, skipping...")
                            continue
                        else:
                            raise e
                    
                    soup = BeautifulSoup(response.content, 'html5lib')
                    deals = []
                    
                    # Updated selectors for current Flipkart layout
                    card_selectors = [
                        '._1xHGtK', '._2KpZ6l', '[data-testid="product-card"]',
                        '._4rR01T', '._2WkVRV', '.sLDaPM', '._1AtVbE',
                        '.sLDaPM', '._2UzuFa', '._1x-ss4', '._1AtVbE',
                        '._1AtVbE', '._2UzuFa', '._1x-ss4', '._1AtVbE',
                        '.sLDaPM', '._2UzuFa', '._1x-ss4', '._1AtVbE',
                        '._1AtVbE', '._2UzuFa', '._1x-ss4', '._1AtVbE'
                    ]
                    
                    product_cards = []
                    for selector in card_selectors:
                        cards = soup.select(selector)
                        if cards:
                            product_cards = cards
                            logger.info(f"✅ Found {len(cards)} Flipkart deals with selector: {selector}")
                            break
                    
                    # Also try alternative approach - look for any product links
                    if not product_cards:
                        product_links = soup.select('a[href*="/p/"]')
                        if product_links:
                            logger.info(f"✅ Found {len(product_links)} Flipkart product links")
                            # Create simple cards from links
                            for link in product_links[:MAX_DEALS_PER_RUN]:
                                try:
                                    title = link.get_text(strip=True)
                                    href = link.get('href', '')
                                    if title and href and len(title) > 5:
                                        if href.startswith('/'):
                                            href = f"https://www.flipkart.com{href}"
                                        deals.append({
                                            'title': title,
                                            'url': href,
                                            'image_url': '',
                                            'price': '',
                                            'original_price': '',
                                            'discount': '',
                                            'source': 'Flipkart',
                                            'popularity': random.randint(1000, 10000)  # Simulated popularity
                                        })
                                except Exception as e:
                                    continue
                    
                    for card in product_cards[:MAX_DEALS_PER_RUN]:  # Limit per URL
                        try:
                            title = link = price = image_url = original_price = discount_percent = ""
                            
                            # Title selectors
                            for sel in ['._4rR01T', '._2WkVRV', '.sLDaPM', 'h3', '.a-size-base-plus', '._1AtVbE', '._2UzuFa']:
                                t = card.select_one(sel)
                                if t:
                                    title = t.get_text(strip=True)
                                    if title and len(title) > 5: break
                            
                            # Link selectors
                            for sel in ['a[href*="/p/"]', 'a[href*="/product/"]', 'a[href*="/item/"]', 'a']:
                                l = card.select_one(sel)
                                if l:
                                    link = l.get('href', '')
                                    if link and ('/p/' in link or '/product/' in link): break
                            
                            # Enhanced price selectors for Flipkart
                            for sel in ['._30jeq3', '._1_WHN1', '.a-price-whole', '._2I9KP_', '._3I9_wc', '._1_WHN1', '._3I9_wc', '._2I9KP_', '.a-price-current .a-offscreen']:
                                p = card.select_one(sel)
                                if p:
                                    price_text = p.get_text(strip=True)
                                    if price_text and any(char.isdigit() for char in price_text):
                                        price = price_text
                                        break
                            
                            # Try to get full price if we only got part
                            if price and not any(char in price for char in ['₹', 'Rs', '$', '€', '£']):
                                # Look for currency symbol
                                for sel in ['._3I9_wc', '._2I9KP_', '.a-price-symbol']:
                                    symbol_elem = card.select_one(sel)
                                    if symbol_elem:
                                        symbol = symbol_elem.get_text(strip=True)
                                        if symbol and symbol in ['₹', 'Rs', '$', '€', '£']:
                                            price = f"{symbol}{price}"
                                            break
                            
                            # Get original price for discount calculation
                            for sel in ['._3I9_wc', '._2I9KP_']:
                                op = card.select_one(sel)
                                if op:
                                    original_price = op.get_text(strip=True)
                            
                            # Get discount percentage if available
                            for sel in ['._3Ay6Sb']:
                                dp = card.select_one(sel)
                                if dp:
                                    discount_percent = dp.get_text(strip=True)
                            
                            # Calculate discount if not found
                            if not discount_percent:
                                current_price_val = self._extract_price(price)
                                original_price_val = self._extract_price(original_price)
                                discount = self._calculate_discount(original_price_val, current_price_val)
                                discount_percent = f"{discount:.0f}%"
                            
                            # Skip if discount is below threshold
                            discount_val = float(re.search(r'(\d+)', discount_percent).group(1) if discount_percent else 0)
                            if discount_val < MIN_DISCOUNT_PERCENT:
                                continue
                            
                            # Enhanced image selectors for Flipkart
                            for sel in ['img', '.a-image-container img', '._2r_T1I', 'img[src*="flipkart"]', 'img[data-src*="flipkart"]', 'img[src*="images"]', 'img[data-src*="images"]']:
                                img = card.select_one(sel)
                                if img:
                                    image_url = img.get('src', '') or img.get('data-src', '')
                                    if image_url and image_url.startswith('http'): break
                            
                            if title and link:
                                if link.startswith('/'):
                                    link = f"https://www.flipkart.com{link}"
                                deals.append({
                                    'title': title, 
                                    'url': link, 
                                    'image_url': image_url, 
                                    'price': price, 
                                    'original_price': original_price,
                                    'discount': discount_percent,
                                    'source': 'Flipkart',
                                    'popularity': random.randint(1000, 10000)  # Simulated popularity
                                })
                        except Exception as e:
                            logger.error(f"❌ Error parsing Flipkart deal element: {e}")
                            continue
                    
                    all_deals.extend(deals)
                    logger.info(f"✅ Scraped {len(deals)} deals from {url}")
                    
                except Exception as e:
                    logger.error(f"❌ Error scraping Flipkart URL {url}: {e}")
                    continue
            
            # Remove duplicates
            unique_deals = []
            seen_urls = set()
            for deal in all_deals:
                if deal['url'] not in seen_urls:
                    unique_deals.append(deal)
                    seen_urls.add(deal['url'])
            
            logger.info(f"✅ Total unique Flipkart deals: {len(unique_deals)}")
            return unique_deals
            
        except Exception as e:
            logger.error(f"❌ Error scraping Flipkart deals: {e}")
            return []
    
    async def _download_image(self, image_url, product_url=None):
        """Download product image with advanced handling and fallbacks"""
        if not image_url:
            return None
            
        # Try main image first
        try:
            session = requests.Session()
            session.headers.update({
                'Referer': 'https://www.google.com/',
                'User-Agent': random.choice(USER_AGENTS),
                'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache'
            })
            response = session.get(image_url, timeout=10)  # Shorter timeout for images
            response.raise_for_status()
            
            # Check if it's actually an image
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('image/'):
                logger.warning(f"⚠️ URL doesn't return image: {content_type}")
                raise Exception("Not an image")
            
            # Determine file extension
            if 'jpeg' in content_type or 'jpg' in content_type:
                ext = '.jpg'
            elif 'png' in content_type:
                ext = '.png'
            elif 'webp' in content_type:
                ext = '.webp'
            elif 'gif' in content_type:
                ext = '.gif'
            else:
                ext = '.jpg'
            
            # Save to file
            timestamp = int(time.time())
            image_path = os.path.join(MEDIA_FOLDER, f"deal_{timestamp}_{random.randint(1000, 9999)}{ext}")
            with open(image_path, 'wb') as f:
                f.write(response.content)
            
            # Verify image
            file_size = os.path.getsize(image_path)
            if file_size < 200:
                logger.warning(f"⚠️ Image file too small ({file_size} bytes), trying favicon...")
                os.remove(image_path)
                raise Exception("Image too small")
                
            logger.info(f"✅ Image downloaded: {file_size} bytes")
            return image_path
            
        except Exception as e:
            logger.error(f"❌ Error downloading main image: {e}")
            
        # Try favicon fallback if product_url provided
        if product_url:
            try:
                parsed = urllib.parse.urlparse(product_url)
                favicon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
                logger.info(f"🔄 Trying favicon fallback: {favicon_url}")

                session = requests.Session()
                session.headers.update({'User-Agent': random.choice(USER_AGENTS)})
                response = session.get(favicon_url, timeout=5)  # Very short timeout for favicon
                response.raise_for_status()

                content_type = response.headers.get('content-type', '')
                if content_type.startswith('image/'):
                    ext = '.ico'
                    timestamp = int(time.time())
                    image_path = os.path.join(MEDIA_FOLDER, f"deal_favicon_{timestamp}_{random.randint(1000, 9999)}{ext}")
                    with open(image_path, 'wb') as f:
                        f.write(response.content)

                    file_size = os.path.getsize(image_path)
                    if file_size > 100:
                        logger.info(f"✅ Favicon downloaded: {file_size} bytes")
                        return image_path
                    else:
                        os.remove(image_path)

            except Exception as favicon_err:
                logger.error(f"❌ Favicon fallback failed: {favicon_err}")
        
        # Try default image if exists
        default_image_path = os.path.join(MEDIA_FOLDER, 'default.jpg')
        if os.path.exists(default_image_path):
            logger.info("🔄 Using default image fallback")
            return default_image_path
            
        return None

    async def _send_deal_to_channel(self, text, image_url=None, product_url=None):
        """Send deal to channel with image handling and retry logic"""
        image_path = None
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # Try to download image if available
                if image_url and attempt == 0:  # Only try image on first attempt
                    image_path = await self._download_image(image_url, product_url)
                
                # Truncate text if too long for caption
                max_caption_length = 1024
                if len(text) > max_caption_length:
                    text = text[:max_caption_length-3] + "..."
                
                # Send with image if available
                if image_path and os.path.exists(image_path):
                    try:
                        with open(image_path, 'rb') as f:
                            await asyncio.wait_for(
                                self.bot.send_photo(
                                    chat_id=self.target_channel,
                                    photo=f,
                                    caption=text
                                ),
                                timeout=30  # 30 second timeout for image send
                            )
                        logger.info("✅ Deal sent with image successfully")
                        return True
                    except asyncio.TimeoutError:
                        logger.error(f"❌ Image send timed out (attempt {attempt+1})")
                        image_path = None
                    except Exception as img_err:
                        logger.error(f"❌ Image send failed (attempt {attempt+1}): {img_err}")
                        # On retry, don't try image again
                        image_path = None
                
                # Fallback to text
                await asyncio.wait_for(
                    self.bot.send_message(
                        chat_id=self.target_channel,
                        text=text
                    ),
                    timeout=15  # 15 second timeout for text send
                )
                logger.info("✅ Deal sent as text successfully")
                return True
                
            except TelegramError as e:
                if "FloodWait" in str(e):
                    wait_time = TELEGRAM_FLOODWAIT_FALLBACK
                    logger.warning(f"⚠️ Telegram FloodWait, waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                elif attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.error(f"❌ Telegram error (attempt {attempt+1}): {e}")
                    logger.info(f"🔁 Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"❌ Failed to send deal after {max_retries} attempts: {e}")
                    return False
            except Exception as e:
                logger.error(f"❌ Unexpected error sending deal: {e}")
                return False
            finally:
                # Clean up image file
                if image_path and os.path.exists(image_path):
                    try:
                        os.remove(image_path)
                    except:
                        pass
        
        return False

    async def process_website_deals(self):
        """Process deals from all website sources"""
        all_deals = []
        
        # Scrape from all enabled sources
        scraping_tasks = [
            self.scrape_amazon_deals(),
            self.scrape_flipkart_deals()
        ]
        
        # Run all scraping tasks concurrently
        results = await asyncio.gather(*scraping_tasks, return_exceptions=True)
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"❌ Scraping task {i} failed: {result}")
            elif isinstance(result, list):
                all_deals.extend(result)
        
        logger.info(f"📊 Total website deals found: {len(all_deals)}")
        
        # Remove duplicates based on URL
        unique_deals = []
        seen_urls = set()
        for deal in all_deals:
            if deal['url'] not in seen_urls:
                unique_deals.append(deal)
                seen_urls.add(deal['url'])
        
        logger.info(f"📊 Total unique website deals: {len(unique_deals)}")
        
        # Process each unique deal
        processed_count = 0
        send_queue = []
        now = datetime.now()
        for deal in unique_deals:
            try:
                if self.db.is_duplicate(deal['url']):
                    logger.info(f"♻️ Duplicate website deal: {deal['title'][:50]}...")
                    continue
                
                # Only send if price change logic allows
                price_val = self._extract_price(deal.get('price', ''))
                if not self.db.should_send_deal(deal['url'], deal['source'], price_val, now):
                    logger.info(f"⏭️ Skipping deal due to price/time window: {deal['title'][:50]}...")
                    continue
                
                send_queue.append(deal)
            except Exception as e:
                logger.error(f"❌ Error processing website deal: {e}")
                continue
        
        # Prioritize by popularity and discount percentage
        def sort_key(d):
            popularity = d.get('popularity', 0)
            discount = d.get('discount', '0')
            discount_val = float(re.search(r'(\d+)', discount).group(1)) if discount else 0
            return (popularity, discount_val)
        
        send_queue.sort(key=sort_key, reverse=True)
        
        # Limit to top deals
        if len(send_queue) > TOP_DEALS_COUNT:
            send_queue = send_queue[:TOP_DEALS_COUNT]
        
        # Process each deal
        for deal in send_queue:
            try:
                # Create deal text
                original_text = f"{deal['title']}"
                if deal.get('original_price'):
                    original_text += f"\n🔖 Original: {deal['original_price']}"
                if deal.get('price'):
                    original_text += f"\n💰 Discounted: {deal['price']}"
                if deal.get('discount'):
                    original_text += f"\n🎯 Discount: {deal['discount']}"
                original_text += f"\n🛒 {deal['source']}"
                
                # Shorten URL (except Amazon with affiliate)
                if 'amazon' in deal['url'].lower():
                    final_url = self.add_amazon_affiliate(deal['url'])
                else:
                    final_url = await self.shortener.shorten(deal['url'])
                
                # Beautify message
                final_text = self.beautifier.beautify(original_text)
                final_text = f"{final_text}\n\n🔗 {final_url}"
                
                if DRY_RUN:
                    logger.info(f"[DRY RUN] Would send website deal: {deal['title'][:50]}...")
                else:
                    logger.info(f"📤 Sending website deal to @{self.target_channel}...")
                    success = await self._send_deal_to_channel(final_text, deal['image_url'], deal['url'])
                    if success:
                        self.db.save_deal(
                            original_text, final_text, deal['url'], final_url,
                            source_type='website', source_name=deal['source']
                        )
                        # Update last sent info
                        price_val = self._extract_price(deal.get('price', ''))
                        self.db.update_last_sent_info(deal['url'], deal['source'], price_val, now.isoformat())
                        processed_count += 1
                        logger.info(f"✅ Website deal processed and posted successfully!")
                    else:
                        logger.error("❌ Failed to send website deal to channel")
                
                # Rate limiting
                await asyncio.sleep(SEND_INTERVAL)
            except Exception as e:
                logger.error(f"❌ Error sending website deal: {e}")
                continue
        
        logger.info(f"🎯 Website processing complete: {processed_count} deals posted")
        return processed_count

# ======== Advanced Message Beautifier ==========
class MessageBeautifier:
    def __init__(self):
        self.templates = [
            "🔥 HOT DEAL ALERT!\n{msg}\n\n👉 Grab it now before it's gone!",
            "🚀 JUST DROPPED!\n{msg}\n\n⚡ Limited stock available!",
            "🛒 UNBEATABLE OFFER!\n{msg}\n\n🌟 Exclusive for our subscribers!",
            "💥 FLASH SALE!\n{msg}\n\n⏰ Ending soon - don't miss out!",
            "🎯 MUST-HAVE PRODUCT!\n{msg}\n\n💯 Top-rated and highly recommended!",
            "💰 MASSIVE SAVINGS!\n{msg}\n\n✅ Verified deal - shop with confidence!",
            "📢 DEAL OF THE DAY!\n{msg}\n\n❤️ Curated just for you!",
            "⚡ LIGHTNING DEAL!\n{msg}\n\n🔥 Hot price - grab it fast!",
            "🎉 SPECIAL OFFER!\n{msg}\n\n💎 Premium quality at unbeatable price!",
            "🏆 BEST DEAL EVER!\n{msg}\n\n⭐ Don't wait - this won't last!",
        ]
        self.emojis = ["🔥", "🚀", "💥", "🎯", "💰", "🛒", "⚡", "✨", "🌟", "❤️", "✅", "👑", "💎", "🏆", "⭐", "🎉"]
        
    def beautify(self, text):
        """Advanced message beautification with Unicode safety"""
        if not text:
            return ""
        
        # Handle encoding issues and surrogate pairs
        try:
            if isinstance(text, str):
                clean_text = unicodedata.normalize('NFC', text)
            else:
                clean_text = text.decode('utf-8', 'surrogateescape')
        except Exception as e:
            logger.error(f"Unicode normalization error: {e}")
            clean_text = text if isinstance(text, str) else str(text)
        
        # Clean HTML entities
        try:
            clean_text = html.unescape(clean_text)
        except Exception as e:
            logger.error(f"HTML unescape error: {e}")
        
        # Remove unwanted prefixes safely
        prefixes = ["DEAL:", "OFFER:", "HOT DEAL:", "🔥", "🚀", "📢", "💥", "🎯"]
        for prefix in prefixes:
            if clean_text.startswith(prefix):
                clean_text = clean_text.replace(prefix, '', 1).strip()
                break
        
        # Add emojis randomly
        words = clean_text.split()
        if len(words) > 5:
            emoji_count = min(3, len(words) // 5)
            for _ in range(emoji_count):
                pos = random.randint(0, len(words))
                words.insert(pos, random.choice(self.emojis))
            clean_text = " ".join(words)
        
        # Apply template
        template = random.choice(self.templates)
        return template.format(msg=clean_text.strip())

# ======== Advanced Message Processor ==========
class MessageProcessor:
    def __init__(self, db_manager, shortener, beautifier, bot, target_channel):
        self.db = db_manager
        self.shortener = shortener
        self.beautifier = beautifier
        self.bot = bot
        self.target_channel = target_channel
        # Only Amazon and Flipkart domains
        self.product_domains = [
            # Amazon
            "amazon", "amzn.to", "amzn.com", "amazon.in", "amazon.com",
            # Flipkart
            "flipkart", "fkrt", "fkrt.cc", "fkrt.co", "flipkart.com"
        ]
        
    def _contains_product_url(self, text):
        """Advanced product URL detection"""
        if not text:
            return False
        text_lower = text.lower()
        return any(domain in text_lower for domain in self.product_domains)
    
    def _extract_links(self, text):
        """Advanced URL extraction with Unicode safety"""
        if not text:
            return []
        try:
            if not isinstance(text, str):
                try:
                    text = text.decode('utf-8', 'surrogateescape')
                except Exception as decode_err:
                    logger.error(f"Decode error in _extract_links: {decode_err}")
                    return []
            
            # Comprehensive URL regex
            url_pattern = r'https?://[^\s>"\']+|www\.[^\s>"\']+'
            urls = re.findall(url_pattern, text)
            
            # Clean and validate URLs
            cleaned_urls = []
            for url in urls:
                if not url.startswith('http'):
                    url = 'https://' + url
                try:
                    result = urllib.parse.urlparse(url)
                    if all([result.scheme, result.netloc]):
                        cleaned_urls.append(url)
                except Exception:
                    continue
            
            return cleaned_urls
        except Exception as e:
            logger.error(f"URL extraction error: {e}")
            return []
    
    async def _send_to_channel(self, text, media_path=None, is_photo=False):
        """Advanced message sending with retry logic and FloodWait handling"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Truncate text if too long for caption
                max_caption_length = 1024
                if len(text) > max_caption_length:
                    text = text[:max_caption_length-3] + "..."
                
                if media_path and os.path.exists(media_path):
                    try:
                        with open(media_path, "rb") as f:
                            if is_photo:
                                await asyncio.wait_for(
                                    self.bot.send_photo(
                                        chat_id=self.target_channel,
                                        photo=f,
                                        caption=text
                                    ),
                                    timeout=30
                                )
                            else:
                                await asyncio.wait_for(
                                    self.bot.send_video(
                                        chat_id=self.target_channel,
                                        video=f,
                                        caption=text
                                    ),
                                    timeout=30
                                )
                        return True
                    except Exception as img_err:
                        logger.error(f"Image send failed, falling back to text: {img_err}")
                
                # If no image or image failed, send as text
                await asyncio.wait_for(
                    self.bot.send_message(
                        chat_id=self.target_channel,
                        text=text
                    ),
                    timeout=15
                )
                return True
            except TelegramError as e:
                if "FloodWait" in str(e):
                    wait_time = TELEGRAM_FLOODWAIT_FALLBACK
                    logger.warning(f"⚠️ Telegram FloodWait, waiting {wait_time}s...")
                    await asyncio.sleep(wait_time)
                elif attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.error(f"Telegram error (attempt {attempt + 1}): {e}")
                    logger.info(f"🔁 Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"❌ Telegram failed after {max_retries} attempts: {e}")
                    # Final fallback - try text only
                    try:
                        await asyncio.wait_for(
                            self.bot.send_message(
                                chat_id=self.target_channel,
                                text=text
                            ),
                            timeout=15
                        )
                        return True
                    except asyncio.TimeoutError:
                        logger.error("Final fallback timed out")
                        return False
                    except Exception as fallback_error:
                        logger.error(f"Final fallback failed: {fallback_error}")
                        return False
        return False
    
    async def process_message(self, client, msg):
        """Advanced message processing with better logic and image/price extraction"""
        try:
            # Skip non-text messages
            if not (msg.text or msg.caption):
                return False

            text = msg.caption or msg.text
            channel_id = str(msg.chat.id)
            message_id = str(msg.id)

            # Check if message already processed
            if self.db.message_exists(channel_id, message_id):
                logger.info(f"⏭️ Message {message_id} already processed, skipping")
                return False

            # Check for product URLs
            if not self._contains_product_url(text):
                logger.info(f"⏭️ No product URL found in message {message_id}, skipping")
                return False

            logger.info(f"🔍 Processing message {message_id} from {msg.chat.title}")

            # Extract URLs
            urls = self._extract_links(text)
            if not urls:
                logger.info(f"⏭️ No valid URLs found in message {message_id}")
                return False

            # Process each URL
            processed_urls = []
            now = datetime.now()
            for url in urls:
                try:
                    # Check for duplicates
                    if self.db.is_duplicate(url):
                        logger.info(f"⏭️ Duplicate URL found: {url}")
                        continue
                    
                    # Try to extract price from the message text
                    price = None
                    price_match = re.search(r'(₹|Rs\.?|\$|€|£)\s?([\d,]+)', text)
                    if price_match:
                        price = price_match.group(0)
                        try:
                            price_val = float(price.replace('₹','').replace(',','').replace('Rs.','').replace('Rs','').replace('$','').replace('€','').replace('£','').strip())
                        except Exception:
                            price_val = None
                    else:
                        price_val = None
                    
                    # Only send if price change logic allows
                    if not self.db.should_send_deal(url, 'telegram', price_val, now):
                        logger.info(f"⏭️ Skipping Telegram deal due to price/time window: {url}")
                        continue
                    
                    # Shorten URL
                    logger.info(f"🔗 Shortening URL: {url}")
                    gplink = await self.shortener.shorten(url)
                    if not gplink:
                        logger.error(f"❌ Failed to shorten URL: {url}")
                        continue
                    
                    # Try to extract image from the message
                    image_path = None
                    if msg.photo:
                        image_path = os.path.join(MEDIA_FOLDER, f"tg_{message_id}.jpg")
                        await client.download_media(msg.photo, file_name=image_path)
                    
                    # Beautify message
                    try:
                        beautified_text = self.beautifier.beautify(text)
                        if not beautified_text:
                            beautified_text = text
                    except Exception as beautify_err:
                        logger.error(f"Beautify error: {beautify_err}. Using original text.")
                        beautified_text = text
                    
                    if price:
                        beautified_text += f"\n💰 Price: {price}"
                    beautified_text += f"\n🛒 Telegram"
                    beautified_text += f"\n\n🔗 {gplink}"
                    
                    # Send to target channel (with image if available)
                    if image_path and os.path.exists(image_path):
                        success = await self._send_to_channel(beautified_text, media_path=image_path, is_photo=True)
                        try:
                            os.remove(image_path)
                        except Exception:
                            pass
                    else:
                        success = await self._send_to_channel(beautified_text)
                    
                    if success:
                        # Save to database only after successful sending
                        self.db.save_deal(
                            original=text,
                            edited=beautified_text,
                            url=url,
                            gplink=gplink,
                            channel_id=channel_id,
                            message_id=message_id,
                            source_type='telegram',
                            source_name=msg.chat.title or msg.chat.username or channel_id,
                            price=price_val
                        )
                        # Update last sent info
                        self.db.update_last_sent_info(url, 'telegram', price_val, now.isoformat())
                        processed_urls.append(url)
                        logger.info(f"✅ Successfully posted deal with URL: {url}")
                        await asyncio.sleep(SEND_INTERVAL)  # Rate limit
                    else:
                        logger.error(f"❌ Failed to send message to channel")
                except Exception as e:
                    logger.error(f"❌ Error processing URL {url}: {e}")
                    continue
            
            if processed_urls:
                self.db.update_last_message_id(channel_id, msg.id)
                logger.info(f"✅ Processed {len(processed_urls)} URLs from message {message_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Error processing message {message_id}: {e}")
            return False

# ======== Advanced Channel Monitor ==========
class ChannelMonitor:
    def __init__(self, client, db_manager, processor, source_channels):
        self.client = client
        self.db = db_manager
        self.processor = processor
        self.source_channels = source_channels
        self.synced_channels = set()
    
    async def _verify_channel_access(self, channel_id):
        try:
            chat = await self.client.get_chat(channel_id)
            logger.info(f"🔓 Access confirmed: {chat.title} (ID: {chat.id})")
            return True
        except (ChannelInvalid, ChannelPrivate) as e:
            logger.error(f"🔒 PRIVATE CHANNEL: {channel_id}. You must join this channel first.")
        except RPCError as e:
            logger.error(f"⚠️ Access error: {e}")
        return False
    
    async def sync_new_messages(self, channel_id):
        """Sync only new messages since last run"""
        if not await self._verify_channel_access(channel_id):
            logger.error(f"❌ Skipping channel {channel_id} due to access issues.")
            return False
        
        last_message_id = self.db.get_last_message_id(channel_id)
        logger.info(f"🔄 Syncing new messages for {channel_id} since message ID: {last_message_id}")
        
        # If first time, just set the current latest message as sync point
        if last_message_id == 0:
            logger.info(f"🆕 First time setup for {channel_id} - setting sync point")
            try:
                # Get the latest message to set as sync point
                async for message in self.client.get_chat_history(channel_id, limit=1):
                    self.db.update_last_message_id(channel_id, message.id)
                    logger.info(f"📌 Set initial sync point: {message.id}")
                    break
            except Exception as e:
                logger.error(f"❌ Failed to set initial sync point: {e}")
            return True
        
        # Get new messages since last sync
        new_messages = []
        try:
            # Get messages newer than last_message_id
            async for message in self.client.get_chat_history(channel_id, limit=50):
                if message.id > last_message_id:
                    new_messages.append(message)
                else:
                    # Stop when we hit an older message
                    break
        except Exception as e:
            logger.error(f"❌ Failed to fetch message history: {e}")
            return False
                
        # Reverse to process oldest first
        new_messages.reverse()
        
        if not new_messages:
            logger.info(f"✅ No new messages found for {channel_id}")
            return True
        
        logger.info(f"📥 Found {len(new_messages)} new messages to process")
        processed_count = 0
        
        for message in new_messages:
            try:
                # Skip messages without text
                if not (message.text or message.caption):
                    continue

                logger.info(f"📥 Processing new message ID: {message.id}")
                
                if DRY_RUN:
                    logger.info(f"[DRY RUN] Would process message: {message.id}")
                else:
                    success = await self.processor.process_message(self.client, message)
                    if success:
                        processed_count += 1
                        # Update sync point after each successful processing
                        self.db.update_last_message_id(channel_id, message.id)
                # Small delay between messages
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"⚠️ Sync error for message {message.id}: {e}")
                continue
        
        logger.info(f"✅ Completed sync for {channel_id} (processed {processed_count} messages)")
        return True
    
    async def monitor_channels(self):
        logger.info("🚀 Starting advanced channel monitoring...")
        for channel in self.source_channels:
            logger.info(f"📡 Setting up monitoring for {channel}")
            await self.sync_new_messages(channel)
        logger.info("✅ Initial sync completed for all channels")
        
        @self.client.on_message(filters.channel & filters.incoming)
        async def message_handler(client, message):
            try:
                channel_id = str(message.chat.id)
                channel_username = message.chat.username or ""
                identifiers = [c.lower() for c in self.source_channels]
                
                if (channel_id.lower() not in identifiers and 
                    channel_username.lower() not in identifiers and
                    f"@{channel_username}".lower() not in identifiers):
                    return
                
                logger.info(f"🆕 New message detected from {message.chat.title} (ID: {message.id})")
                
                if DRY_RUN:
                    logger.info(f"[DRY RUN] Would process real-time message: {message.id}")
                else:
                    await self.processor.process_message(client, message)
            except Exception as e:
                logger.error(f"❌ Handler error: {e}", exc_info=True)
        
        logger.info("🚀 Real-time monitoring activated - waiting for new messages...")
        status_counter = 0
        while True:
            await asyncio.sleep(3600)
            status_counter += 1
            logger.info(f"💓 Health-check: Monitoring active - Hour {status_counter} - Waiting for new messages...")

# ======== Main Application ==========
async def main():
    logger.info("🚀 Deal Auto Poster - Advanced Edition Starting")
    
    # Initialize core components
    try:
        db_manager = DatabaseManager(DB_PATH)
        logger.info("💾 Database initialized")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return
    
    try:
        shortener = LinkShortener(GP_API_KEY)
        beautifier = MessageBeautifier()
        telegram_bot = Bot(BOT_TOKEN)
        bot_info = await telegram_bot.get_me()
        logger.info(f"🤖 Bot initialized: @{bot_info.username}")
    except Exception as e:
        logger.error(f"❌ Bot initialization failed: {e}")
        return
    
    # Create message processor
    processor = MessageProcessor(
        db_manager, 
        shortener, 
        beautifier,
        telegram_bot, 
        CHANNEL_USERNAME
    )
    
    # Create website scraper
    website_scraper = WebsiteDealScraper(
        db_manager,
        shortener,
        beautifier,
        telegram_bot,
        CHANNEL_USERNAME,
        AMZN_AFFILIATE_ID
    )
    
    # Create Pyrogram client
    try:
        pyro_client = Client(
            "deal_poster_session",
            api_id=API_ID,
            api_hash=API_HASH,
            sleep_threshold=0,
            workers=16,
            workdir=os.getcwd()
        )
        
        await pyro_client.start()
        pyro_user = await pyro_client.get_me()
        logger.info(f"👤 Logged in as {pyro_user.first_name} (ID: {pyro_user.id})")
    except Exception as e:
        logger.error(f"❌ Pyrogram login failed: {e}")
        return
    
    # Initialize channel monitor
    try:
        monitor = ChannelMonitor(
            pyro_client,
            db_manager,
            processor,
            SOURCE_CHANNELS
        )
        
        # Start both Telegram monitoring and website scraping
        logger.info("🚀 Starting both Telegram and Website monitoring...")
        
        try:
            # Create tasks for both monitoring systems
            telegram_task = asyncio.create_task(monitor.monitor_channels())
            website_task = asyncio.create_task(website_monitoring_loop(website_scraper))
            
            # Wait for both tasks
            await asyncio.gather(telegram_task, website_task, return_exceptions=True)
        except Exception as e:
            logger.error(f"❌ Task execution error: {e}")
        finally:
            # Cleanup
            await pyro_client.stop()
            logger.info("✅ Pyrogram client stopped")
        
    except Exception as e:
        logger.error(f"❌ Monitoring failed: {e}")

async def website_monitoring_loop(scraper):
    """Monitor websites for new deals"""
    logger.info("🌐 Starting website deal monitoring...")
    
    while True:
        try:
            logger.info("🔄 Checking websites for new deals...")
            await scraper.process_website_deals()
            
            # Wait before next check
            logger.info(f"⏰ Waiting {SCRAPE_INTERVAL} seconds before next website check...")
            await asyncio.sleep(SCRAPE_INTERVAL)
            
        except asyncio.CancelledError:
            logger.info("🛑 Website monitoring cancelled")
            break
        except Exception as e:
            logger.error(f"❌ Website monitoring error: {e}")
            logger.info("⏰ Waiting 5 minutes before retry...")
            await asyncio.sleep(300)  # Wait 5 minutes on error

# ======== Application Entry Point ==========
if __name__ == "__main__":
    try:
        logger.info("🚀 Starting application main loop")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Application stopped by user")
    except Exception as e:
        logger.error(f"🔥 Critical error: {e}", exc_info=True)
    finally:
        logger.info("🏁 Application shutdown complete")
        logger.info("✅ Resources released")