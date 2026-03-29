"""
Car Flip Bot - Multi-Source Scraper
Sources: Craigslist RSS, OfferUp, Facebook Marketplace
Sends Telegram alerts when a hot deal is found.
"""

import requests
import schedule
import time
import json
import logging
import xml.etree.ElementTree as ET
import re
from dataclasses import dataclass, asdict
from datetime import datetime
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_FLIP_SCORE = 55
MAX_PRICE = 30_000
MIN_PRICE = 3_000
CHECK_INTERVAL_MINUTES = 15

CITIES = [
    "atlanta", "dallas", "houston", "phoenix",
    "denver", "chicago", "miami", "losangeles",
    "seattle", "detroit"
]

OFFERUP_LOCATIONS = [
    {"name": "Atlanta", "lat": 33.749, "lng": -84.388},
    {"name": "Dallas", "lat": 32.776, "lng": -96.797},
    {"name": "Houston", "lat": 29.760, "lng": -95.369},
    {"name": "Phoenix", "lat": 33.448, "lng": -112.074},
    {"name": "Denver", "lat": 39.739, "lng": -104.984},
]

FB_CITIES = [
    ("atlanta", "GA"), ("dallas", "TX"), ("houston", "TX"),
    ("phoenix", "AZ"), ("denver", "CO"), ("chicago", "IL"),
]

HOT_MODELS = [
    "camry", "civic", "accord", "corolla",
    "f-150", "f150", "silverado", "ram",
    "wrangler", "4runner", "tacoma",
    "cr-v", "rav4", "highlander", "escape"
]

MARKET_VALUES = {
    "camry": 19000, "civic": 18000, "accord": 21000, "corolla": 17000,
    "f-150": 38000, "f150": 38000, "silverado": 36000, "ram": 35000,
    "wrangler": 34000, "4runner": 38000, "tacoma": 36000,
    "cr-v": 24000, "rav4": 26000, "highlander": 32000, "escape": 21000,
    "malibu": 16000, "sentra": 15000, "altima": 17000, "fusion": 16000,
    "explorer": 28000, "edge": 24000, "equinox": 21000, "traverse": 28000,
}

# ─────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────
@dataclass
class CarListing:
    title: str
    price: int
    url: str
    location: str
    source: str
    year: int = 0
    make: str = ""
    model: str = ""
    mileage: int = 0
    kbb_value: int = 0
    flip_score: int = 0
    est_profit: int = 0
    found_at: str = ""

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def extract_price(text: str) -> int:
    match = re.search(r'\$[\s]*([\d,]+)', text)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0

def extract_year(text: str) -> int:
    match = re.search(r'\b(19[89]\d|20[012]\d)\b', text)
    return int(match.group()) if match else 0

def extract_mileage(text: str) -> int:
    match = re.search(r'([\d,]+)\s*(?:miles?|mi\b|k\s*miles?)', text, re.IGNORECASE)
    if match:
        val = match.group(1).replace(",", "")
        return int(val) * 1000 if int(val) < 999 else int(val)
    return 0

def parse_make_model(title: str) -> tuple:
    makes = {
        "toyota": ["camry", "corolla", "tacoma", "4runner", "rav4", "highlander", "tundra"],
        "honda": ["civic", "accord", "cr-v", "pilot", "odyssey"],
        "ford": ["f-150", "f150", "mustang", "escape", "explorer", "edge", "fusion"],
        "chevrolet": ["silverado", "malibu", "equinox", "traverse", "tahoe"],
        "chevy": ["silverado", "malibu", "equinox", "traverse", "tahoe"],
        "jeep": ["wrangler", "cherokee", "grand cherokee", "compass"],
        "nissan": ["altima", "sentra", "rogue", "pathfinder", "frontier"],
        "dodge": ["ram", "charger", "challenger", "durango"],
        "ram": ["1500", "2500", "3500"],
        "subaru": ["outback", "forester", "impreza", "crosstrek"],
        "hyundai": ["elantra", "sonata", "tucson", "santa fe"],
        "kia": ["optima", "sorento", "sportage", "soul"],
        "bmw": ["3 series", "5 series", "x3", "x5"],
    }
    title_lower = title.lower()
    for make, models in makes.items():
        if make in title_lower:
            for model in models:
                if model in title_lower:
                    return make.title(), model.title()
            return make.title(), "Unknown"
    return "Unknown", "Unknown"

def estimate_value(model: str, year: int, mileage: int) -> int:
    base = MARKET_VALUES.get(model.lower(), 18000)
    age = datetime.now().year - year if year > 0 else 5
    depreciation = base * (0.88 ** age)
    mileage_adj = max(0, (mileage - 30000) * 0.05) if mileage > 0 else 0
    return max(3000, int(depreciation - mileage_adj))

def compute_score(listing: CarListing) -> int:
    score = 0
    if listing.kbb_value > 0:
        margin_pct = (listing.kbb_value - listing.price) / listing.kbb_value * 100
        if margin_pct >= 30: score += 40
        elif margin_pct >= 20: score += 28
        elif margin_pct >= 12: score += 15
        else: score += 5
    if listing.mileage > 0:
        if listing.mileage < 50000: score += 25
        elif listing.mileage < 80000: score += 18
        elif listing.mileage < 110000: score += 10
        else: score += 3
    else:
        score += 10
    if listing.model.lower() in HOT_MODELS:
        score += 15
    if listing.year >= 2018: score += 20
    elif listing.year >= 2015: score += 12
    elif listing.year >= 2012: score += 6
    return min(score, 100)

# ─────────────────────────────────────────────
# SOURCE 1: CRAIGSLIST RSS
# ─────────────────────────────────────────────
def scrape_craigslist(city: str) -> list:
    url = f"https://{city}.craigslist.org/search/cta?format=rss&min_price={MIN_PRICE}&max_price={MAX_PRICE}&auto_title_status=1"
    headers = {"User-Agent": "Mozilla/5.0"}
    listings = []
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = item.findtext("description", "").strip()
            price = extract_price(title + " " + desc)
            if price and MIN_PRICE <= price <= MAX_PRICE:
                listings.append({
                    "title": title, "url": link, "price": price,
                    "location": city.title(), "source": "Craigslist", "desc": desc
                })
        log.info(f"[Craigslist/{city}] {len(listings)} listings")
    except Exception as e:
        log.error(f"Craigslist error {city}: {e}")
    return listings

# ─────────────────────────────────────────────
# SOURCE 2: OFFERUP
# ─────────────────────────────────────────────
def scrape_offerup(location: dict) -> list:
    url = "https://offerup.com/api/items/search/"
    params = {
        "q": "used car",
        "category_id": 2,
        "lat": location["lat"],
        "lon": location["lng"],
        "radius": 50,
        "price_min": MIN_PRICE,
        "price_max": MAX_PRICE,
        "limit": 24,
    }
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    listings = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        for item in items:
            title = item.get("title", "")
            price_data = item.get("price", {})
            amount = int(float(price_data.get("amount", 0))) if price_data else 0
            item_id = item.get("id", "")
            desc = item.get("description", "")
            if amount and MIN_PRICE <= amount <= MAX_PRICE:
                listings.append({
                    "title": title,
                    "url": f"https://offerup.com/item/detail/{item_id}",
                    "price": amount, "location": location["name"],
                    "source": "OfferUp", "desc": desc
                })
        log.info(f"[OfferUp/{location['name']}] {len(listings)} listings")
    except Exception as e:
        log.error(f"OfferUp error {location['name']}: {e}")
    return listings

# ─────────────────────────────────────────────
# SOURCE 3: FACEBOOK MARKETPLACE
# ─────────────────────────────────────────────
def scrape_facebook(city: str, state: str) -> list:
    url = f"https://www.facebook.com/marketplace/{city}/vehicles"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    listings = []
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        matches = re.findall(
            r'"listing_price":\{"amount":"(\d+)".*?"name":"([^"]+)".*?"id":"(\d+)"',
            resp.text
        )
        for price_str, name, listing_id in matches[:20]:
            price = int(price_str)
            if MIN_PRICE <= price <= MAX_PRICE:
                listings.append({
                    "title": name,
                    "url": f"https://www.facebook.com/marketplace/item/{listing_id}",
                    "price": price, "location": f"{city.title()}, {state}",
                    "source": "Facebook Marketplace", "desc": name
                })
        log.info(f"[Facebook/{city}] {len(listings)} listings")
    except Exception as e:
        log.error(f"Facebook error {city}: {e}")
    return listings

# ─────────────────────────────────────────────
# TELEGRAM ALERT
# ─────────────────────────────────────────────
def send_alert(listing: CarListing):
    margin = listing.kbb_value - listing.price
    pct = (margin / listing.kbb_value * 100) if listing.kbb_value else 0
    emoji = {"Craigslist": "📋", "OfferUp": "🟠", "Facebook Marketplace": "🔵"}.get(listing.source, "🚗")
    msg = (
        f"🚗 *HOT FLIP ALERT* — Score: {listing.flip_score}/100\n"
        f"{emoji} Source: *{listing.source}*\n\n"
        f"*{listing.year if listing.year else '?'} {listing.make} {listing.model}*\n"
        f"📍 {listing.location}\n"
        f"💰 Ask: ${listing.price:,}  |  Est. Value: ${listing.kbb_value:,}\n"
        f"📉 {pct:.1f}% below market  |  Est. profit: ${listing.est_profit:,}\n"
        f"🛣 {listing.mileage:,} miles\n\n"
        f"[View Listing]({listing.url})"
    )
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=10
        )
        if r.status_code == 200:
            log.info(f"Alert sent: {listing.title}")
        else:
            log.error(f"Telegram error {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")

# ─────────────────────────────────────────────
# PROCESS & SCORE
# ─────────────────────────────────────────────
seen_urls: set = set()

def process(raw_listings: list) -> list:
    hot = []
    for raw in raw_listings:
        url = raw.get("url", "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = raw["title"]
        price = raw["price"]
        make, model = parse_make_model(title)
        year = extract_year(title)
        mileage = extract_mileage(title + " " + raw.get("desc", ""))
        if make == "Unknown" or price < MIN_PRICE:
            continue
        kbb = estimate_value(model, year, mileage)
        listing = CarListing(
            title=title, price=price, url=url,
            location=raw["location"], source=raw["source"],
            year=year, make=make, model=model,
            mileage=mileage, kbb_value=kbb,
            found_at=datetime.now().isoformat(),
        )
        listing.flip_score = compute_score(listing)
        listing.est_profit = (kbb - price) - 600
        if listing.flip_score >= MIN_FLIP_SCORE and listing.est_profit > 1000:
            hot.append(listing)
            log.info(f"  🔥 [{listing.source}] {title} — Score {listing.flip_score}, Est. ${listing.est_profit:,}")
    return hot

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run_scan():
    log.info("=== Starting multi-source scan ===")
    all_raw = []

    for city in CITIES:
        all_raw.extend(scrape_craigslist(city))
        time.sleep(1)

    for loc in OFFERUP_LOCATIONS:
        all_raw.extend(scrape_offerup(loc))
        time.sleep(1)

    for city, state in FB_CITIES:
        all_raw.extend(scrape_facebook(city, state))
        time.sleep(2)

    hot_deals = process(all_raw)
    log.info(f"Scan complete. {len(hot_deals)} hot deals found.")

    for deal in sorted(hot_deals, key=lambda x: x.flip_score, reverse=True)[:5]:
        send_alert(deal)

    with open("hot_deals.json", "w") as f:
        json.dump([asdict(d) for d in hot_deals], f, indent=2)

if __name__ == "__main__":
    log.info("Car Flip Bot started.")
    run_scan()
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
