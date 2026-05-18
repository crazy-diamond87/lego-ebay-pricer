"""
prepare_and_list.py — Safely load new products and list them on eBay

This script handles the full flow in the correct order:

  Step 1: Load catalog_loaded.csv → marks existing products as already listed
  Step 2: Clean + load catalog_new.csv → only inserts genuinely new products
  Step 3: (Optional) Push new products to eBay via API

Usage:
    # See what would happen — no database or eBay changes
    python prepare_and_list.py --dry-run

    # Run steps 1 + 2 only (build the DB, don't list yet)
    python prepare_and_list.py --load-only

    # Full run: load + list new products on eBay
    python prepare_and_list.py

    # Full run but only list N products (safe for testing)
    python prepare_and_list.py --limit 3

    # Skip CSV loading — list straight from whatever is already in the DB
    python prepare_and_list.py --from-db
    python prepare_and_list.py --from-db --limit 5
    python prepare_and_list.py --from-db --set 75368
    python prepare_and_list.py --from-db --dry-run

Credentials:
    Copy .env.example to .env and fill in your eBay API credentials.
    Never commit your .env file.
"""

import argparse
import csv
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

from database import get_conn, init_db, upsert_products

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — loaded from environment variables (set in .env)
# ─────────────────────────────────────────────────────────────────────────────

def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Missing required environment variable: {key}\n"
            f"Copy .env.example to .env and fill in your credentials."
        )
    return value


CONFIG = {
    "app_id":        _require_env("EBAY_APP_ID"),
    "dev_id":        _require_env("EBAY_DEV_ID"),
    "cert_id":       _require_env("EBAY_CERT_ID"),
    "user_token":    _require_env("EBAY_USER_TOKEN"),
    "postal_code":   os.getenv("EBAY_POSTAL_CODE", "10115"),
    "site_id":       os.getenv("EBAY_SITE_ID", "77"),          # 77 = eBay.de
    "dispatch_time": int(os.getenv("EBAY_DISPATCH_TIME", "1")),
    "quantity":      int(os.getenv("EBAY_QUANTITY", "10")),
}

TRADING_API_URL = "https://api.ebay.com/ws/api.dll"

# Paths — adjust if your CSVs are elsewhere
EXISTING_CATALOG = Path("catalog_loaded.csv")
NEW_CATALOG      = Path("catalog_new.csv")


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — DB setup
# ─────────────────────────────────────────────────────────────────────────────

def init_listings_table() -> None:
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ebay_listings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id   INTEGER NOT NULL REFERENCES products(id),
            ebay_item_id TEXT,
            status       TEXT NOT NULL,
            error_msg    TEXT,
            listed_at    TEXT DEFAULT (datetime('now')),
            price_listed REAL
        );
        CREATE INDEX IF NOT EXISTS idx_listings_product
            ON ebay_listings(product_id);
        """)


def get_listed_product_ids() -> set[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT product_id FROM ebay_listings WHERE status = 'listed'"
        ).fetchall()
    return {r[0] for r in rows}


def save_listing(product_id, ebay_item_id, status, error_msg, price):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO ebay_listings
                (product_id, ebay_item_id, status, error_msg, price_listed)
            VALUES (?, ?, ?, ?, ?)
        """, (product_id, ebay_item_id, status, error_msg, price))


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — CSV loading
# ─────────────────────────────────────────────────────────────────────────────

def clean_title(title: str) -> str:
    """Remove newlines and extra whitespace from scraped titles."""
    title = title.replace("\n", " ").replace("\r", " ")
    title = re.sub(r"\s+", " ", title).strip()
    return title[:80]  # eBay max title length


def clean_set_number(raw: str | None) -> str | None:
    """Normalise set numbers to clean integers — strips float suffix from CSV exports.

    CSV files generated from pandas or Excel store integer-valued float columns
    as "21349.0" rather than "21349". This function normalises all such values at
    load time so that every downstream consumer (XML builder, scraper, image lookup)
    sees a clean digit string.

    Examples:
        "21349.0" → "21349"
        "75368"   → "75368"
        ""        → None
        None      → None
        "abc"     → None   (non-numeric values are rejected)
    """
    if not raw:
        return None
    cleaned = str(raw).split(".")[0].strip()
    return cleaned if cleaned.isdigit() else None


# ─────────────────────────────────────────────────────────────────────────────
# Pricing — tier-based markup + X.99 rounding
# ─────────────────────────────────────────────────────────────────────────────

# Markup tiers (based on cost/input price).
# Format: (lower_bound, upper_bound_inclusive, markup_euros)
# Tiers 95+ extrapolate the +1€-per-€10 pattern.
PRICE_TIERS = [
    (0,    6.99,  4),
    (7,   14.99,  5),
    (15,  24.99,  6),
    (25,  34.99,  7),
    (35,  44.99,  8),
    (45,  54.99,  9),
    (55,  64.99, 10),
    (65,  74.99, 11),
    (75,  84.99, 12),
    (85,  94.99, 13),
    # ── Extrapolated (pattern: +1 per €10 band) ────────────────────────────
    (95,  104.99, 14),
    (105, 114.99, 15),
    (115, 124.99, 16),
    (125, 134.99, 17),
    (135, 144.99, 18),
    (145, 154.99, 19),
    (155, 164.99, 20),
    (165, 174.99, 21),
    (175, 184.99, 22),
    (185, 194.99, 23),
    (195, 204.99, 24),
    (205, 214.99, 25),
]


def apply_price_markup(cost_price: float) -> float:
    """Apply tier-based markup to a cost price and round UP to the nearest X.99.

    Rounding rule: ceil(cost + markup + 0.01) - 0.01
      -> always rounds UP to the nearest .99 ending, never down.

    Examples:
        6.79  (+4)  ->  10.79  ->  10.99
        14.00 (+5)  ->  19.00  ->  19.99
        25.00 (+7)  ->  32.00  ->  32.99
        36.98 (+8)  ->  44.98  ->  44.99
       107.99 (+15) -> 122.99  -> 122.99   (already .99 -- unchanged)
       206.49 (+25) -> 231.49  -> 231.99
    """
    import math

    markup = None
    for low, high, m in PRICE_TIERS:
        if low <= cost_price <= high:
            markup = m
            break

    if markup is None:
        # Safety fallback for any price above the last tier -- continues pattern
        markup = 13 + math.ceil((cost_price - 94.99) / 10)
        logger.warning(
            f"  Price {cost_price:.2f} exceeds all defined tiers -- "
            f"extrapolated markup: +{markup}"
        )

    # Round intermediate sum to 2dp first to avoid floating-point drift
    selling_price = round(cost_price + markup, 2)
    # Round UP to nearest .99: add 0.01, ceil to next integer, subtract 0.01
    return round(math.ceil(selling_price + 0.01) - 0.01, 2)


def format_ebay_title(product: dict) -> str:
    """Build a standardised eBay listing title:

        LEGO {set_number} {series} {name} NEU & OVP

    Handles two source formats found in the catalogs:
      catalog_new  (OTTO) : 'LEGO®\\nName (set), LEGO Series Konstruktionsspielsteine, ...'
      catalog_loaded (eBay): 'LEGO Name (set), LEGO Series, (n St) NEU & OVP'
                             'LEGO Series: Name (set) n St - NEU & OVP'

    Always within eBay's 80-character title limit.
    """
    raw        = product.get("ebay_title", "")
    set_number = str(product.get("set_number") or "").strip()

    # Normalise: strip brand marks and collapse whitespace
    text = raw.replace("®", "").replace("™", "")
    text = re.sub(r"[\n\r]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Remove single leading "LEGO " so the set_number split works cleanly
    text = re.sub(r"^LEGO\s+", "", text, count=1).strip()

    # Split on (set_number) to isolate name half / series half
    if set_number:
        halves = re.split(rf"\s*\({re.escape(set_number)}\)\s*", text, maxsplit=1)
    else:
        halves = re.split(r"\s*\(\d{4,6}\)\s*", text, maxsplit=1)

    before = halves[0].strip().rstrip(",").strip()
    after  = halves[1].strip() if len(halves) > 1 else ""

    # ── Extract series and name from the "before" half ────────────────────────
    if ":" in before:
        colon  = before.index(":")
        series = before[:colon].strip()
        name   = before[colon + 1:].strip().lstrip("–- ").strip()
    else:
        name   = before
        series = ""

    # ── Extract series from the "after" half if not already found ─────────────
    if not series and after:
        m = re.search(
            r",?\s*LEGO\s+(?:LEGO\s+)?(.+?)\s+Konstruktionsspielsteine",
            after
        )
        if m:
            series = m.group(1).strip()
        else:
            m2 = re.search(r",?\s*LEGO\s+(.+?)(?:\s*,|\s+NEU|\s*$)", after)
            if m2:
                candidate = m2.group(1).strip()
                candidate = re.sub(r"\s*\(?\d+\s*St\)?$", "", candidate).strip()
                if candidate:
                    series = candidate

    # ── Assemble and enforce eBay 80-char limit ───────────────────────────────
    prefix = f"LEGO {set_number}".strip() if set_number else "LEGO"
    if series:
        prefix += f" {series}"
    suffix = "NEU & OVP"
    title  = f"{prefix} {name} {suffix}"

    if len(title) > 80:
        max_name = 80 - len(prefix) - 1 - 1 - len(suffix)
        title = f"{prefix} {name[:max(max_name, 5)].rstrip()} {suffix}" if max_name >= 5 else title[:80]

    return title


def load_csv(path: Path) -> list[dict]:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = clean_title(row.get("ebay_title", ""))
            price_str = row.get("your_price", "").strip()
            if not title or not price_str:
                continue
            try:
                price = float(price_str)
            except ValueError:
                continue
            if price <= 0:
                continue
            rows.append({
                "ebay_title": title,
                "art_nr":     row.get("art_nr", "").strip() or None,
                "set_number": clean_set_number(row.get("set_number", "")),
                "category":   row.get("category", "").strip() or "LEGO Other",
                "your_price": price,
            })
    return rows


def get_existing_titles() -> set[str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT ebay_title FROM products").fetchall()
    return {r[0] for r in rows}


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — eBay listing
# ─────────────────────────────────────────────────────────────────────────────

# eBay.de category IDs (verified)
CATEGORY_MAP = {
    "Star Wars":       "19006",
    "Technic":         "19006",
    "City":            "19006",
    "Harry Potter":    "19006",
    "Marvel":          "19006",
    "Minecraft":       "19006",
    "Creator":         "19006",
    "DUPLO":           "19001",
    "Classic":         "19006",
    "NINJAGO":         "19006",
    "Disney":          "19006",
    "Jurassic":        "19006",
    "Icons":           "19006",
    "Botanicals":      "19006",
    "Speed Champions": "19006",
    "Friends":         "19005",
    "DREAMZzz":        "19006",
    "BrickHeadz":      "19006",
    "ART":             "19006",
    "LEGO Other":      "19006",
    "Other":           "19006",
}

# Categories where ConditionID is not accepted — omit it for these
NO_CONDITION_CATEGORIES = {"19001"}  # DUPLO and similar

# Images folder — adjust path if yours is different
IMAGES_DIR = Path("images")


def upload_image(image_path: Path) -> str | None:
    """
    Upload a local image to eBay Picture Services (EPS).
    eBay requires a specific multipart format:
      Part 1: name="XML Payload"  — the XML request
      Part 2: name="image"        — the image bytes
    The Content-Type header must NOT be set manually — requests sets it
    automatically with the correct boundary when using files=.
    """
    if not image_path.exists():
        return None

    xml_request = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f'<RequesterCredentials><eBayAuthToken>{CONFIG["user_token"]}</eBayAuthToken></RequesterCredentials>'
        f'<PictureName>{image_path.stem}</PictureName>'
        '<PictureSet>Standard</PictureSet>'
        '</UploadSiteHostedPicturesRequest>'
    )

    headers = {
        "X-EBAY-API-SITEID":              CONFIG["site_id"],
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1119",
        "X-EBAY-API-CALL-NAME":           "UploadSiteHostedPictures",
        "X-EBAY-API-APP-NAME":            CONFIG["app_id"],
        "X-EBAY-API-DEV-NAME":            CONFIG["dev_id"],
        "X-EBAY-API-CERT-NAME":           CONFIG["cert_id"],
    }

    try:
        with open(image_path, "rb") as img_file:
            image_data = img_file.read()

        files = [
            ("XML Payload", ("payload.xml", xml_request.encode("utf-8"), "text/xml")),
            ("image",       (image_path.name, image_data, "image/jpeg")),
        ]
        resp = requests.post(
            "https://api.ebay.com/ws/api.dll",
            headers=headers,
            files=files,
            timeout=60,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
        ack = root.findtext("e:Ack", namespaces=ns) or ""
        if ack in ("Success", "Warning"):
            url = root.findtext(
                "e:SiteHostedPictureDetails/e:FullURL", namespaces=ns
            )
            if url:
                logger.info(f"  Image uploaded: {image_path.name} → {url[:60]}…")
                return url
        errors = [
            e.findtext("e:LongMessage", namespaces=ns) or ""
            for e in root.findall("e:Errors", namespaces=ns)
        ]
        logger.warning(f"  Image upload failed for {image_path.name}: {'; '.join(errors)}")
        return None
    except Exception as e:
        logger.error(f"  Image upload error ({image_path.name}): {e}")
        return None


def get_image_urls(set_number: str) -> list[str]:
    """
    Find local images for a set and upload them to eBay Picture Services.
    Looks for: images/{set_number}_1.jpg, _2.jpg, … (up to 12, eBay's limit).
    Falls back to images/{set_number}.jpg (legacy single-image naming).
    Returns a list of hosted eBay picture URLs.
    """
    if not set_number:
        return []

    urls = []

    # Multi-image naming: {set_number}_1.jpg, _2.jpg, ...
    for idx in range(1, 13):
        img_path = IMAGES_DIR / f"{set_number}_{idx}.jpg"
        if not img_path.exists():
            break
        url = upload_image(img_path)
        if url:
            urls.append(url)

    # Legacy single-image fallback
    if not urls:
        img_path = IMAGES_DIR / f"{set_number}.jpg"
        if img_path.exists():
            logger.info(f"  Legacy image found -- uploading: {img_path.name}")
            url = upload_image(img_path)
            if url:
                return [url]

    logger.warning(f"  No image files found for set {set_number}")
    return urls


def build_xml(product: dict) -> str:
    title      = _escape(format_ebay_title(product))
    price      = apply_price_markup(product["your_price"])
    category   = CATEGORY_MAP.get(product.get("category", ""), "19006")
    set_number = product.get("set_number") or ""
    art_nr     = product.get("art_nr") or ""
    set_spec   = (f"<NameValueList><Name>Set-Nummer</Name>"
                  f"<Value>{set_number}</Value></NameValueList>"
                  if set_number else "")
    ean_spec   = (f"<NameValueList><Name>EAN</Name>"
                  f"<Value>{art_nr}</Value></NameValueList>"
                  if art_nr else "")
    clean_t    = format_ebay_title(product)

    # Upload all images and build <PictureDetails> block (eBay max: 12)
    image_urls = get_image_urls(set_number)
    picture_block = ""
    if image_urls:
        url_tags = "\n      ".join(f"<PictureURL>{u}</PictureURL>" for u in image_urls)
        picture_block = f"""
    <PictureDetails>
      {url_tags}
    </PictureDetails>"""

    # Only include ConditionID for categories that support it
    condition_block = ""
    if category not in NO_CONDITION_CATEGORIES:
        condition_block = "<ConditionID>1000</ConditionID>"

    set_nr_block = f"<p><strong>Set-Nummer:</strong> {set_number}</p>" if set_number else ""
    db_description = product.get("description") or ""
    description_body = (
        f'<p>{db_description}</p>' if db_description
        else f'<p>Original verpackte Neuware direkt aus dem Handel.</p>'
    )
    description = (
        f'<div style="font-family:Arial,sans-serif;max-width:700px">'
        f'<h2>{clean_t}</h2>'
        f'{set_nr_block}'
        f'<p><strong>Kategorie:</strong> {product.get("category", "")}</p>'
        f'<hr/>'
        f'{description_body}'
        f'<p>Privatverkauf. Dieser Verkauf erfolgt unter Ausschluss jeglicher Gewährleistung. Ankauf und Versand über den Großhändler.</p>'
        f'<p>Geburtstagsgeschenk Weihnachtsgeschenk Valentinstag Geschenk Muttertag Kindertag Ostergeschenk Ostern Frauentag</p>'
        f'</div>'
    )

    return f"""<?xml version="1.0" encoding="utf-8"?>
<AddFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{CONFIG['user_token']}</eBayAuthToken>
  </RequesterCredentials>
  <ErrorLanguage>en_US</ErrorLanguage>
  <WarningLevel>High</WarningLevel>
  <Item>
    <Title>{title}</Title>
    <Description><![CDATA[{description}]]></Description>
    <PrimaryCategory><CategoryID>{category}</CategoryID></PrimaryCategory>
    <StartPrice>{price:.2f}</StartPrice>
    <CategoryMappingAllowed>true</CategoryMappingAllowed>
    <Country>DE</Country>
    <Currency>EUR</Currency>
    <DispatchTimeMax>{CONFIG['dispatch_time']}</DispatchTimeMax>
    <ListingDuration>GTC</ListingDuration>
    <ListingType>FixedPriceItem</ListingType>
    <PostalCode>{CONFIG['postal_code']}</PostalCode>
    <Quantity>{CONFIG['quantity']}</Quantity>
    <Site>Germany</Site>
    {condition_block}
    {picture_block}
    <ReturnPolicy>
      <ReturnsAcceptedOption>ReturnsNotAccepted</ReturnsAcceptedOption>
    </ReturnPolicy>
    <ShippingDetails>
      <ShippingType>Flat</ShippingType>
      <ShippingServiceOptions>
        <ShippingServicePriority>1</ShippingServicePriority>
        <ShippingService>DE_DHLPaket</ShippingService>
        <ShippingServiceCost>0.00</ShippingServiceCost>
        <FreeShipping>true</FreeShipping>
      </ShippingServiceOptions>
    </ShippingDetails>
    <ItemSpecifics>
      <NameValueList><Name>Marke</Name><Value>LEGO</Value></NameValueList>
      {set_spec}
      {ean_spec}
      <NameValueList><Name>Zustand</Name><Value>Neu</Value></NameValueList>
    </ItemSpecifics>
  </Item>
</AddFixedPriceItemRequest>"""


def _escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def call_api(xml_body: str, debug: bool = False) -> ET.Element:
    headers = {
        "X-EBAY-API-SITEID": CONFIG["site_id"],
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1119",
        "X-EBAY-API-CALL-NAME": "AddFixedPriceItem",
        "X-EBAY-API-APP-NAME": CONFIG["app_id"],
        "X-EBAY-API-DEV-NAME": CONFIG["dev_id"],
        "X-EBAY-API-CERT-NAME": CONFIG["cert_id"],
        "Content-Type": "text/xml; charset=utf-8",
    }
    if debug:
        logger.debug("── REQUEST XML ──────────────────────────────────────────")
        logger.debug(xml_body)
    resp = requests.post(TRADING_API_URL, data=xml_body.encode("utf-8"),
                         headers=headers, timeout=30)
    resp.raise_for_status()
    if debug:
        logger.debug("── RAW RESPONSE ─────────────────────────────────────────")
        logger.debug(resp.text)
    return ET.fromstring(resp.content)


def list_product(product: dict, dry_run: bool, debug: bool = False) -> tuple[bool, str, str | None]:
    if dry_run:
        if debug:
            logger.debug("── DRY-RUN XML ──────────────────────────────────────────")
            logger.debug(build_xml(product))
        return True, "DRY_RUN", None
    try:
        xml = build_xml(product)
        root = call_api(xml, debug=debug)
        ns   = {"e": "urn:ebay:apis:eBLBaseComponents"}
        ack  = root.findtext("e:Ack", namespaces=ns) or ""
        item_id = root.findtext("e:ItemID", namespaces=ns) or ""
        errors = [
            f"[{e.findtext('e:SeverityCode', namespaces=ns)}] "
            f"{e.findtext('e:LongMessage', namespaces=ns) or ''}"
            for e in root.findall("e:Errors", namespaces=ns)
        ]
        if ack in ("Success", "Warning"):
            return True, item_id, ("; ".join(errors) if errors else None)
        return False, "", "; ".join(errors) or "Unknown error"
    except Exception as e:
        return False, "", str(e)


# ─────────────────────────────────────────────────────────────────────────────
# eBay sync
# ─────────────────────────────────────────────────────────────────────────────

def fetch_active_ebay_item_ids() -> set[str]:
    from datetime import datetime, timezone, timedelta
    now      = datetime.now(timezone.utc)
    end_from = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_to   = (now + timedelta(days=119)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    item_ids = set()
    page     = 1
    while True:
        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<GetSellerListRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials><eBayAuthToken>{CONFIG['user_token']}</eBayAuthToken></RequesterCredentials>
  <DetailLevel>ReturnAll</DetailLevel>
  <EndTimeFrom>{end_from}</EndTimeFrom>
  <EndTimeTo>{end_to}</EndTimeTo>
  <Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>
  <OnlyActiveItems>true</OnlyActiveItems>
</GetSellerListRequest>"""
        headers = {
            "X-EBAY-API-SITEID": CONFIG["site_id"],
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1119",
            "X-EBAY-API-CALL-NAME": "GetSellerList",
            "X-EBAY-API-APP-NAME": CONFIG["app_id"],
            "X-EBAY-API-DEV-NAME": CONFIG["dev_id"],
            "X-EBAY-API-CERT-NAME": CONFIG["cert_id"],
            "Content-Type": "text/xml; charset=utf-8",
        }
        resp = requests.post(TRADING_API_URL, data=xml.encode("utf-8"),
                             headers=headers, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns   = {"e": "urn:ebay:apis:eBLBaseComponents"}
        ack  = root.findtext("e:Ack", namespaces=ns) or ""
        if ack not in ("Success", "Warning"):
            errors = [e.findtext("e:LongMessage", namespaces=ns) or ""
                      for e in root.findall("e:Errors", namespaces=ns)]
            raise RuntimeError(f"GetSellerList failed: {'; '.join(errors)}")
        for item in root.findall(".//e:Item", namespaces=ns):
            item_id = item.findtext("e:ItemID", namespaces=ns)
            if item_id:
                item_ids.add(item_id)
        total_pages = int(root.findtext(
            "e:PaginationResult/e:TotalNumberOfPages", namespaces=ns) or "1")
        if page >= total_pages:
            break
        page += 1
    return item_ids


def sync_with_ebay() -> None:
    init_db()
    init_listings_table()
    logger.info(f"{'─'*60}")
    logger.info("SYNC: Fetching active listings from eBay...")
    try:
        live_ids = fetch_active_ebay_item_ids()
    except Exception as e:
        logger.error(f"  Failed: {e}")
        return
    logger.info(f"  {len(live_ids)} active listings on eBay")
    with get_conn() as conn:
        db_listings = conn.execute("""
            SELECT l.id, l.ebay_item_id, p.ebay_title
            FROM ebay_listings l JOIN products p ON p.id = l.product_id
            WHERE l.status = 'listed'
            AND l.ebay_item_id != 'EXISTING'
            AND l.ebay_item_id IS NOT NULL
        """).fetchall()
    ended = [r for r in db_listings if r["ebay_item_id"] not in live_ids]
    if not ended:
        logger.info("  ✅ All DB listings are live — nothing to sync")
        return
    logger.info(f"  {len(ended)} listing(s) deleted on eBay → marking as 'ended':")
    with get_conn() as conn:
        for row in ended:
            logger.info(f"    → [{row['ebay_item_id']}] {row['ebay_title'][:60]}")
            conn.execute("UPDATE ebay_listings SET status = 'ended' WHERE id = ?", (row["id"],))
    logger.info(f"Done! {len(ended)} product(s) will be re-listed on next run.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def run(dry_run=False, load_only=False, limit=0, set_number=None, from_db=False, debug=False):
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug mode enabled — request XML and raw responses will be logged")
    init_db()
    init_listings_table()

    if from_db:
        # ── DB-direct mode: skip CSV loading entirely ─────────────────────────
        logger.info(f"{'─'*60}")
        logger.info("--from-db: Skipping CSV load — listing directly from database")
    else:
        # ── Step 1: Load existing catalog → mark as already listed ───────────────
        logger.info(f"{'─'*60}")
        logger.info("STEP 1: Loading existing catalog (catalog_loaded.csv)")

        if not EXISTING_CATALOG.exists():
            logger.warning(f"  {EXISTING_CATALOG} not found — skipping")
        else:
            existing_rows = load_csv(EXISTING_CATALOG)
            if not dry_run:
                inserted = upsert_products(existing_rows)
                logger.info(f"  Inserted {inserted} products from existing catalog")
            else:
                logger.info(f"  [DRY RUN] Would insert up to {len(existing_rows)} existing products")

            # Mark ONLY the existing catalog products as already listed
            if not dry_run:
                existing_titles_set = {r["ebay_title"] for r in existing_rows}
                with get_conn() as conn:
                    already_listed = get_listed_product_ids()
                    marked = 0
                    for title in existing_titles_set:
                        row = conn.execute(
                            "SELECT id, your_price FROM products WHERE ebay_title = ?", (title,)
                        ).fetchone()
                        if row and row[0] not in already_listed:
                            conn.execute("""
                                INSERT INTO ebay_listings
                                    (product_id, ebay_item_id, status, price_listed)
                                VALUES (?, 'EXISTING', 'listed', ?)
                            """, (row[0], row[1]))
                            marked += 1
                    logger.info(f"  Marked {marked} existing products as already listed — lister will skip them")

        # ── Step 2: Load new catalog → only truly new products inserted ──────────
        logger.info(f"{'─'*60}")
        logger.info("STEP 2: Loading new catalog (catalog_new.csv)")

        if not NEW_CATALOG.exists():
            logger.error(f"  {NEW_CATALOG} not found!")
            return

        new_rows = load_csv(NEW_CATALOG)
        logger.info(f"  {len(new_rows)} products parsed from catalog_new.csv")

        existing_titles = get_existing_titles()
        dupes = [r for r in new_rows if r["ebay_title"] in existing_titles]
        genuinely_new = [r for r in new_rows if r["ebay_title"] not in existing_titles]

        logger.info(f"  Already in DB (will skip): {len(dupes)}")
        logger.info(f"  Genuinely new products:    {len(genuinely_new)}")

        if dupes:
            logger.info("  Skipped duplicates:")
            for d in dupes:
                logger.info(f"    → {d['ebay_title'][:65]}")

        if not dry_run:
            inserted = upsert_products(new_rows)
            logger.info(f"  Inserted {inserted} new products into database")
        else:
            logger.info(f"  [DRY RUN] Would insert {len(genuinely_new)} new products")

        if load_only:
            logger.info("\n--load-only flag set. Stopping before eBay listing step.")
            return

    # ── Step 3: List new products on eBay ────────────────────────────────────
    logger.info(f"{'─'*60}")
    logger.info("STEP 3: Listing new products on eBay.de")

    listed_ids = get_listed_product_ids()
    with get_conn() as conn:
        all_products = [dict(r) for r in conn.execute(
            "SELECT * FROM products ORDER BY id"
        ).fetchall()]

    to_list = [p for p in all_products if p["id"] not in listed_ids]
    logger.info(f"  {len(to_list)} products queued for listing")

    if set_number:
        sn_clean = set_number.split(".")[0].strip()
        to_list = [p for p in to_list if (p.get("set_number") or "").split(".")[0].strip() == sn_clean]
        if not to_list:
            already = [p for p in all_products if (p.get("set_number") or "").split(".")[0].strip() == sn_clean]
            if already:
                logger.warning(f"  Set {sn_clean} is already listed. Use --force (not yet implemented) to relist.")
            else:
                logger.warning(f"  Set {sn_clean} not found in database. Run --load-only first.")
            return
        logger.info(f"  Filtered to set {sn_clean}")

    MAX_COST_PRICE = 100.00
    overpriced = [p for p in to_list if p["your_price"] > MAX_COST_PRICE]
    to_list    = [p for p in to_list if p["your_price"] <= MAX_COST_PRICE]
    if overpriced:
        logger.info(f"  Skipped {len(overpriced)} product(s) with cost > €{MAX_COST_PRICE:.0f}:")
        for p in overpriced:
            logger.info(f"    → €{p['your_price']:.2f}  {p['ebay_title'][:55]}")

    if limit:
        to_list = to_list[:limit]
        logger.info(f"  Limited to first {limit} (--limit flag)")

    if not to_list:
        logger.info("  Nothing to list!")
        return

    ok = fail = 0
    for i, product in enumerate(to_list, 1):
        title = product["ebay_title"][:55]
        logger.info(f"  [{i}/{len(to_list)}] {title} @ cost €{product['your_price']:.2f} → sell €{apply_price_markup(product['your_price']):.2f}")

        success, item_id, error = list_product(product, dry_run, debug=debug)

        if success:
            ok += 1
            if not dry_run:
                save_listing(product["id"], item_id, "listed", error, product["your_price"])
                logger.info(f"    ✅ Item ID: {item_id}")
        else:
            fail += 1
            if not dry_run:
                save_listing(product["id"], None, "error", error, product["your_price"])
            logger.error(f"    ❌ {error}")

        if not dry_run:
            time.sleep(0.5)

    logger.info(f"{'─'*60}")
    logger.info(f"Done!  ✅ {ok} listed   ❌ {fail} failed")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Load new catalog and list on eBay.de")
    p.add_argument("--dry-run",   action="store_true", help="Preview only — no changes")
    p.add_argument("--load-only", action="store_true", help="Build DB only, skip eBay listing")
    p.add_argument("--limit",     type=int, default=0,  help="Only list this many products")
    p.add_argument("--set",       default=None,          help="List a single product by set number e.g. --set 75368")
    p.add_argument("--sync",      action="store_true",   help="Sync DB with live eBay listings")
    p.add_argument("--from-db",   action="store_true",   help="Skip CSV loading — list directly from the database")
    p.add_argument("--debug",     action="store_true",   help="Log full request XML and raw eBay responses")
    args = p.parse_args()
    if args.sync:
        sync_with_ebay()
    else:
        run(dry_run=args.dry_run, load_only=args.load_only,
            limit=args.limit, set_number=args.set, from_db=args.from_db, debug=args.debug)
