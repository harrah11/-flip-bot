"""
Car Flip Bot - Cloud-Friendly Multi-Source Scraper
Sources: AutoTempest, CarGurus RSS, Cars.com RSS, CarsDirect
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

MIN_FLIP_SCORE = 50
MAX_PRICE = 30_000
MIN_PRICE = 3_000
CHECK_INTERVAL_MINUTES = 15

# Search terms — bot will search each of these
SEARCH_TERMS = [
    "toyota camry", "honda civic", "honda accord",
    "ford f-150", "chevy silverado", "toyota tacoma",
    "jeep wrangler", "toyota 4runner", "toyota rav4",
    "honda cr-v", "nissan altima", "ford escape",
]

# US zip codes for location-based searches
ZIP_CODES = [
    "30301",  # Atlanta
    "75201",  # Dallas
    "77001",  # Houston
    "85001",  # Phoenix
    "80201",  # Denver
    "60601",  # Chicago
    "33101",  # Miami
    "90001",  # Los Angeles
]

MARKET_VALUES = {
    "camry": 19000, "civic": 18000, "accord": 21000, "corolla": 17000,
    "f-150": 38000, "f150": 38000, "silverado": 36000, "ram": 35000,
    "wrangler": 34000, "4runner": 38000, "tacoma": 36000,
    "cr-v": 24000, "rav4": 26000, "highlander": 32000, "escape": 21000,
    "malibu": 16000, "sentra": 15000, "altima": 17000, "fusion": 16000,
    "explorer": 28000, "edge": 24000, "equinox": 21000, "traverse": 28000,
}

HOT_MODELS = list(MARKET_VALUES.keys())

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
        "toyota": ["camry", "corolla", "tacoma", "4runner", "rav4", "highlander", "tundra", "sienna"],
        "honda": ["civic", "accord", "cr-v", "pilot", "odyssey", "hrv", "ridgeline"],
        "ford": ["f-150", "f150", "mustang", "escape", "explorer", "edge", "fusion", "ranger"],
        "chevrolet": ["silverado", "malibu", "equinox", "traverse", "tahoe", "suburban", "colorado"],
        "chevy": ["silverado", "malibu", "equinox", "traverse", "tahoe", "suburban"],
        "jeep": ["wrangler", "cherokee", "grand cherokee", "compass", "gladiator"],
        "nissan": ["altima", "sentra", "rogue", "pathfinder", "frontier", "maxima", "murano"],
        "dodge": ["ram", "charger", "challenger", "durango", "journey"],
        "ram": ["1500", "2500", "3500", "promaster"],
        "subaru": ["outback", "forester", "impreza", "crosstrek", "legacy", "wrx"],
        "hyundai": ["elantra", "sonata", "tucson", "santa fe", "kona", "palisade"],
        "kia": ["optima", "sorento", "sportage", "soul", "telluride", "stinger"],
        "bmw": ["3 series", "5 series", "x3", "x5", "330i", "530i"],
        "mercedes": ["c-class", "e-class", "glc", "gle", "c300", "e300"],
        "volkswagen": ["jetta", "passat", "tiguan", "atlas", "golf"],
        "audi": ["a4", "a6", "q5", "q7", "q3"],
        "lexus": ["rx", "es", "is", "nx", "gx"],
        "acura": ["mdx", "rdx", "tlx", "tsx"],
        "mazda": ["cx-5", "cx5", "mazda3", "mazda6", "cx-9"],
        "gmc": ["sierra", "terrain", "acadia", "yukon", "canyon"],
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
# SOURCE 1: CARS.COM RSS
# ─────────────────────────────────────────────
def scrape_cars_com(search_term: str, zip_code: str) -> list:
    make, model = search_term.split(" ", 1) if " " in search_term else (search_term, "")
    url = "https://www.cars.com/shopping/results/"
    params = {
        "stock_type": "used",
        "makes[]": make.lower(),
        "models[]": f"{make.lower()}-{model.lower().replace(' ', '_')}",
        "maximum_distance": 100,
        "zip": zip_code,
        "price_max": MAX_PRICE,
        "price_min": MIN_PRICE,
        "sort": "price_lowest",
        "per_page": 20,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }
    listings = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        text = resp.text

        # Extract listing data from page
        prices = re.findall(r'\$(\d{1,2},\d{3})', text)
        titles = re.findall(r'(\d{4}\s+\w+\s+[\w\s-]+?)(?=\s*\$|\s*·)', text)
        urls_found = re.findall(r'href="(/vehicledetail/[^"]+)"', text)
        mileages = re.findall(r'([\d,]+)\s*mi\.', text)

        for i, url_path in enumerate(urls_found[:15]):
            price_val = int(prices[i].replace(",", "")) if i < len(prices) else 0
            title_val = titles[i].strip() if i < len(titles) else search_term.title()
            mileage_val = int(mileages[i].replace(",", "")) if i < len(mileages) else 0

            if price_val and MIN_PRICE <= price_val <= MAX_PRICE:
                listings.append({
                    "title": title_val,
                    "url": f"https://www.cars.com{url_path}",
                    "price": price_val,
                    "location": f"ZIP {zip_code}",
                    "source": "Cars.com",
                    "desc": f"{mileage_val} miles"
                })

        log.info(f"[Cars.com/{search_term}/{zip_code}] {len(listings)} listings")
    except Exception as e:
        log.error(f"Cars.com error {search_term}: {e}")
    return listings

# ─────────────────────────────────────────────
# SOURCE 2: AUTOTEMPEST (aggregates multiple sites)
# ─────────────────────────────────────────────
def scrape_autotempest(search_term: str, zip_code: str) -> list:
    parts = search_term.split(" ", 1)
    make = parts[0]
    model = parts[1] if len(parts) > 1 else ""
    url = "https://www.autotempest.com/results"
    params = {
        "make": make.lower(),
        "model": model.lower().replace(" ", "-"),
        "zip": zip_code,
        "radius": 100,
        "minyear": 2012,
        "maxprice": MAX_PRICE,
        "minprice": MIN_PRICE,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.autotempest.com/",
    }
    listings = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        text = resp.text

        # Parse listing cards
        blocks = re.findall(
            r'class="[^"]*listing[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>.*?'
            r'(\d{4}\s+[\w\s]+?)(?:</|·).*?\$([\d,]+)',
            text, re.DOTALL
        )

        for listing_url, title, price_str in blocks[:15]:
            price = int(price_str.replace(",", ""))
            if MIN_PRICE <= price <= MAX_PRICE:
                listings.append({
                    "title": title.strip(),
                    "url": listing_url if listing_url.startswith("http") else f"https://www.autotempest.com{listing_url}",
                    "price": price,
                    "location": f"ZIP {zip_code}",
                    "source": "AutoTempest",
                    "desc": title
                })

        log.info(f"[AutoTempest/{search_term}] {len(listings)} listings")
    except Exception as e:
        log.error(f"AutoTempest error {search_term}: {e}")
    return listings

# ─────────────────────────────────────────────
# SOURCE 3: CARSDIRECT RSS FEED
# ─────────────────────────────────────────────
def scrape_carsdirect() -> list:
    url = "https://www.carsdirect.com/rss/deals"
    headers = {"User-Agent": "Mozilla/5.0"}
    listings = []
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = item.findtext("description", "").strip()
            price = extract_price(title + " " + desc)
            if price and MIN_PRICE <= price <= MAX_PRICE:
                listings.append({
                    "title": title, "url": link, "price": price,
                    "location": "National", "source": "CarsDirect", "desc": desc
                })
        log.info(f"[CarsDirect] {len(listings)} listings")
    except Exception as e:
        log.error(f"CarsDirect error: {e}")
    return listings

# ─────────────────────────────────────────────
# SOURCE 4: CARGURUS (public search)
# ─────────────────────────────────────────────
def scrape_cargurus(search_term: str, zip_code: str) -> list:
    parts = search_term.split(" ", 1)
    make = parts[0].title()
    model = parts[1].title() if len(parts) > 1 else ""
    url = "https://www.cargurus.com/Cars/inventoryResults.action"
    params = {
        "zip": zip_code,
        "showNegotiable": "true",
        "sortDir": "ASC",
        "sortType": "PRICE",
        "maxPrice": MAX_PRICE,
        "minPrice": MIN_PRICE,
        "distance": 100,
        "entitySelectingHelper.selectedEntity": f"{make} {model}",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.cargurus.com/",
    }
    listings = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        for listing in data.get("listings", [])[:15]:
            price = listing.get("price", 0)
            year = listing.get("year", 0)
            make_name = listing.get("makeName", "")
            model_name = listing.get("modelName", "")
            mileage = listing.get("mileage", 0)
            listing_id = listing.get("id", "")
            city = listing.get("city", "")
            state = listing.get("stateCode", "")

            if price and MIN_PRICE <= price <= MAX_PRICE:
                listings.append({
                    "title": f"{year} {make_name} {model_name}",
                    "url": f"https://www.cargurus.com/Cars/inventoryResults.action#listing={listing_id}",
                    "price": price,
                    "location": f"{city}, {state}",
                    "source": "CarGurus",
                    "desc": f"{mileage} miles"
                })
        log.info(f"[CarGurus/{search_term}/{zip_code}] {len(listings)} listings")
    except Exception as e:
        log.error(f"CarGurus error {search_term}: {e}")
    return listings

# ─────────────────────────────────────────────
# TELEGRAM ALERT
# ─────────────────────────────────────────────
def send_alert(listing: CarListing):
    margin = listing.kbb_value - listing.price
    pct = (margin / listing.kbb_value * 100) if listing.kbb_value else 0
    emoji = {
        "Cars.com": "🚗", "AutoTempest": "🔍",
        "CarsDirect": "💼", "CarGurus": "📊"
    }.get(listing.source, "🚗")
    msg = (
        f"🔥 *HOT FLIP ALERT* — Score: {listing.flip_score}/100\n"
        f"{emoji} Source: *{listing.source}*\n\n"
        f"*{listing.year if listing.year else '?'} {listing.make} {listing.model}*\n"
        f"📍 {listing.location}\n"
        f"💰 Ask: ${listing.price:,}  |  Est. Value: ${listing.kbb_value:,}\n"
        f"📉 {pct:.1f}% below market\n"
        f"💵 Est. profit after costs: ${listing.est_profit:,}\n"
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
            log.info(f"✅ Alert sent: {listing.title}")
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
        if price < MIN_PRICE:
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
        if listing.flip_score >= MIN_FLIP_SCORE and listing.est_profit > 800:
            hot.append(listing)
            log.info(f"  🔥 [{listing.source}] {title} — Score {listing.flip_score}, Est. ${listing.est_profit:,}")
    return hot

# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────
def run_scan():
    log.info("=== Starting multi-source scan ===")
    all_raw = []

    # CarGurus — best source, try first
    for term in SEARCH_TERMS[:6]:
        for zip_code in ZIP_CODES[:3]:
            all_raw.extend(scrape_cargurus(term, zip_code))
            time.sleep(1)

    # Cars.com
    for term in SEARCH_TERMS[:4]:
        for zip_code in ZIP_CODES[:2]:
            all_raw.extend(scrape_cars_com(term, zip_code))
            time.sleep(1)

    # AutoTempest
    for term in SEARCH_TERMS[:4]:
        all_raw.extend(scrape_autotempest(term, ZIP_CODES[0]))
        time.sleep(1)

    # CarsDirect RSS
    all_raw.extend(scrape_carsdirect())

    hot_deals = process(all_raw)
    log.info(f"=== Scan complete. {len(hot_deals)} hot deals found. ===")

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
