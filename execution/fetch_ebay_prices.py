"""
Fetch raw (ungraded) and PSA 9 / PSA 10 prices from eBay completed sales.

Uses the eBay Finding API findCompletedItems with SoldItemsOnly=true.
Makes ONE API call per card (not 3), then buckets results by grade from
the title. This keeps total calls to ~300 per run instead of ~900,
avoiding the soft rate-limit that causes HTTP 500 responses.

Output: .tmp/ebay_prices.csv  (card_name, set_name, card_number, raw_price,
                                psa9_price, psa10_price, raw_sales_count,
                                psa9_sales_count, psa10_sales_count, source_url)

Usage:
    python execution/fetch_ebay_prices.py
    python execution/fetch_ebay_prices.py --input .tmp/filtered_cards.csv
"""

import argparse
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading

import pandas as pd
from dotenv import load_dotenv
import os

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome124")
except ImportError:
    import requests as _req_fallback
    _SESSION = _req_fallback.Session()

EBAY_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
INPUT_FILE  = Path(".tmp/filtered_cards.csv")
OUTPUT_FILE = Path(".tmp/ebay_prices.csv")

MAX_WORKERS = 2    # parallel threads
RATE_DELAY  = 1.5  # seconds between requests per thread
MAX_RETRIES = 3    # retries on HTTP 500


def _get(params: dict) -> dict | None:
    """Call the eBay Finding API. On HTTP 500 retry once after 5s, then give up."""
    for attempt in range(1, 3):
        try:
            resp = _SESSION.get(EBAY_FINDING_URL, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 500 and attempt == 1:
                time.sleep(5)
                continue
        except Exception:
            if attempt == 1:
                time.sleep(5)
    return None


def _is_raw(title: str) -> bool:
    t = title.lower()
    return not any(kw in t for kw in
                   ["psa ", "psa-", "psa9", "psa10", "bgs ", "cgc ", "sgc ",
                    "graded", "gem mint", "beckett"])


def _has_grade(title: str, grade: int) -> bool:
    t = title.upper()
    if grade == 10:
        return ("PSA 10" in t) or ("PSA10" in t)
    if grade == 9:
        return (("PSA 9" in t) or ("PSA9" in t)) and not _has_grade(title, 10) and ("PSA 9.5" not in t)
    return False


def _median(prices: list[float]) -> float | None:
    return float(pd.Series(prices).median()) if prices else None


def fetch_card_prices(card_name: str, set_name: str,
                      card_number: str, app_id: str) -> dict:
    """
    One API call per card: fetch last 30 completed sold listings and
    bucket them into raw / PSA 9 / PSA 10 by title.
    """
    time.sleep(RATE_DELAY)
    keywords = f"{card_name} {set_name} pokemon"

    params = {
        "OPERATION-NAME":                  "findCompletedItems",
        "SERVICE-VERSION":                 "1.13.0",
        "SECURITY-APPNAME":                app_id,
        "RESPONSE-DATA-FORMAT":            "JSON",
        "keywords":                        keywords,
        "itemFilter(0).name":              "SoldItemsOnly",
        "itemFilter(0).value":             "true",
        "sortOrder":                       "StartTimeNewest",
        "paginationInput.entriesPerPage":  "30",
    }

    data = _get(params)

    raw_prices, psa9_prices, psa10_prices = [], [], []

    if data:
        items = (data
                 .get("findCompletedItemsResponse", [{}])[0]
                 .get("searchResult", [{}])[0]
                 .get("item", []))

        for item in items:
            title = item.get("title", [""])[0]
            price_str = (item.get("sellingStatus", [{}])[0]
                            .get("convertedCurrentPrice", [{}])[0]
                            .get("__value__", "0"))
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue
            if price <= 0:
                continue

            if _has_grade(title, 10):
                psa10_prices.append(price)
            elif _has_grade(title, 9):
                psa9_prices.append(price)
            elif _is_raw(title):
                raw_prices.append(price)

    return {
        "card_name":         card_name,
        "set_name":          set_name,
        "card_number":       card_number,
        "raw_price":         _median(raw_prices),
        "psa9_price":        _median(psa9_prices),
        "psa10_price":       _median(psa10_prices),
        "raw_sales_count":   len(raw_prices),
        "psa9_sales_count":  len(psa9_prices),
        "psa10_sales_count": len(psa10_prices),
        "source_url": (
            "https://www.ebay.com/sch/i.html?"
            + urllib.parse.urlencode({"_nkw": keywords,
                                      "LH_Complete": "1", "LH_Sold": "1"})
        ),
    }


def run(input_path: str = str(INPUT_FILE),
        output_path: str = str(OUTPUT_FILE),
        workers: int = MAX_WORKERS) -> pd.DataFrame:
    load_dotenv()
    app_id = os.environ.get("EBAY_APP_ID")
    if not app_id:
        raise RuntimeError("EBAY_APP_ID not set — cannot fetch eBay prices")

    cards_df = pd.read_csv(input_path)
    total = len(cards_df)
    print(f"Fetching eBay sold prices for {total} cards "
          f"(1 call/card, {workers} workers, {RATE_DELAY}s delay)...")

    results = []
    completed = 0
    lock = threading.Lock()

    def _fetch(row):
        return fetch_card_prices(
            str(row.get("card_name", "")).strip(),
            str(row.get("set_name", "")).strip(),
            str(row.get("card_number", "")).strip(),
            app_id,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch, row): row for _, row in cards_df.iterrows()}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as e:
                row = futures[future]
                result = {
                    "card_name": row.get("card_name"), "set_name": row.get("set_name"),
                    "card_number": row.get("card_number"),
                    "raw_price": None, "psa9_price": None, "psa10_price": None,
                    "raw_sales_count": 0, "psa9_sales_count": 0, "psa10_sales_count": 0,
                    "source_url": None, "error": str(e),
                }

            with lock:
                results.append(result)
                completed += 1
                if completed % 50 == 0 or completed == total:
                    has_raw = sum(1 for r in results if r.get("raw_price"))
                    has_psa = sum(1 for r in results if r.get("psa9_price") or r.get("psa10_price"))
                    print(f"  {completed}/{total} ({completed/total*100:.0f}%)  —  "
                          f"{has_raw} with raw price, {has_psa} with PSA price")

    df = pd.DataFrame(results)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)

    has_any = df["raw_price"].notna() | df["psa9_price"].notna() | df["psa10_price"].notna()
    print(f"\neBay prices fetched → {output_path}")
    print(f"  {has_any.sum()}/{total} cards had at least one price")
    print(f"  {df['raw_price'].notna().sum()} with raw price")
    print(f"  {df['psa9_price'].notna().sum()} with PSA 9 price")
    print(f"  {df['psa10_price'].notna().sum()} with PSA 10 price")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch eBay sold prices for filtered cards")
    parser.add_argument("--input",   default=str(INPUT_FILE))
    parser.add_argument("--output",  default=str(OUTPUT_FILE))
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = parser.parse_args()
    run(input_path=args.input, output_path=args.output, workers=args.workers)
