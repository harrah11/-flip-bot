"""
Car Flip Bot - Scraper & Deal Analyzer
Scrapes Craigslist for used car listings and scores them for flip potential.
Sends Telegram alerts when a hot deal is found.

Setup:
    pip install requests beautifulsoup4 playwright python-telegram-bot schedule
    playwright install chromium
"""

import requests
import schedule
import time
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG — edit these
# ─────────────────────────────────────────────
TELEGRAM_TOKEN = "YOUR_BOT_TOKEN"          # from @BotFather
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"          # your chat or group ID
KBB_API_KEY = ""                           # optional: KBB/Edmunds API key
MIN_FLIP_SCORE = 65                        # only alert on deals 65+
MAX_MILEAGE = 120_000
MAX_PRICE = 30_000
CHECK_INTERVAL_MINUTES = 15

# Target cities (Craigslist subdomain, display name)
CITIES = [
    ("atlanta", "Atlanta, GA"),
    ("dallas", "Dallas, TX"),
    ("houston", "Houston, TX"),
    ("phoenix", "Phoenix, AZ"),
    ("denver", "Denver, CO"),
]

# High-demand models that sell fast
HOT_MODELS = [
    "camry", "civic", "accord", "corolla",   # reliable sedans
    "f-150", "f150", "silverado", "ram 1500", # trucks
    "wrangler", "4runner", "tacoma",           # SUVs/off-road
    "cr-v", "rav4", "highlander",              # crossovers
]

# ─────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────
@dataclass
class CarListing:
    title: str
    year: int
    make: str
    model: str
    price: int
    mileage: int
    location: str
    url: str
    days_listed: int = 0
    accident_history: bool = False
    kbb_value: int = 0
    flip_score: int = 0
    est_profit: int = 0
    source: str = "craigslist"
    found_at: str = ""

# ─────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────
class CraigslistScraper:
    """Scrapes used car listings from Craigslist."""

    BASE_URL = "https://{city}.craigslist.org/search/cta"
    HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CarFlipBot/1.0)"}

    def search(self, city: str, max_price: int, max_miles: int) -> list[dict]:
        url = self.BASE_URL.format(city=city)
        params = {
            "max_price": max_price,
            "max_auto_miles": max_miles,
            "auto_title_status": 1,  # clean title only
            "format": "json",
        }
        listings = []
        try:
            resp = requests.get(url, params=params, headers=self.HEADERS, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("li.result-row")
            for item in items:
                listing = self._parse_item(item, city)
                if listing:
                    listings.append(listing)
            log.info(f"[{city}] Found {len(listings)} listings")
        except Exception as e:
            log.error(f"Error scraping {city}: {e}")
        return listings

    def _parse_item(self, item, city: str) -> dict | None:
        try:
            title_el = item.select_one(".result-title")
            price_el = item.select_one(".result-price")
            date_el = item.select_one("time")
            if not title_el or not price_el:
                return None
            title = title_el.text.strip()
            price_str = price_el.text.replace("$", "").replace(",", "").strip()
            price = int(price_str) if price_str.isdigit() else 0
            url = title_el["href"]
            posted = date_el["datetime"] if date_el else ""
            return {"title": title, "price": price, "url": url, "posted": posted, "city": city}
        except Exception:
            return None

    def get_detail(self, url: str) -> dict:
        """Fetch mileage and other details from the listing page."""
        details = {"mileage": 0, "accident": False}
        try:
            resp = requests.get(url, headers=self.HEADERS, timeout=10)
            soup = BeautifulSoup(resp.text, "html.parser")
            attrs = soup.select(".attrgroup span")
            for attr in attrs:
                text = attr.text.lower()
                if "miles" in text:
                    miles = "".join(filter(str.isdigit, text))
                    details["mileage"] = int(miles) if miles else 0
                if "accident" in text or "salvage" in text:
                    details["accident"] = True
        except Exception as e:
            log.warning(f"Could not fetch detail for {url}: {e}")
        return details


# ─────────────────────────────────────────────
# VALUATION
# ─────────────────────────────────────────────
class MarketValueEstimator:
    """
    Estimates market value. In production, use KBB or Edmunds API.
    This fallback uses make/model/year/mileage heuristics.
    """

    BASE_VALUES = {
        "toyota": {"camry": 20000, "corolla": 18000, "4runner": 32000, "tacoma": 35000},
        "honda": {"civic": 19000, "accord": 22000, "cr-v": 25000},
        "ford": {"f-150": 40000, "mustang": 28000, "escape": 22000},
        "jeep": {"wrangler": 36000, "cherokee": 24000},
        "chevrolet": {"silverado": 38000, "malibu": 18000, "equinox": 22000},
        "bmw": {"3 series": 28000, "5 series": 35000},
    }

    def estimate(self, make: str, model: str, year: int, mileage: int) -> int:
        base = self.BASE_VALUES.get(make.lower(), {}).get(model.lower(), 18000)
        age = datetime.now().year - year
        depreciation = base * (0.88 ** age)                    # ~12%/yr
        mileage_adj = max(0, (mileage - 30000) * 0.05)         # $0.05/mile over 30k
        value = max(3000, depreciation - mileage_adj)
        return int(value)


# ─────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────
def compute_flip_score(listing: CarListing) -> int:
    score = 0

    # Margin below market
    if listing.kbb_value > 0:
        margin_pct = (listing.kbb_value - listing.price) / listing.kbb_value * 100
        if margin_pct >= 30: score += 40
        elif margin_pct >= 20: score += 28
        elif margin_pct >= 12: score += 15
        else: score += 5

    # Mileage
    if listing.mileage < 50000: score += 25
    elif listing.mileage < 80000: score += 18
    elif listing.mileage < 110000: score += 10
    else: score += 3

    # Days on market (motivated seller signal)
    if listing.days_listed >= 7: score += 20
    elif listing.days_listed >= 3: score += 12
    else: score += 5

    # Clean title / no accident
    if not listing.accident_history: score += 15

    # Hot model bonus
    model_lower = listing.model.lower()
    if any(hot in model_lower for hot in HOT_MODELS): score += 10

    return min(score, 100)


# ─────────────────────────────────────────────
# TITLE PARSER
# ─────────────────────────────────────────────
def parse_title(title: str) -> tuple[int, str, str]:
    """Extract year, make, model from listing title."""
    import re
    year_match = re.search(r'\b(19|20)\d{2}\b', title)
    year = int(year_match.group()) if year_match else 0
    words = title.split()
    makes = ["toyota", "honda", "ford", "chevy", "chevrolet", "jeep", "bmw",
             "nissan", "hyundai", "kia", "subaru", "dodge", "ram"]
    make, model = "Unknown", "Unknown"
    for i, word in enumerate(words):
        if word.lower() in makes:
            make = word.title()
            model = " ".join(words[i+1:i+3]).title() if i+1 < len(words) else "Unknown"
            break
    return year, make, model


# ─────────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────────
def send_telegram_alert(listing: CarListing):
    margin = listing.kbb_value - listing.price
    pct = (margin / listing.kbb_value * 100) if listing.kbb_value else 0
    msg = (
        f"🚗 *HOT FLIP ALERT* — Score: {listing.flip_score}/100\n\n"
        f"*{listing.year} {listing.make} {listing.model}*\n"
        f"📍 {listing.location}\n"
        f"💰 Ask: ${listing.price:,}  |  KBB: ${listing.kbb_value:,}\n"
        f"📉 {pct:.1f}% below market  |  Est. profit: ${listing.est_profit:,}\n"
        f"🛣 {listing.mileage:,} miles  |  Listed {listing.days_listed}d ago\n"
        f"{'⚠️ Accident history' if listing.accident_history else '✅ Clean title'}\n\n"
        f"[View Listing]({listing.url})"
    )
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        }, timeout=10)
        log.info(f"Alert sent for {listing.title}")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
seen_urls: set[str] = set()
scraper = CraigslistScraper()
valuator = MarketValueEstimator()

def run_scan():
    log.info("═══ Starting scan ═══")
    hot_deals = []

    for city_code, city_name in CITIES:
        raw_listings = scraper.search(city_code, MAX_PRICE, MAX_MILEAGE)

        for raw in raw_listings:
            url = raw.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)

            year, make, model = parse_title(raw["title"])
            if year < 2010 or make == "Unknown":
                continue

            # Fetch detail page
            detail = scraper.get_detail(url)
            mileage = detail.get("mileage", 0)
            accident = detail.get("accident", False)

            if mileage > MAX_MILEAGE or mileage == 0:
                continue

            # Estimate market value
            kbb_est = valuator.estimate(make, model, year, mileage)

            listing = CarListing(
                title=raw["title"],
                year=year, make=make, model=model,
                price=raw["price"],
                mileage=mileage,
                location=city_name,
                url=url,
                accident_history=accident,
                kbb_value=kbb_est,
                source="craigslist",
                found_at=datetime.now().isoformat(),
            )
            listing.flip_score = compute_flip_score(listing)
            listing.est_profit = (kbb_est - raw["price"]) - 600  # minus ~$600 costs

            if listing.flip_score >= MIN_FLIP_SCORE and listing.est_profit > 1500:
                hot_deals.append(listing)
                log.info(f"  🔥 {listing.title} — Score {listing.flip_score}, Est. ${listing.est_profit:,} profit")

    log.info(f"Scan complete. {len(hot_deals)} hot deals found.")

    for deal in sorted(hot_deals, key=lambda x: x.flip_score, reverse=True)[:5]:
        send_telegram_alert(deal)

    # Save results to JSON
    with open("hot_deals.json", "w") as f:
        json.dump([asdict(d) for d in hot_deals], f, indent=2)

    return hot_deals


if __name__ == "__main__":
    log.info("Car Flip Bot started.")
    run_scan()  # run immediately on start
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(30)
