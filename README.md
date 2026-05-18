# LEGO eBay Dropship Pricer

A Python automation toolkit for LEGO dropshipping on eBay.de. Scrapes product catalogs from Otto.de, fetches product images from Rebrickable and Brickset, lists items on eBay via the Trading API, and monitors competitor prices across Otto, Alza, and Bing Shopping.

---

## Overview

The workflow runs in three stages, plus an ongoing price-monitoring loop:

```
1. catalog_loader.py   →  catalog_new.csv
2. image_fetcher.py    →  images/*.jpg
3. prepare_and_list.py →  prices.db  +  live eBay listings
        ↕
   main.py  (scrape / report / suggest — run on any schedule)
```

---

## Requirements

- Python 3.11+
- An [eBay Developer account](https://developer.ebay.com/) with a production app and user token
- A free [Rebrickable API key](https://rebrickable.com/api/)
- (Optional) A free [Brickset API key](https://brickset.com/tools/webservices/requestkey) for additional images

### Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```
EBAY_APP_ID=YourApp-PRD-...
EBAY_DEV_ID=xxxxxxxx-xxxx-...
EBAY_CERT_ID=PRD-...
EBAY_USER_TOKEN=paste_your_ebay_auth_token_here
EBAY_POSTAL_CODE=10115
EBAY_SITE_ID=77
EBAY_DISPATCH_TIME=1
EBAY_QUANTITY=10
```

> **Never commit your `.env` file.** It is already in `.gitignore`.

---

## Stage 1 — Catalog Loader (`catalog_loader.py`)

Scrapes Otto.de for bestselling LEGO sets (€10–€70), paginates through results, fetches product descriptions from each product page, and writes `catalog_new.csv`.

```bash
# Default: top 5 pages (~100 products)
python catalog_loader.py

# More products
python catalog_loader.py --pages 10

# Show browser window (useful for first run / debugging)
python catalog_loader.py --headful

# Save debug screenshots and HTML per page
python catalog_loader.py --debug --pages 1

# Skip description fetching (faster, less complete)
python catalog_loader.py --no-descriptions

# Fetch a single product page directly by URL
python catalog_loader.py --url https://www.otto.de/p/lego-...

# Use a proxy
python catalog_loader.py --proxy http://user:pass@host:port
```

**Output:** `catalog_new.csv` with columns: `set_number`, `category`, `ebay_title`, `your_price`, `art_nr`, `description`

---

## Stage 2 — Image Fetcher (`image_fetcher.py`)

Downloads up to 5 product images per set from Rebrickable (hero/box shot) and Brickset (additional angles). Skips sets that already have enough images on disk. Progress is saved to `image_results.csv` after every set, so re-runs are safe.

```bash
# Hero image only (Rebrickable)
python image_fetcher.py --key YOUR_REBRICKABLE_KEY

# Full multi-image run (Rebrickable + Brickset)
python image_fetcher.py --key YOUR_RB_KEY --brickset-key YOUR_BS_KEY

# Test on 2 sets first
python image_fetcher.py --key YOUR_RB_KEY --brickset-key YOUR_BS_KEY --limit 2 --debug

# Re-process sets that previously only got 1 image
python image_fetcher.py --key YOUR_RB_KEY --brickset-key YOUR_BS_KEY --min-images 2

# Read from a specific catalog file (default: catalog_loaded.csv)
python image_fetcher.py --key YOUR_RB_KEY --catalog catalog_new.csv
```

> **Tip:** Pass `--catalog catalog_new.csv` if you want to fetch images for the freshly scraped catalog before loading it into the database.

**Output:** `images/{set_number}_1.jpg`, `_2.jpg`, … and `image_results.csv`

---

## Stage 3 — Prepare & List (`prepare_and_list.py`)

Loads the new catalog into `prices.db`, deduplicates against already-listed products, applies a tier-based price markup, and pushes live eBay.de listings via the Trading API.

```bash
# Preview what would happen — no DB or eBay changes
python prepare_and_list.py --dry-run

# Load CSVs into DB only, skip eBay listing
python prepare_and_list.py --load-only

# Full run: load + list all new products
python prepare_and_list.py

# List only N products (safe for testing)
python prepare_and_list.py --limit 3

# List a single set by number
python prepare_and_list.py --from-db --set 75368

# Skip CSV loading — list directly from what is already in the DB
python prepare_and_list.py --from-db

# Sync DB with live eBay listings (marks ended listings for re-listing)
python prepare_and_list.py --sync

# Verbose mode — logs full request XML and eBay responses
python prepare_and_list.py --debug
```

### Pricing logic

Prices are calculated from the Otto cost price using a fixed-markup tier table, then rounded **up** to the nearest `.99` ending:

| Cost range | Markup |
|---|---|
| €0 – €6.99 | +€4 |
| €7 – €14.99 | +€5 |
| €15 – €24.99 | +€6 |
| €25 – €34.99 | +€7 |
| … | +€1 per €10 band |

Products with a cost price above **€100** are skipped automatically as a risk guard.

---

## Ongoing — Price Monitor (`main.py`)

Scrapes competitor prices on Otto, Alza, and Bing Shopping and generates CSV reports. Run this on any schedule (e.g. daily cron) independently of the listing workflow.

```bash
# First-time setup: load your product catalog
python main.py load catalog_new.csv

# Scrape all platforms
python main.py scrape

# Scrape specific platforms only
python main.py scrape --platforms otto
python main.py scrape --platforms otto,alza,bing

# Show browser window (useful for debugging bot detection)
python main.py scrape --headful

# Use a proxy
python main.py scrape --proxy http://user:pass@host:port

# Export price comparison CSV
python main.py report

# Export pricing suggestions (current price vs. market + suggested new price)
python main.py suggest
```

**Outputs** (in `output/`):
- `price_comparison_YYYYMMDD_HHMM.csv` — your price vs. Otto / Alza / Bing
- `price_suggestions_YYYYMMDD_HHMM.csv` — suggested sell prices (Bing floor + 15% margin, capped at 125% of market max)

---

## Project structure

```
.
├── catalog_loader.py      # Stage 1: scrape Otto.de catalog
├── image_fetcher.py       # Stage 2: download images from Rebrickable / Brickset
├── prepare_and_list.py    # Stage 3: load DB + list on eBay
├── main.py                # Ongoing: price monitoring (scrape / report / suggest)
├── database.py            # SQLite helpers (init, upsert, snapshots, alerts)
├── scrapers.py            # Playwright scrapers for Otto / Alza / Bing Shopping
├── requirements.txt
├── .env.example           # Credential template — copy to .env
└── .gitignore
```

---

## Database schema (prices.db)

| Table | Purpose |
|---|---|
| `products` | Master product list (set_number, title, cost price, category) |
| `price_snapshots` | One row per scrape per platform — full price history |
| `price_alerts` | Price change events (drop / rise / new / out_of_stock) |
| `ebay_listings` | eBay item IDs and listing status per product |

---

## Troubleshooting

**Otto tiles not found / zero products scraped**
Run `--headful --debug --pages 1` and open the saved `debug_page1.html` in a browser to inspect what Otto rendered. Otto occasionally changes its CSS class names.

**eBay API returns `IAF_BINDING_INVALID_ITEMSPECIFIC`**
The category ID for that product type may have changed. Check [eBay's category finder](https://pages.ebay.de/sellerinformation/sellingresources/categoryfinder/) and update `CATEGORY_MAP` in `prepare_and_list.py`.

**Image upload fails with `PictureSet not found`**
eBay Picture Services can be intermittently slow. Re-run with `--from-db --set <set_number>` to retry individual listings.

**`playwright-stealth` not installed warning**
Install it with `pip install playwright-stealth`. It reduces the chance of being blocked on Otto, but the scraper works without it.

---

## License

MIT
