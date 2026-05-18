"""
image_fetcher.py — Download multiple LEGO product images per set

Fetches images per set from two sources:
  1. Rebrickable API  — hero/box shot        (needs --key,          1 call/set)
  2. Brickset API     — additional angles    (needs --brickset-key, 1 call/set
                        after a one-time batch getSets call at startup)

Images are saved as:
    images/{set_number}_1.jpg        ← Rebrickable hero
    images/{set_number}_2.jpg ...    ← Brickset additional images

Brickset API rate limits (free key):
  getSets          — 100 calls/day.  This script batches ALL set numbers into
                     ONE getSets call at startup, so only 1 call is spent here.
  getAdditionalImages — no documented limit; 1 call per set during the run.

Re-runs skip sets that already have enough images on disk (--min-images).
Progress is saved to image_results.csv after every set.

Usage:
    # Rebrickable hero only (no Brickset key)
    python image_fetcher.py --key RB_KEY

    # Full multi-image run
    python image_fetcher.py --key RB_KEY --brickset-key BS_KEY

    # Test on 2 sets first
    python image_fetcher.py --key RB_KEY --brickset-key BS_KEY --limit 2 --debug

    # Re-process sets that previously only got 1 image
    python image_fetcher.py --key RB_KEY --brickset-key BS_KEY --min-images 2

Get a free Rebrickable API key at: https://rebrickable.com/api/
Get a free Brickset API key at:    https://brickset.com/tools/webservices/requestkey

Output:
    images/{set_number}_1.jpg ...   — product images
    image_results.csv               — set_number, images_saved, status

NOTE: If you get "OSError: Invalid argument" when saving the CSV,
      close image_results.csv in Excel first.
"""

import argparse
import csv
import time
import logging
import sys
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

REBRICKABLE_SET_API       = "https://rebrickable.com/api/v3/lego/sets/{set_num}-1/"
BRICKSET_API              = "https://brickset.com/api/v3.asmx"
BRICKSET_GET_SETS         = BRICKSET_API + "/getSets"
BRICKSET_GET_IMAGES       = BRICKSET_API + "/getAdditionalImages"

CATALOG_DEFAULT = "catalog_loaded.csv"
OUTPUT_CSV      = "image_results.csv"
CSV_FIELDS      = ["set_number", "images_saved", "image_files", "status"]


# -----------------------------------------------------------------------------
# CSV helpers
# -----------------------------------------------------------------------------

def load_catalog(catalog_path: str) -> list[dict]:
    rows = []
    with open(catalog_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sn = str(row.get("set_number", "")).strip().split(".")[0]
            if sn and sn.isdigit():
                rows.append({"set_number": sn})
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for row in rows:
        if row["set_number"] not in seen:
            seen.add(row["set_number"])
            unique.append(row)
    return unique


def load_existing_results(csv_path: str, min_images: int = 0) -> dict[str, dict]:
    """
    Load previously completed sets from the results CSV.
    If min_images > 0, sets with fewer saved images than that threshold
    are excluded — forcing them to be re-processed.
    """
    existing = {}
    reprocess = []
    p = Path(csv_path)
    if not p.exists():
        return existing
    with open(p, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sn = row.get("set_number", "").strip()
            if not sn:
                continue
            saved = int(row.get("images_saved", 0) or 0)
            if min_images > 0 and saved < min_images:
                reprocess.append(sn)
            else:
                existing[sn] = row
    if reprocess:
        logger.info(
            f"Loaded {len(existing)} existing results — will skip these.\n"
            f"  Re-queuing {len(reprocess)} sets with fewer than {min_images} image(s): "
            f"{', '.join(reprocess[:10])}{'...' if len(reprocess) > 10 else ''}"
        )
    else:
        logger.info(f"Loaded {len(existing)} existing results — will skip these.")
    return existing


def save_results(results: list[dict], csv_path: str) -> None:
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(results)
    except OSError as e:
        logger.error(
            f"Could not save '{csv_path}': {e}\n"
            "  >>> Close image_results.csv in Excel and re-run."
        )


# -----------------------------------------------------------------------------
# Image URL fetchers
# -----------------------------------------------------------------------------

def fetch_rebrickable_hero(set_number: str, api_key: str,
                           session: requests.Session,
                           debug: bool = False) -> str | None:
    """
    Fetch the hero/box image URL from Rebrickable's set endpoint.
    Returns the URL string, or None if not found.
    One API call per set, counts against Rebrickable free-plan quota.
    """
    headers = {"Authorization": f"key {api_key}"}
    try:
        resp = session.get(
            REBRICKABLE_SET_API.format(set_num=set_number),
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 404:
            logger.warning(f"  [{set_number}] Not found on Rebrickable (404)")
            return None
        resp.raise_for_status()
        data = resp.json()
        if debug:
            logger.debug(f"  [{set_number}] Rebrickable keys: {list(data.keys())}")
        hero = data.get("set_img_url") or ""
        if hero:
            logger.info(f"  [{set_number}] Rebrickable hero: {hero}")
        else:
            logger.warning(f"  [{set_number}] No set_img_url in Rebrickable response")
        return hero or None
    except requests.RequestException as e:
        logger.error(f"  [{set_number}] Rebrickable API error: {e}")
        return None


def batch_fetch_brickset_set_ids(set_numbers: list[str], api_key: str,
                                  session: requests.Session) -> dict[str, int]:
    """
    Resolve all set numbers to Brickset internal setIDs in as few API calls
    as possible by batching up to 50 set numbers per getSets request.

    Brickset's getSets accepts a comma-separated setNumber list (marked with *
    in the docs). Each call counts against the 100/day free-plan quota, so
    batching 79 sets costs just 2 calls instead of 79.

    Returns {set_number: setID} for every set found.
    """
    BATCH = 50
    result: dict[str, int] = {}
    batches = [set_numbers[i:i + BATCH] for i in range(0, len(set_numbers), BATCH)]

    for batch_num, batch in enumerate(batches, 1):
        # Brickset requires the full set number including variant suffix, e.g. "21349-1"
        joined = ",".join(f"{sn}-1" for sn in batch)
        logger.info(
            f"Brickset: resolving setIDs batch {batch_num}/{len(batches)} "
            f"({len(batch)} sets)..."
        )
        try:
            resp = session.get(
                BRICKSET_GET_SETS,
                params={
                    "apiKey":   api_key,
                    "userHash": "",
                    "params":   f'{{"setNumber":"{joined}"}}',
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "success":
                logger.warning(f"  Brickset getSets error: {data.get('message', data)}")
                continue
            for s in data.get("sets", []):
                # Brickset number field is like "21349-1", strip the variant
                sn = str(s.get("number", "")).strip()
                if sn and s.get("setID"):
                    result[sn] = s["setID"]
            logger.info(f"  Resolved {len(result)} setIDs so far")
        except requests.RequestException as e:
            logger.error(f"  Brickset getSets batch {batch_num} failed: {e}")

    logger.info(f"Brickset: resolved {len(result)}/{len(set_numbers)} setIDs total\n")
    return result


def fetch_brickset_additional_images(set_number: str, set_id: int,
                                      api_key: str,
                                      session: requests.Session,
                                      debug: bool = False) -> list[str]:
    """
    Fetch additional image URLs from Brickset's getAdditionalImages endpoint.
    Requires the Brickset internal setID (from batch_fetch_brickset_set_ids).
    Returns a list of full-resolution image URLs (may be empty).
    """
    try:
        resp = session.get(
            BRICKSET_GET_IMAGES,
            params={"apiKey": api_key, "setID": set_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if debug:
            logger.debug(f"  [{set_number}] Brickset getAdditionalImages: {data}")
        urls = [
            img["imageURL"]
            for img in data.get("additionalImages", [])
            if img.get("imageURL")
        ]
        logger.info(f"  [{set_number}] Brickset additional images: {len(urls)} found")
        return urls
    except requests.RequestException as e:
        logger.warning(f"  [{set_number}] Brickset getAdditionalImages failed: {e}")
        return []


def fetch_all_image_urls(set_number: str,
                          rb_api_key: str,
                          bs_api_key: str | None,
                          bs_set_ids: dict[str, int],
                          session: requests.Session,
                          debug: bool = False) -> list[str]:
    """
    Collect image URLs for a set:
      1. Rebrickable hero (always, 1 API call)
      2. Brickset additional images (if --brickset-key provided and setID known)

    Returns a deduplicated list, Rebrickable hero first.
    """
    urls: list[str] = []

    # ── 1. Rebrickable hero ───────────────────────────────────────────────────
    hero = fetch_rebrickable_hero(set_number, rb_api_key, session, debug=debug)
    if hero:
        urls.append(hero)

    # ── 2. Brickset additional images ─────────────────────────────────────────
    if bs_api_key:
        set_id = bs_set_ids.get(set_number)
        if set_id:
            extras = fetch_brickset_additional_images(
                set_number, set_id, bs_api_key, session, debug=debug
            )
            for url in extras:
                if url not in urls:
                    urls.append(url)
        else:
            logger.warning(f"  [{set_number}] No Brickset setID — skipping additional images")

    logger.info(f"  [{set_number}] {len(urls)} image URL(s) collected total")
    return urls


def download_image(image_url: str, dest_path: Path,
                   session: requests.Session) -> bool:
    try:
        resp = session.get(image_url, timeout=30, stream=True)
        resp.raise_for_status()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except requests.RequestException as e:
        logger.error(f"  Download failed ({dest_path.name}): {e}")
        return False


# -----------------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------------

def fetch_all(rb_api_key: str, bs_api_key: str | None,
              catalog_path: str, out_dir: str,
              delay: float, max_images: int, limit: int,
              min_images: int, debug: bool) -> None:

    if debug:
        logging.getLogger().setLevel(logging.DEBUG)

    catalog  = load_catalog(catalog_path)
    existing = load_existing_results(OUTPUT_CSV, min_images=min_images)
    logger.info(f"Loaded {len(catalog)} unique sets from '{catalog_path}'")

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": "lego-image-fetcher/1.0"})

    todo = [row for row in catalog if row["set_number"] not in existing]
    if limit:
        todo = todo[:limit]
        logger.info(f"--limit {limit}: processing first {limit} sets only")
    logger.info(f"{len(todo)} sets to process  ({len(existing)} already done)\n")

    # ── Pre-fetch all Brickset setIDs in one batch (uses ~2 API calls total) ──
    bs_set_ids: dict[str, int] = {}
    if bs_api_key and todo:
        all_set_numbers = [row["set_number"] for row in todo]
        bs_set_ids = batch_fetch_brickset_set_ids(all_set_numbers, bs_api_key, session)

    results: list[dict] = list(existing.values())
    ok = fail = 0

    for i, row in enumerate(todo, 1):
        sn = row["set_number"]
        logger.info(f"[{i}/{len(todo)}] Set {sn} ...")

        # Skip if enough images already exist on disk
        existing_files = sorted(out.glob(f"{sn}_*.jpg"))
        if existing_files and len(existing_files) >= min_images:
            logger.info(f"  Already on disk ({len(existing_files)} image(s)), skipping")
            results.append({
                "set_number":   sn,
                "images_saved": len(existing_files),
                "image_files":  "|".join(str(p) for p in existing_files),
                "status":       "skipped",
            })
            ok += 1
            continue

        # Fetch all available image URLs
        urls = fetch_all_image_urls(
            sn, rb_api_key, bs_api_key, bs_set_ids, session, debug=debug
        )

        if not urls:
            results.append({"set_number": sn, "images_saved": 0,
                            "image_files": "", "status": "not_found"})
            fail += 1
            save_results(results, OUTPUT_CSV)
            time.sleep(delay)
            continue

        # Download up to max_images
        urls_to_download = urls[:max_images]
        saved_paths = []

        for idx, url in enumerate(urls_to_download, 1):
            dest = out / f"{sn}_{idx}.jpg"
            if download_image(url, dest, session):
                saved_paths.append(str(dest))
                logger.info(f"  ✅ Saved image {idx}/{len(urls_to_download)}: {dest.name}")

        n = len(saved_paths)
        status = "ok" if n == len(urls_to_download) else ("partial" if n > 0 else "download_failed")
        logger.info(f"  Saved {n}/{len(urls_to_download)} images  [{status}]")

        results.append({
            "set_number":   sn,
            "images_saved": n,
            "image_files":  "|".join(saved_paths),
            "status":       status,
        })
        ok   += 1 if n > 0 else 0
        fail += 1 if n == 0 else 0

        save_results(results, OUTPUT_CSV)
        time.sleep(delay)

    logger.info(f"\n{'─'*55}")
    logger.info(f"Done!  {ok} sets OK   {fail} failed")
    logger.info(f"Results -> {OUTPUT_CSV}")
    logger.info(f"Images  -> {out.resolve()}/")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Download multiple LEGO product images per set"
    )
    p.add_argument(
        "--key", required=True,
        help="Rebrickable API key (free at rebrickable.com/api/)"
    )
    p.add_argument(
        "--brickset-key", default=None,
        help=(
            "Brickset API key for additional images (free at "
            "brickset.com/tools/webservices/requestkey). "
            "Without this only the Rebrickable hero image is fetched."
        )
    )
    p.add_argument(
        "--catalog", default=CATALOG_DEFAULT,
        help=f"Catalog CSV path (default: {CATALOG_DEFAULT})"
    )
    p.add_argument(
        "--out", default="images",
        help="Output folder for images (default: images/)"
    )
    p.add_argument(
        "--max-images", type=int, default=5,
        help="Max images to download per set (default: 5)"
    )
    p.add_argument(
        "--delay", type=float, default=1.5,
        help=(
            "Seconds between sets (default: 1.5). "
            "Rebrickable free plan: ~100 req/day → ~100 sets/day (1 call each). "
            "Brickset getAdditionalImages: 1 call/set, no documented daily cap."
        )
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Only process this many sets — useful for testing (0 = all)"
    )
    p.add_argument(
        "--min-images", type=int, default=1,
        help=(
            "Re-process sets with fewer saved images than this threshold. "
            "Use --min-images 2 to retry sets that only got 1 image. "
            "(default: 1)"
        )
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Print raw API response keys. Use with --limit 2 to diagnose issues."
    )
    args = p.parse_args()

    if not Path(args.catalog).exists():
        print(f"Catalog not found: {args.catalog}")
        sys.exit(1)

    if not args.brickset_key:
        logger.warning(
            "No --brickset-key provided. Only Rebrickable hero images will be fetched.\n"
            "  Get a free key at: https://brickset.com/tools/webservices/requestkey"
        )

    fetch_all(
        rb_api_key=args.key,
        bs_api_key=args.brickset_key,
        catalog_path=args.catalog,
        out_dir=args.out,
        delay=args.delay,
        max_images=args.max_images,
        limit=args.limit,
        min_images=args.min_images,
        debug=args.debug,
    )
