"""
Car Flip Bot - ScraperAPI Version
Routes all requests through ScraperAPI to bypass blocking.
Sources: Craigslist, CarGurus, Cars.com, AutoTrader
Sends Telegram alerts when a hot deal is found.
"""

import requests
import schedule
import time
import json
import logging
import re
import xml.etree.ElementTree as ET
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
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")

MIN_FLIP_SCORE = 50
MAX_PRICE = 30_000
MIN_PRICE = 3_000
CHECK_INTERVAL_MINUTES = 15

SEARCHES = [
    ("toyota", "camry"),
    ("toyota", "tacoma"),
    ("toyota", "rav4"),
    ("honda", "civic"),
    ("honda", "accord"),
    ("ford", "f-150"),
    ("chevrolet", "silverado"),
    ("jeep", "wrangler"),
    ("toyota", "4runner"),
    ("honda", "cr-v"),
]

CRAIGSLIST_CITIES = [
    "atlanta", "dallas", "houston", "phoenix",
    "denver", "chicago", "miami", "losangeles",
]

ZIP_CODES = [
    ("30301", "Atlanta, GA"),
    ("75201", "Dallas, TX"),
    ("77001", "Houston, TX"),
    ("85001", "Phoenix, AZ"),
    ("80201", "Denver, CO"),
]

MARKET_VALUES = {
    "camry": 19000, "civic": 18000, "accord": 21000, "corolla": 17000,
    "f-150": 38000, "silverado": 36000, "ram 1500": 35000,
    "wrangler": 34000, "4runner": 38000, "tacoma": 36000,
    "cr-v": 24000, "rav4": 26000, "highlander": 32000, "escape": 21000,
    "malibu": 16000, "altima": 17000, "fusion": 16000,
    "explorer": 28000, "equinox": 21000, "traverse": 28000,
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
# SCRAPERAPI FETCH
# ─────────────────────────────────────────────
def scraper_get(url: str, timeout: int = 30) -> requests.Response:
    """Route request through ScraperAPI to bypass blocking."""
    api_url = f"http://api.scraperapi.com?api_key={SCRAPER_API_KEY}&url={requests.utils.quote(url)}&render=false"
    return requests.get(api_url, timeout=timeout)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def extract_price(text: str) -> int:
    match = re.search(r'\$[\s]*([\d,]+)', text)
    if match:
        val = int(match.group(1).replace(",", ""))
        return val if MIN_PRICE <= val <= MAX_PRICE else 0
    return 0

def extract_year(text: str) -> int:
    match = re.search(r'\b(19[89]\d|20[012]\d)\b', text)
    return int(match.group()) if match else 0

def extract_mileage(text: str) -> int:
    match = re.search(r'([\d,]+)\s*(?:miles?|mi\.?\b)', text, re.IGNORECASE)
    if match:
        val = int(match.group(1).replace(",", ""))
        return val * 1000 if val < 999 else val
    return 0

def estimate_value(model: str, year: int, mileage: int) -> int:
    base = MARKET_VALUES.get(model.lower(), 18000)
    age = max(0, datetime.now().year - year) if year > 0 else 5
    value = base * (0.88 ** age)
    if mileage > 30000:
        value -= (mileage - 30000) * 0.05
    return max(3000, int(value))

def compute_score(listing: CarListing) -> int:
    score = 0
    if listing.kbb_value > 0 and listing.price > 0:
        pct = (listing.kbb_value - listing.price) / listing.kbb_value * 100
        if pct >= 30: score += 40
        elif pct >= 20: score += 28
        elif pct >= 12: score += 15
        else: score += 5
    if listing.mileage > 0:
        if listing.mileage < 50000: score += 25
        elif listing.mileage < 80000: score += 18
        elif listing.mileage < 110000: score += 10
        else: score += 3
    else:
        score += 10
    if listing.year >= 2018: score += 20
    elif listing.year >= 2015: score += 12
    elif listing.year >= 2012: score += 6
    return min(score, 100)

# ─────────────────────────────────────────────
# SOURCE 1: CRAIGSLIST RSS via ScraperAPI
# ─────────────────────────────────────────────
def scrape_craigslist(make: str, model: str, city: str) -> list:
    query = f"{make}+{model}"
    url = f"https://{city}.craigslist.org/search/cta?format=rss&query={query}&min_price={MIN_PRICE}&max_price={MAX_PRICE}&auto_title_status=1&srchType=T"
    listings = []
    try:
        resp = scraper_get(url)
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            desc = item.findtext("description", "").strip()
            price = extract_price(title + " " + desc)
            if price:
                listings.append({
                    "title": title, "url": link, "price": price,
                    "location": city.title(), "source": "Craigslist",
                    "make": make.title(), "model": model.title(),
                    "year": extract_year(title),
                    "mileage": extract_mileage(title + " " + desc),
                    "desc": desc,
                })
        log.info(f"[Craigslist] {make} {model}/{city} — {len(listings)} listings")
    except Exception as e:
        log.error(f"Craigslist error {city}: {e}")
    return listings

# ─────────────────────────────────────────────
# SOURCE 2: AUTOTRADER via ScraperAPI
# ─────────────────────────────────────────────
def scrape_autotrader(make: str, model: str, zip_code: str, city: str) -> list:
    url = f"https://www.autotrader.com/cars-for-sale/used-cars/{make}/{model}/{zip_code}?maxPrice={MAX_PRICE}&minPrice={MIN_PRICE}&searchRadius=100&sortBy=priceASC&numRecords=25"
    listings = []
    try:
        resp = scraper_get(url)
        text = resp.text
        prices = re.findall(r'"price":(\d+)', text)
        years = re.findall(r'"year":(\d{4})', text)
        makes_found = re.findall(r'"make":"([^"]+)"', text)
        models_found = re.findall(r'"model":"([^"]+)"', text)
        miles = re.findall(r'"mileage":(\d+)', text)
        listing_ids = re.findall(r'"listingId":"([^"]+)"', text)

        for i in range(min(len(prices), len(listing_ids), 15)):
            price = int(prices[i]) if i < len(prices) else 0
            if not price or price < MIN_PRICE or price > MAX_PRICE:
                continue
            yr = int(years[i]) if i < len(years) else 0
            mk = makes_found[i].title() if i < len(makes_found) else make.title()
            md = models_found[i].title() if i < len(models_found) else model.title()
            mi = int(miles[i]) if i < len(miles) else 0
            lid = listing_ids[i] if i < len(listing_ids) else ""
            listings.append({
                "title": f"{yr} {mk} {md}",
                "price": price,
                "url": f"https://www.autotrader.com/cars-for-sale/{lid}",
                "location": city,
                "source": "AutoTrader",
                "make": mk, "model": md,
                "year": yr, "mileage": mi,
                "desc": f"{mi} miles",
            })
        log.info(f"[AutoTrader] {make} {model}/{city} — {len(listings)} listings")
    except Exception as e:
        log.error(f"AutoTrader error {make} {model}: {e}")
    return listings

# ─────────────────────────────────────────────
# SOURCE 3: CARS.COM via ScraperAPI
# ─────────────────────────────────────────────
def scrape_cars_com(make: str, model: str, zip_code: str, city: str) -> list:
    model_slug = model.lower().replace(" ", "-").replace("/", "-")
    url = f"https://www.cars.com/shopping/results/?stock_type=used&makes[]={make.lower()}&models[]={make.lower()}-{model_slug}&zip={zip_code}&maximum_distance=100&price_max={MAX_PRICE}&price_min={MIN_PRICE}&sort=price_lowest&per_page=20"
    listings = []
    try:
        resp = scraper_get(url)
        text = resp.text
        prices = re.findall(r'"price":\s*"?\$?([\d,]+)"?', text)
        years = re.findall(r'"year":\s*"?(\d{4})"?', text)
        makes_found = re.findall(r'"make":\s*"([^"]+)"', text)
        models_found = re.findall(r'"model":\s*"([^"]+)"', text)
        miles = re.findall(r'"mileage":\s*"?([\d,]+)"?', text)
        slugs = re.findall(r'href="(/vehicledetail/[^"]+)"', text)

        for i in range(min(len(prices), len(slugs), 15)):
            price_str = prices[i].replace(",", "")
            price = int(price_str) if price_str.isdigit() else 0
            if not price or price < MIN_PRICE or price > MAX_PRICE:
                continue
            yr = int(years[i]) if i < len(years) else 0
            mk = makes_found[i].title() if i < len(makes_found) else make.title()
            md = models_found[i].title() if i < len(models_found) else model.title()
            mi_str = miles[i].replace(",", "") if i < len(miles) else "0"
            mi = int(mi_str) if mi_str.isdigit() else 0
            slug = slugs[i] if i < len(slugs) else ""
            listings.append({
                "title": f"{yr} {mk} {md}",
                "price": price,
                "url": f"https://www.cars.com{slug}",
                "location": city,
                "source": "Cars.com",
                "make": mk, "model": md,
                "year": yr, "mileage": mi,
                "desc": f"{mi} miles",
            })
        log.info(f"[Cars.com] {make} {model}/{city} — {len(listings)} listings")
    except Exception as e:
        log.error(f"Cars.com error {make} {model}: {e}")
    return listings

# ─────────────────────────────────────────────
# TELEGRAM ALERT
# ─────────────────────────────────────────────
def send_alert(listing: CarListing):
    margin = listing.kbb_value - listing.price
    pct = (margin / listing.kbb_value * 100) if listing.kbb_value else 0
    emoji = {
        "Craigslist": "📋", "AutoTrader": "🚘", "Cars.com": "🚗"
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
        log.error(f"Telegram error: {e}")

# ─────────────────────────────────────────────
# PROCESS & SCORE
# ─────────────────────────────────────────────
seen_urls: set = set()

def process(raw_listings: list) -> list:
    hot = []
    for raw in raw_listings:
        url = raw.get("url", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        price = raw.get("price", 0)
        if not price or price < MIN_PRICE or price > MAX_PRICE:
            continue
        make = raw.get("make", "Unknown")
        model = raw.get("model", "Unknown")
        year = raw.get("year", 0) or extract_year(raw.get("title", ""))
        mileage = raw.get("mileage", 0) or extract_mileage(raw.get("desc", ""))
        kbb = estimate_value(model, year, mileage)
        listing = CarListing(
            title=raw["title"], price=price, url=url,
            location=raw["location"], source=raw["source"],
            year=year, make=make, model=model,
            mileage=mileage, kbb_value=kbb,
            found_at=datetime.now().isoformat(),
        )
        listing.flip_score = compute_score(listing)
        listing.est_profit = (kbb - price) - 600
        if listing.flip_score >= MIN_FLIP_SCORE and listing.est_profit > 500:
            hot.append(listing)
            log.info(f"  🔥 [{listing.source}] {listing.title} — Score {listing.flip_score}, Est. ${listing.est_profit:,}")
    return hot

# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────
def run_scan():
    log.info("=== Starting ScraperAPI scan ===")
    all_raw = []

    # Craigslist RSS
    for make, model in SEARCHES[:5]:
        for city in CRAIGSLIST_CITIES[:3]:
            all_raw.extend(scrape_craigslist(make, model, city))
            time.sleep(2)

    # AutoTrader
    for make, model in SEARCHES[:4]:
        for zip_code, city in ZIP_CODES[:2]:
            all_raw.extend(scrape_autotrader(make, model, zip_code, city))
            time.sleep(2)

    # Cars.com
    for make, model in SEARCHES[:4]:
        for zip_code, city in ZIP_CODES[:2]:
            all_raw.extend(scrape_cars_com(make, model, zip_code, city))
            time.sleep(2)

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
