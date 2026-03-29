"""
Car Flip Bot - MarketCheck API Scraper
Uses MarketCheck Cars Search API (via RapidAPI) for real live listings.
Sends Telegram alerts when a hot deal is found.
"""

import requests
import schedule
import time
import json
import logging
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
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")

MIN_FLIP_SCORE = 50
MAX_PRICE = 30_000
MIN_PRICE = 3_000
CHECK_INTERVAL_MINUTES = 15

# Search combinations — make + model
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

# ZIP codes to search around
ZIP_CODES = ["30301", "75201", "77001", "85001", "80201", "60601"]

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
# HELPERS
# ─────────────────────────────────────────────
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
    if listing.year >= 2018: score += 20
    elif listing.year >= 2015: score += 12
    elif listing.year >= 2012: score += 6
    return min(score, 100)

# ─────────────────────────────────────────────
# MARKETCHECK API
# ─────────────────────────────────────────────
def search_marketcheck(make: str, model: str, zip_code: str) -> list:
    url = "https://marketcheck-cars-search-v1.p.rapidapi.com/search"
    params = {
        "make": make,
        "model": model,
        "zip": zip_code,
        "radius": 100,
        "price_min": MIN_PRICE,
        "price_max": MAX_PRICE,
        "car_type": "used",
        "sort_by": "price",
        "sort_order": "asc",
        "rows": 20,
        "start": 0,
    }
    headers = {
        "x-rapidapi-host": "marketcheck-cars-search-v1.p.rapidapi.com",
        "x-rapidapi-key": RAPIDAPI_KEY,
        "Content-Type": "application/json",
    }
    listings = []
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        data = resp.json()
        items = data.get("listings", [])
        for item in items:
            price = item.get("price", 0)
            year = item.get("build", {}).get("year", 0)
            make_name = item.get("build", {}).get("make", make).title()
            model_name = item.get("build", {}).get("model", model).title()
            mileage = item.get("miles", 0)
            city = item.get("dealer", {}).get("city", "")
            state = item.get("dealer", {}).get("state", "")
            vin = item.get("vin", "")
            listing_id = item.get("id", "")

            if price and MIN_PRICE <= price <= MAX_PRICE:
                listings.append({
                    "title": f"{year} {make_name} {model_name}",
                    "price": price,
                    "url": f"https://www.marketcheck.com/car/{vin}/{listing_id}",
                    "location": f"{city}, {state}",
                    "source": "MarketCheck",
                    "make": make_name,
                    "model": model_name,
                    "year": year,
                    "mileage": mileage,
                })
        log.info(f"[MarketCheck] {make} {model} / {zip_code} — {len(listings)} listings")
    except Exception as e:
        log.error(f"MarketCheck error {make} {model}: {e}")
    return listings

# ─────────────────────────────────────────────
# TELEGRAM ALERT
# ─────────────────────────────────────────────
def send_alert(listing: CarListing):
    margin = listing.kbb_value - listing.price
    pct = (margin / listing.kbb_value * 100) if listing.kbb_value else 0
    msg = (
        f"🔥 *HOT FLIP ALERT* — Score: {listing.flip_score}/100\n\n"
        f"*{listing.year} {listing.make} {listing.model}*\n"
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
        if url in seen_urls:
            continue
        seen_urls.add(url)

        price = raw["price"]
        year = raw.get("year", 0)
        make = raw.get("make", "Unknown")
        model = raw.get("model", "Unknown")
        mileage = raw.get("mileage", 0)

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

        if listing.flip_score >= MIN_FLIP_SCORE and listing.est_profit > 800:
            hot.append(listing)
            log.info(f"  🔥 {listing.title} — Score {listing.flip_score}, Est. ${listing.est_profit:,}")
    return hot

# ─────────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────────
def run_scan():
    log.info("=== Starting MarketCheck scan ===")
    all_raw = []

    for make, model in SEARCHES:
        for zip_code in ZIP_CODES[:2]:
            all_raw.extend(search_marketcheck(make, model, zip_code))
            time.sleep(1)

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
