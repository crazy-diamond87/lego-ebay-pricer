"""
catalog_builder.py — Rebuild your catalog by scraping LEGO sets from Otto.de

Searches Otto for "lego" filtered to €10–€70, sorted by bestsellers, paginates
through results, and writes a catalog CSV ready to load into the Price Scraper app.

Usage:
    python catalog_builder.py                    # top 5 pages (~100 products)
    python catalog_builder.py --pages 10         # ~200 products
    python catalog_builder.py --headful          # show browser (recommended first run)
    python catalog_builder.py --debug            # save screenshot + HTML for inspection
    python catalog_builder.py --proxy http://user:pass@host:port

Output CSV (compatible with the Price Scraper app):
    set_number, category, ebay_title, your_price, art_nr

Load into app:
    python main.py load catalog_new.csv
"""

import asyncio
import csv
import re
import sys
import logging
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page

try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Otto search URL — lego, sorted by bestsellers
# Price filtering (€10–€70) is applied in-script after extraction
# ─────────────────────────────────────────────────────────────────────────────

OTTO_LEGO_BASE = "https://www.otto.de/suche/lego/?sortierungMap=TOPSELLER"
OTTO_PRODUCTS_PER_PAGE = 48  # Otto search returns 48 results per page

PRICE_MIN = 10.0   # Filter: skip products cheaper than this
PRICE_MAX = 70.0   # Filter: skip products more expensive than this

HEADERS = {
    "Accept-Language": "de-DE,de;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OttoProduct:
    title: str
    price: float
    set_number: Optional[str] = None
    category: str = "LEGO Other"
    art_nr: Optional[str] = None
    url: str = ""
    description: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _human_delay(min_ms: int = 700, max_ms: int = 1800):
    import random
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _parse_german_price(text: str) -> Optional[float]:
    cleaned = re.sub(r'[€EUR\s]', '', text.strip())
    m = re.search(r'(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)', cleaned)
    if not m:
        return None
    raw = m.group(1).replace('.', '').replace(',', '.')
    try:
        v = float(raw)
        return v if v > 0.5 else None   # filter out stray "1" from ratings etc.
    except ValueError:
        return None


def _extract_set_number(title: str) -> Optional[str]:
    m = re.search(r'\((\d{4,6})\)', title)
    if m:
        return m.group(1)
    m = re.search(r'(?<!\d)(\d{4,6})(?!\d)', title)
    return m.group(1) if m else None


_CATEGORIES = [
    "Botanicals", "Speed Champions", "Technic", "Minecraft",
    "Harry Potter", "Marvel", "Creator", "DUPLO", "Classic",
    "City", "NINJAGO", "Star Wars", "Disney", "Jurassic",
    "Icons", "DREAMZzz", "ART", "BrickHeadz", "Friends",
    "Monkie Kid", "Avatar",
]

def _extract_category(title: str) -> str:
    tl = title.lower()
    for cat in _CATEGORIES:
        if cat.lower() in tl:
            return cat
    return "LEGO Other"


# ─────────────────────────────────────────────────────────────────────────────
# Cookie dismissal
# ─────────────────────────────────────────────────────────────────────────────

async def _accept_cookies(page: Page) -> None:
    for btn in [
        'button[data-testid="uc-accept-all-button"]',
        'button:has-text("Alle akzeptieren")',
        '#uc-btn-accept-banner',
        'button:has-text("Akzeptieren")',
        '[id*="accept"]',
    ]:
        try:
            await page.click(btn, timeout=4000)
            await _human_delay(500, 900)
            logger.info("Cookie banner dismissed.")
            return
        except Exception:
            continue
    logger.info("No cookie banner found (or already dismissed).")


# ─────────────────────────────────────────────────────────────────────────────
# Scroll to fully load lazy-rendered product grid
# ─────────────────────────────────────────────────────────────────────────────

async def _scroll_to_load(page: Page) -> None:
    """
    Otto lazy-loads product tiles as you scroll.
    Scroll in steps so all tiles are rendered before we extract them.
    """
    logger.info("Scrolling page to trigger lazy-load…")
    scroll_height = await page.evaluate("document.body.scrollHeight")
    step = 600
    pos = 0
    while pos < scroll_height:
        pos = min(pos + step, scroll_height)
        await page.evaluate(f"window.scrollTo(0, {pos})")
        await asyncio.sleep(0.3)
        scroll_height = await page.evaluate("document.body.scrollHeight")

    await page.evaluate("window.scrollTo(0, 0)")
    await _human_delay(800, 1200)


# ─────────────────────────────────────────────────────────────────────────────
# Debug dump
# ─────────────────────────────────────────────────────────────────────────────

async def _debug_dump(page: Page, page_num: int) -> None:
    stem = f"debug_page{page_num}"
    await page.screenshot(path=f"{stem}.png", full_page=True)
    Path(f"{stem}.html").write_text(await page.content(), encoding="utf-8")
    logger.info(f"  Debug files saved: {stem}.png  /  {stem}.html")
    logger.info("  Open the HTML in a browser to inspect what Otto rendered.")



# ─────────────────────────────────────────────────────────────────────────────
# Description scraper — visits each product page and extracts Beschreibung
# ─────────────────────────────────────────────────────────────────────────────

# Otto product pages put the description in various containers depending on
# the product type. We try selectors from most- to least-specific.
DESC_SELECTORS = [
    '.js_pdp_description__expander',    # Otto PDP description expander (confirmed)
    '[data-testid="product-description"]',
    '[class*="Description__Text"]',
    '[class*="ProductDescription"]',
    '[class*="Beschreibung"]',
    '[class*="description"]',
    '.find-section-beschreibung',
    '.find-product-description',
    '[id*="description"]',
    '[id*="beschreibung"]',
    # Generic fallback: largest <p> block on the page that looks like body text
]


async def _scrape_description(page: Page, url: str, idx: int, total: int) -> str:
    """Visit a product page and return its description text (empty string on failure)."""
    if not url:
        return ""

    logger.info(f"  [{idx}/{total}] Fetching description: {url[:80]}")
    try:
        await page.goto(url, wait_until="load", timeout=35_000)
    except Exception as e:
        logger.warning(f"  Description page load failed: {e}")
        return ""

    await _human_delay(400, 700)

    # The description is static HTML inside .js_pdp_description__expander.
    # Use inner_html() to get raw markup then strip tags in Python —
    # more reliable than innerText/textContent which depend on CSS state.
    try:
        html = await page.locator('.js_pdp_description__expander').first.inner_html(timeout=8000)
        if html:
            text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', '', text)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            if len(text) > 30:
                return text[:2000]
    except Exception:
        pass

    # Fallback selector loop using text_content() (CSS-independent)
    for sel in DESC_SELECTORS:
        try:
            el = page.locator(sel).first
            text = (await el.text_content(timeout=3000) or "").strip()
            if len(text) > 30:
                text = re.sub(r"\n{3,}", "\n\n", text)
                text = re.sub(r" {2,}", " ", text)
                return text[:2000]
        except Exception:
            continue

    # Last-resort: grab the longest <p> on the page
    try:
        paragraphs = await page.locator("p").all_inner_texts()
        candidates = [p.strip() for p in paragraphs if len(p.strip()) > 80]
        if candidates:
            return max(candidates, key=len)[:2000]
    except Exception:
        pass

    logger.debug("  No description found on product page.")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Single PDP scraper — fetch one product page directly by URL
# ─────────────────────────────────────────────────────────────────────────────

PDP_TITLE_SELECTORS = [
    'h1[data-testid="product-title"]',
    'h1[class*="Title"]',
    'h1[class*="title"]',
    'h1',
]

PDP_PRICE_SELECTORS = [
    '.js_pdp_price__retail-price__value_original',  # Otto PDP confirmed (e.g. "45,99 €")
    '[data-testid="product-price"]',
    '[class*="Price__value"]',
    '[class*="PriceValue"]',
    '[class*="Price__Value"]',
    '[class*="RegularPrice"]',
    '[class*="SalePrice"]',
    '[class*="price"]',
    'span[class*="Price"]',
]

PDP_ARTNR_SELECTORS = [
    # Otto typically renders the article number near the product header or in a
    # spec table.  We try several patterns in order.
    '[data-testid="article-number"]',
    '[class*="ArticleNumber"]',
    '[class*="article-number"]',
    '[class*="ArtNr"]',
    'span[class*="artnr" i]',
    # Fallback: look for a <dt>/<dd> or <li> that contains "Art.-Nr" text
]


async def _scrape_pdp(page: Page, url: str, accept_cookies: bool = False) -> Optional[OttoProduct]:
    """
    Navigate to a single Otto product detail page and return an OttoProduct.
    Returns None if the page cannot be parsed.
    """
    logger.info(f"[PDP] Fetching: {url}")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    except Exception as e:
        logger.warning(f"[PDP] Page load failed: {e}")
        return None

    await _human_delay(800, 1400)

    if accept_cookies:
        await _accept_cookies(page)

    # ── Title ──────────────────────────────────────────────────────────────────
    title = ""
    for sel in PDP_TITLE_SELECTORS:
        try:
            t = (await page.locator(sel).first.inner_text(timeout=3000)).strip()
            if len(t) > 5:
                title = t
                break
        except Exception:
            continue

    if not title:
        logger.warning("[PDP] Could not extract title — skipping.")
        return None

    # ── Price ──────────────────────────────────────────────────────────────────
    price_val: Optional[float] = None

    # Primary: confirmed Otto PDP price text selectors
    for sel in PDP_PRICE_SELECTORS:
        try:
            raw = (await page.locator(sel).first.inner_text(timeout=3000)).strip()
            if not raw:
                continue
            raw_clean = re.sub(r'(?i)^ab\s*', '', raw).split('UVP')[0].strip()
            price_val = _parse_german_price(raw_clean)
            if price_val:
                break
        except Exception:
            continue

    # Fallback: read data-price-cents attribute (price in euro-cents, e.g. "4599" = €45.99)
    if not price_val:
        try:
            cents = await page.locator('.js_pdp_price__tag[data-benefit-id="original"]').first.get_attribute(
                'data-price-cents', timeout=3000
            )
            if cents:
                price_val = int(cents) / 100
        except Exception:
            pass

    if not price_val:
        logger.warning(f"[PDP] Could not extract price for '{title[:60]}' — skipping.")
        return None

    # ── Article number ─────────────────────────────────────────────────────────
    art_nr: Optional[str] = None

    # Try dedicated selectors first
    for sel in PDP_ARTNR_SELECTORS:
        try:
            t = (await page.locator(sel).first.inner_text(timeout=2000)).strip()
            # Strip labels like "Art.-Nr.: 12345678"
            m = re.search(r'(\d{6,12})', t)
            if m:
                art_nr = m.group(1)
                break
        except Exception:
            continue

    # Fallback: scan <dt>/<dd> pairs for "Art" keyword
    if not art_nr:
        try:
            dts = await page.locator("dt").all_inner_texts()
            dds = await page.locator("dd").all_inner_texts()
            for dt, dd in zip(dts, dds):
                if "art" in dt.lower():
                    m = re.search(r'(\d{6,12})', dd)
                    if m:
                        art_nr = m.group(1)
                        break
        except Exception:
            pass

    # Fallback: scan visible text for "Art.-Nr" pattern
    if not art_nr:
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
            m = re.search(r'Art(?:ikel)?[.\-\s]*Nr[.\s:]*\s*(\d{6,12})', body_text, re.IGNORECASE)
            if m:
                art_nr = m.group(1)
        except Exception:
            pass

    # ── Description ────────────────────────────────────────────────────────────
    description = ""
    try:
        html = await page.locator('.js_pdp_description__expander').first.inner_html(timeout=8000)
        if html:
            text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', '', text)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            if len(text) > 30:
                description = text[:2000]
    except Exception:
        pass

    if not description:
        for sel in DESC_SELECTORS:
            try:
                el = page.locator(sel).first
                text = (await el.text_content(timeout=3000) or "").strip()
                if len(text) > 30:
                    text = re.sub(r"\n{3,}", "\n\n", text)
                    text = re.sub(r" {2,}", " ", text)
                    description = text[:2000]
                    break
            except Exception:
                continue

    if not description:
        try:
            paragraphs = await page.locator("p").all_inner_texts()
            candidates = [p.strip() for p in paragraphs if len(p.strip()) > 80]
            if candidates:
                description = max(candidates, key=len)[:2000]
        except Exception:
            pass

    set_number = _extract_set_number(title)
    category   = _extract_category(title)

    logger.info(
        f"[PDP] ✓ [{category}] {title[:65]}  →  €{price_val:.2f}"
        + (f"  art_nr={art_nr}" if art_nr else "  (no art_nr found)")
    )

    return OttoProduct(
        title=title,
        price=price_val,
        set_number=set_number,
        category=category,
        art_nr=art_nr,
        url=url,
        description=description,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tile / title / price selectors
# ─────────────────────────────────────────────────────────────────────────────

TILE_SELECTORS = [
    'article.reptile-tile-item',            # Otto confirmed (reptile search grid)
    '[data-qa="reptile-product-tile"]',     # Otto confirmed (data-qa variant)
    '[data-testid="product-list-item"]',
    '[class*="ProductTile"]',
    '[class*="Tile__Root"]',
    '[class*="product-tile"]',
    '[class*="ProductCard"]',
    '[class*="product-card"]',
    'li[class*="find-product-tile"]',
    '.find-product-tile',
    'li[class*="product"]',
    'article',
]

TITLE_SELECTORS = [
    '.reptile-product-title__title',        # Otto confirmed (title span inside link)
    '[data-testid="product-title"]',
    '[class*="Title__Text"]',
    '[class*="ProductTitle"]',
    '[class*="title"]',
    'h2', 'h3', 'h4',
]

PRICE_SELECTORS = [
    '.reptile-price__retailPrice',          # Otto confirmed (current/sale price)
    '[data-testid="product-price"]',
    '[class*="Price__value"]',
    '[class*="PriceValue"]',
    '[class*="Price__Value"]',
    '[class*="RegularPrice"]',
    '[class*="SalePrice"]',
    '.find-offer-price__normal-price',
    '[class*="price"]',
    'span[class*="Price"]',
]


async def _find_tile_selector(page: Page) -> Optional[str]:
    """Return the first tile selector that finds 2+ elements."""
    for sel in TILE_SELECTORS:
        try:
            count = await page.locator(sel).count()
            if count >= 2:
                logger.info(f"Tile selector matched ({count} tiles): {sel!r}")
                return sel
        except Exception:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Single-page scraper
# ─────────────────────────────────────────────────────────────────────────────

async def _scrape_page(
    page: Page, url: str, page_num: int, debug: bool = False
) -> list[OttoProduct]:
    products: list[OttoProduct] = []
    logger.info(f"[Otto] Page {page_num}: {url}")

    try:
        await page.goto(url, wait_until="networkidle", timeout=35_000)
    except Exception:
        logger.warning("networkidle timed out — falling back to domcontentloaded")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=35_000)
        except Exception as e:
            logger.error(f"Failed to load page: {e}")
            return products

    await _human_delay(1200, 2000)

    if page_num == 1:
        await _accept_cookies(page)

    await _scroll_to_load(page)

    if debug:
        await _debug_dump(page, page_num)

    tile_sel = await _find_tile_selector(page)
    if not tile_sel:
        logger.warning(
            f"[Otto] No tile selector matched on page {page_num}. "
            "Run with --debug --headful to inspect."
        )
        return products

    tiles = page.locator(tile_sel)
    total = await tiles.count()
    logger.info(f"[Otto] {total} tiles on page {page_num}")

    for i in range(total):
        tile = tiles.nth(i)

        # Title
        title = ""
        for sel in TITLE_SELECTORS:
            try:
                t = (await tile.locator(sel).first.inner_text(timeout=1500)).strip()
                if len(t) > 10:
                    title = t
                    break
            except Exception:
                continue
        if not title:
            continue

        # Price
        price_val = None
        for sel in PRICE_SELECTORS:
            try:
                raw = (await tile.locator(sel).first.inner_text(timeout=1500)).strip()
                if not raw:
                    continue
                raw_clean = re.sub(r'(?i)^ab\s*', '', raw).split('UVP')[0].strip()
                price_val = _parse_german_price(raw_clean)
                if price_val:
                    break
            except Exception:
                continue
        if not price_val:
            logger.debug(f"  Tile {i}: '{title[:50]}' — no price, skipping")
            continue

        # Apply price range filter
        if not (PRICE_MIN <= price_val <= PRICE_MAX):
            logger.debug(f"  Tile {i}: '{title[:50]}' — €{price_val:.2f} outside range, skipping")
            continue

        # URL
        product_url = ""
        try:
            href = await tile.locator("a").first.get_attribute("href")
            if href:
                product_url = href if href.startswith("http") else f"https://www.otto.de{href}"
        except Exception:
            pass

        # Art-Nr — available directly on the tile element, no PDP visit needed
        art_nr = None
        try:
            art_nr = await tile.get_attribute("data-article-number")
        except Exception:
            pass

        set_number = _extract_set_number(title)
        category   = _extract_category(title)
        logger.info(f"  ✓ [{category}] {title[:65]}  →  €{price_val:.2f}" + (f"  art_nr={art_nr}" if art_nr else ""))

        products.append(OttoProduct(
            title=title, price=price_val,
            set_number=set_number, category=category, url=product_url,
            art_nr=art_nr,
        ))

    logger.info(
        f"[Otto] Page {page_num}: {len(products)} extracted, "
        f"{total - len(products)} skipped"
    )
    return products


def _page_url(n: int) -> str:
    # Otto search pagination uses &o= as an offset (0-based, 48 per page)
    if n == 1:
        return OTTO_LEGO_BASE
    offset = (n - 1) * OTTO_PRODUCTS_PER_PAGE
    return f"{OTTO_LEGO_BASE}&o={offset}"


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

async def build_catalog(
    pages: int = 5,
    headless: bool = True,
    proxy: Optional[dict] = None,
    out_path: str = "catalog_new.csv",
    debug: bool = False,
    fetch_descriptions: bool = True,
    pdp_urls: Optional[list[str]] = None,
) -> list[OttoProduct]:

    all_products: list[OttoProduct] = []
    seen: set[str] = set()

    async with async_playwright() as pw:
        launch_opts: dict = {
            "headless": headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ],
        }
        if proxy:
            launch_opts["proxy"] = proxy

        browser = await pw.chromium.launch(**launch_opts)
        context = await browser.new_context(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            extra_http_headers=HEADERS,
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en'] });
            window.chrome = { runtime: {} };
        """)

        page = await context.new_page()

        if STEALTH_AVAILABLE:
            await stealth_async(page)
            logger.info("playwright-stealth applied.")
        else:
            logger.warning(
                "playwright-stealth not installed. Run: pip install playwright-stealth"
            )

        # ── PDP-only mode: fetch individual product URLs directly ────────────────
        if pdp_urls:
            logger.info(f"\nPDP mode: fetching {len(pdp_urls)} product URL(s)…")
            for i, url in enumerate(pdp_urls):
                product = await _scrape_pdp(page, url.strip(), accept_cookies=(i == 0))
                if product:
                    all_products.append(product)
                await _human_delay(800, 1500)

            await browser.close()

            _write_catalog(all_products, out_path)
            return all_products

        # ── Normal search-scrape mode ────────────────────────────────────────────
        for page_num in range(1, pages + 1):
            found = await _scrape_page(page, _page_url(page_num), page_num, debug=debug)

            if not found:
                logger.info(f"Page {page_num} returned nothing — stopping.")
                break

            for p in found:
                key = p.title.lower().strip()
                if key not in seen:
                    seen.add(key)
                    all_products.append(p)

            await _human_delay(1200, 2500)

        # ── Fetch descriptions (unless skipped) ──────────────────────────────────
        if fetch_descriptions and all_products:
            logger.info(f"\nFetching descriptions for {len(all_products)} products…")
            desc_page = await context.new_page()
            if STEALTH_AVAILABLE:
                await stealth_async(desc_page)
            for i, product in enumerate(all_products, 1):
                product.description = await _scrape_description(
                    desc_page, product.url, i, len(all_products)
                )
                await _human_delay(400, 900)
            await desc_page.close()

        await browser.close()

    logger.info(f"\nTotal unique products: {len(all_products)}")

    _write_catalog(all_products, out_path)
    return all_products


def _write_catalog(products: list[OttoProduct], out_path: str) -> None:
    out = Path(out_path)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["set_number", "category", "ebay_title", "your_price", "art_nr", "description"]
        )
        writer.writeheader()
        for p in products:
            writer.writerow({
                "set_number": p.set_number or "",
                "category":   p.category,
                "ebay_title": p.title,
                "your_price": f"{p.price:.2f}",
                "art_nr":     p.art_nr or "",
                "description": p.description,
            })
    logger.info(f"Catalog saved → {out.resolve()}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Rebuild catalog by scraping popular LEGO sets from Otto.de"
    )
    p.add_argument("--pages",   type=int, default=5)
    p.add_argument("--out",     default="catalog_new.csv")
    p.add_argument("--headful", action="store_true", help="Show browser window")
    p.add_argument("--debug",   action="store_true", help="Save screenshot + HTML per page")
    p.add_argument("--proxy",   default=None)
    p.add_argument("--no-descriptions", action="store_true",
                   help="Skip fetching product descriptions (faster)")
    p.add_argument(
        "--url", dest="urls", action="append", metavar="URL",
        help="Fetch a single Otto product page (PDP) directly. Repeat for multiple URLs. "
             "Skips the search scrape entirely.",
    )
    args = p.parse_args()

    proxy_dict = {"server": args.proxy} if args.proxy else None

    products = asyncio.run(build_catalog(
        pages=args.pages,
        headless=not args.headful,
        proxy=proxy_dict,
        out_path=args.out,
        debug=args.debug,
        fetch_descriptions=not args.no_descriptions,
        pdp_urls=args.urls or [],
    ))

    if products:
        print(f"\n✅  {len(products)} products written to '{args.out}'")
        print(f"   Load: python main.py load {args.out}")
    else:
        print("\n❌  No products found.")
        print("   Diagnose with:  python catalog_builder.py --headful --debug --pages 1")
        print("   Then open debug_page1.html in a browser to see what Otto rendered.")
        sys.exit(1)
