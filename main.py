"""
main.py — Price scraper entry point

Usage:
    # First time setup
    pip install -r requirements.txt
    playwright install chromium

    # Load your product catalog
    python main.py load  products.csv

    # Run a scrape (all products, all three platforms)
    python main.py scrape

    # Scrape only specific platforms
    python main.py scrape --platforms otto
    python main.py scrape --platforms bing
    python main.py scrape --platforms otto,alza,bing

    # Run headful (visible browser, useful for debugging)
    python main.py scrape --headful

    # Use a proxy (format: http://user:pass@host:port)
    python main.py scrape --proxy http://user:pass@proxy.example.com:8080

    # Generate a price comparison CSV
    python main.py report

    # Generate a pricing suggestion CSV (your_price vs market + suggested new price)
    python main.py suggest
"""

import asyncio
import csv
import re
import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime

from database import init_db, upsert_products, get_all_products, save_snapshot, \
    get_last_snapshot, write_alert, get_latest_prices_report
from scrapers import run_scraper
from prepare_and_list import clean_set_number

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def _extract_set_number(title: str) -> str | None:
    """Pull a 5–6 digit LEGO set number from a title like '(75368)'."""
    m = re.search(r'\((\d{5,6})\)', title)
    return m.group(1) if m else None


def _extract_category(title: str) -> str:
    cats = [
        "Botanicals", "Speed Champions", "Technic", "Minecraft",
        "Harry Potter", "Marvel", "Creator", "DUPLO", "Classic",
        "City", "NINJAGO", "Star Wars", "Disney", "Jurassic",
        "Icons", "DREAMZzz", "ART", "BrickHeadz", "Friends",
    ]
    for c in cats:
        if c.lower() in title.lower():
            return c
    if "LEGO" in title.upper():
        return "LEGO Other"
    if "ADIDAS" in title.upper():
        return "Adidas"
    if "APPLE" in title.upper():
        return "Apple"
    if any(k in title for k in ["PlayStation", "Nintendo", "Grand Theft", "Unravel"]):
        return "Video Games"
    return "Other"


def parse_csv(path: str) -> list[dict]:
    """Parse catalog CSV - supports both eBay invoice format and pre-processed catalog format."""
    products = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:

            # ── Pre-processed catalog format (set_number, category, ebay_title, your_price) ──
            if 'ebay_title' in reader.fieldnames:
                title = row.get('ebay_title', '').strip()
                price_str = row.get('your_price', '').strip()
                art_nr = row.get('art_nr', '').strip() or None
                set_number = clean_set_number(row.get('set_number', ''))
                category = row.get('category', '').strip() or None

            # ── Raw eBay invoice format (eBay, Endpreis, Art. Nr.) ──
            else:
                title = row.get('eBay', '').strip()
                price_str = row.get('Endpreis', '').strip()
                art_nr = row.get('Art. Nr.', '').strip() or None
                set_number = None
                category = None

            if not title or not price_str:
                continue
            try:
                price = float(price_str)
            except ValueError:
                continue
            if price <= 0:
                continue

            products.append({
                "ebay_title": title,
                "art_nr": art_nr,
                "set_number": set_number or _extract_set_number(title),
                "category": category or _extract_category(title),
                "your_price": price,
            })

    logger.info(f"Parsed {len(products)} products from {path}")
    return products


# ---------------------------------------------------------------------------
# Scrape command
# ---------------------------------------------------------------------------

async def _on_result(product_id: int, result):
    """Persist each scraped result immediately and check for price changes."""
    last = get_last_snapshot(product_id, result.platform)

    save_snapshot(
        product_id=product_id,
        platform=result.platform,
        url=result.url,
        price=result.price,
        in_stock=result.in_stock,
    )

    # Detect changes vs last snapshot
    if last:
        old_price = last["price"]
        new_price = result.price
        if old_price is None and new_price is not None:
            write_alert(product_id, result.platform, None, new_price, "new")
            logger.info(f"  [ALERT] New price found: €{new_price:.2f}")
        elif old_price is not None and new_price is None:
            write_alert(product_id, result.platform, old_price, None, "out_of_stock")
            logger.warning(f"  [ALERT] Went out of stock (was €{old_price:.2f})")
        elif old_price and new_price and abs(new_price - old_price) / old_price >= 0.03:
            direction = "drop" if new_price < old_price else "rise"
            write_alert(product_id, result.platform, old_price, new_price, direction)
            pct = (new_price - old_price) / old_price * 100
            logger.info(f"  [ALERT] Price {direction}: €{old_price:.2f} → €{new_price:.2f} ({pct:+.1f}%)")


async def cmd_scrape(args):
    platforms = [p.strip() for p in args.platforms.split(",")]
    proxy = {"server": args.proxy} if args.proxy else None
    products = [dict(p) for p in get_all_products()]

    if not products:
        logger.error("No products in database. Run 'python main.py load <csv_path>' first.")
        sys.exit(1)

    logger.info(f"Starting scrape of {len(products)} products on {platforms}")
    logger.info("This will take a while — roughly 3–5 seconds per product per platform.\n")

    await run_scraper(
        products=products,
        platforms=platforms,
        headless=not args.headful,
        proxy=proxy,
        on_result=_on_result,
    )
    logger.info("Scrape complete.")


# ---------------------------------------------------------------------------
# Report command
# ---------------------------------------------------------------------------

def cmd_report(_args):
    rows = get_latest_prices_report()
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = OUTPUT_DIR / f"price_comparison_{ts}.csv"

    with open(out, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow([
            "Set Number", "Category", "eBay Title",
            "Your Price (€)", "Otto (€)", "Alza (€)", "Bing Shopping (€)",
            "Market Floor (€)", "Diff vs Otto", "Diff vs Alza", "Diff vs Bing",
        ])
        for r in rows:
            your   = r["your_price"]
            otto   = r["otto_price"]
            alza   = r["alza_price"]
            bing   = r["bing_price"]
            market_prices = [p for p in [otto, alza, bing] if p]
            floor = min(market_prices) if market_prices else None
            writer.writerow([
                r["set_number"] or "",
                r["category"] or "",
                r["ebay_title"][:80],
                f"{your:.2f}",
                f"{otto:.2f}"   if otto   else "–",
                f"{alza:.2f}"   if alza   else "–",
                f"{bing:.2f}"   if bing   else "–",
                f"{floor:.2f}"  if floor  else "–",
                f"{your - otto:+.2f}"   if otto   else "–",
                f"{your - alza:+.2f}"   if alza   else "–",
                f"{your - bing:+.2f}"   if bing   else "–",
            ])

    logger.info(f"Report saved to {out}")
    print(f"\nReport: {out}")


# ---------------------------------------------------------------------------
# Suggest command — pricing recommendations
# ---------------------------------------------------------------------------

MARGIN_TARGET = 0.15       # aim for at least 15% above market where possible
CEILING_FACTOR = 1.25      # never suggest more than 25% above highest market price
PENNY_ENDINGS = [0.99, 0.49, 0.95]  # round suggested prices to these endings


def _round_to_ending(price: float) -> float:
    """Round to the nearest .99 / .49 / .95 ending."""
    import math
    base = math.floor(price)
    best = None
    best_dist = float('inf')
    for ending in PENNY_ENDINGS:
        candidate = base + ending
        dist = abs(candidate - price)
        if dist < best_dist:
            best_dist = dist
            best = candidate
        candidate2 = base + 1 + ending - 1  # next integer + ending
        dist2 = abs(candidate2 - price)
        if dist2 < best_dist:
            best_dist = dist2
            best = candidate2
    return best or price


def cmd_suggest(_args):
    rows = get_latest_prices_report()
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = OUTPUT_DIR / f"price_suggestions_{ts}.csv"

    raised = 0
    total = 0

    with open(out, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow([
            "Set Number", "Category", "eBay Title (truncated)",
            "Current Price (€)", "Otto (€)", "Alza (€)", "Bing Shopping (€)",
            "Market Floor (€)", "Suggested Price (€)", "Uplift (€)", "Uplift (%)", "Rationale",
        ])

        for r in rows:
            total += 1
            your   = r["your_price"]
            otto   = r["otto_price"]
            alza   = r["alza_price"]
            bing   = r["bing_price"]
            market_prices = [p for p in [otto, alza, bing] if p]

            if not market_prices:
                writer.writerow([
                    r["set_number"] or "", r["category"] or "",
                    r["ebay_title"][:70], f"{your:.2f}",
                    "–", "–", "–", "–", f"{your:.2f}", "–", "–",
                    "No market data yet",
                ])
                continue

            market_floor = min(market_prices)
            market_max   = max(market_prices)

            # Bing Shopping is the market-floor reference — it aggregates German retailer
            # prices without bot-detection issues. Falls back to min(otto, alza).
            floor_ref = bing if bing else market_floor

            # Target: floor_ref + margin, capped at ceiling
            target_raw  = floor_ref * (1 + MARGIN_TARGET)
            ceiling     = market_max * CEILING_FACTOR
            suggested_raw = min(target_raw, ceiling)
            suggested   = _round_to_ending(suggested_raw)

            # Build rationale string
            if bing:
                rationale_base = f"Bing floor €{bing:.2f} × {1+MARGIN_TARGET:.0%} margin"
            else:
                rationale_base = f"Market min €{market_floor:.2f} × {1+MARGIN_TARGET:.0%} margin"

            if suggested < your:
                suggested = your
                rationale = "Already above suggested floor — keep current"
            elif suggested > your * 1.01:
                raised += 1
                rationale = rationale_base
            else:
                rationale = "Minimal uplift available"

            uplift     = suggested - your
            uplift_pct = uplift / your * 100

            writer.writerow([
                r["set_number"] or "", r["category"] or "",
                r["ebay_title"][:70],
                f"{your:.2f}",
                f"{otto:.2f}"   if otto   else "–",
                f"{alza:.2f}"   if alza   else "–",
                f"{bing:.2f}"   if bing   else "–",
                f"{market_floor:.2f}",
                f"{suggested:.2f}",
                f"{uplift:+.2f}",
                f"{uplift_pct:+.1f}%",
                rationale,
            ])

    logger.info(f"Suggestion report: {raised}/{total} products have upward pricing potential.")
    logger.info(f"Saved to {out}")
    print(f"\nSuggestion report: {out}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description="eBay dropship price scraper")
    sub = parser.add_subparsers(dest="command")

    # load
    p_load = sub.add_parser("load", help="Load products from eBay CSV into the database")
    p_load.add_argument("csv_path", help="Path to your CSV file")

    # scrape
    p_scrape = sub.add_parser("scrape", help="Scrape prices from Otto.de and Alza.de")
    p_scrape.add_argument("--platforms", default="otto,alza,bing",
                          help="Comma-separated list of platforms (default: otto,alza,bing)")
    p_scrape.add_argument("--headful", action="store_true",
                          help="Show the browser window (useful for debugging)")
    p_scrape.add_argument("--proxy", default=None,
                          help="Proxy URL e.g. http://user:pass@host:port")

    # report
    sub.add_parser("report", help="Export latest price comparison to CSV")

    # suggest
    sub.add_parser("suggest", help="Generate pricing suggestions based on market data")

    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    init_db()

    if args.command == "load":
        products = parse_csv(args.csv_path)
        upsert_products(products)
        logger.info(f"Loaded {len(products)} products into the database.")

    elif args.command == "scrape":
        asyncio.run(cmd_scrape(args))

    elif args.command == "report":
        cmd_report(args)

    elif args.command == "suggest":
        cmd_suggest(args)

    else:
        parser.print_help()
