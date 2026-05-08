"""
Fetch raw (ungraded) and PSA 9 / PSA 10 prices from eBay completed sales.

Replaces fetch_tcgplayer_prices.py. Uses the eBay Finding API
findCompletedItems operation with SoldItemsOnly=true — same EBAY_APP_ID,
no new credentials, no Cloudflare, no scraping.

For each card:
  raw_price   — median of last SAMPLE_SIZE sold ungraded listings
  psa9_price  — median of last SAMPLE_SIZE sold PSA 9 listings
  psa10_price — median of last SAMPLE_SIZE sold PSA 10 listings

Output: .tmp/ebay_prices.csv  (card_name, set_name, card_number, raw_price,
                                psa9_price, psa10_price, raw_sales_count,
                                psa9_sales_count, psa10_sales_count, source_url)

Usage:
    python execution/fetch_ebay_prices.py
    python execution/fetch_ebay_prices.py --input .tmp/filtered_cards.csv
    python execution/fetch_ebay_prices.py --workers 5 --sample 20
"""

import argparse
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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

SAMPLE_SIZE  = 20   # sold listings to sample per price bucket
MAX_WORKERS  = 3    # parallel threads (stay well within eBay rate limits)
RATE_DELAY   = 0.3  # seconds between requests per thread


def _get(params: dict) -> dict | None:
    """Call the eBay Finding API and return the parsed JSON, or None on failure."""
    try:
        resp = _SESSION.get(EBAY_FINDING_URL, params=params, timeout=20)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _is_raw(title: str) -> bool:
    """Return True if the listing title looks like an ungraded copy."""
    t = title.lower()
    graded_kws = ["psa ", "psa-", "psa9", "psa10", "bgs ", "cgc ", "sgc ",
                  "graded", "gem mint", "beckett"]
    return not any(kw in t for kw in graded_kws)


def _has_grade(title: str, grade: int) -> bool:
    """Return True if the title explicitly mentions the given PSA grade."""
    t = title.upper()
    if grade == 9:
        # Accept "PSA 9" but reject "PSA 9.5", "PSA 10"
        return ("PSA 9" in t) and ("PSA 9.5" not in t) and ("PSA 10" not in t) and ("PSA10" not in t)
    if grade == 10:
        return ("PSA 10" in t) or ("PSA10" in t)
    return False


def _sold_prices(keywords: str, app_id: str,
                 title_filter=None, max_results: int = SAMPLE_SIZE) -> list[float]:
    """
    Query findCompletedItems for sold listings matching keywords.
    Returns a list of sold prices (float). title_filter is an optional callable.
    """
    params = {
        "OPERATION-NAME":           "findCompletedItems",
        "SERVICE-VERSION":          "1.13.0",
        "SECURITY-APPNAME":         app_id,
        "RESPONSE-DATA-FORMAT":     "JSON",
        "keywords":                 keywords,
        "itemFilter(0).name":       "SoldItemsOnly",
        "itemFilter(0).value":      "true",
        "sortOrder":                "StartTimeNewest",
        "paginationInput.entriesPerPage": str(min(max_results * 3, 100)),
    }

    data = _get(params)
    if data is None:
        return []

    items = (data
             .get("findCompletedItemsResponse", [{}])[0]
             .get("searchResult", [{}])[0]
             .get("item", []))

    prices = []
    for item in items:
        title = item.get("title", [""])[0]
        if title_filter and not title_filter(title):
            continue

        price_str = (item.get("sellingStatus", [{}])[0]
                        .get("convertedCurrentPrice", [{}])[0]
                        .get("__value__", "0"))
        try:
            price = float(price_str)
            if price > 0:
                prices.append(price)
        except (ValueError, TypeError):
            pass

        if len(prices) >= max_results:
            break

    return prices


def _median(prices: list[float]) -> float | None:
    if not prices:
        return None
    return float(pd.Series(prices).median())


def fetch_card_prices(card_name: str, set_name: str,
                      card_number: str, app_id: str) -> dict:
    """Fetch raw, PSA 9, and PSA 10 median sold prices for one card."""
    base = f"{card_name} {set_name} pokemon"

    time.sleep(RATE_DELAY)
    raw_prices = _sold_prices(base, app_id, title_filter=_is_raw)

    time.sleep(RATE_DELAY)
    psa9_prices = _sold_prices(
        f"PSA 9 {base}", app_id,
        title_filter=lambda t: _has_grade(t, 9),
    )

    time.sleep(RATE_DELAY)
    psa10_prices = _sold_prices(
        f"PSA 10 {base}", app_id,
        title_filter=lambda t: _has_grade(t, 10),
    )

    search_url = (
        "https://www.ebay.com/sch/i.html?"
        + urllib.parse.urlencode({
            "_nkw": base,
            "LH_Complete": "1",
            "LH_Sold": "1",
        })
    )

    return {
        "card_name":        card_name,
        "set_name":         set_name,
        "card_number":      card_number,
        "raw_price":        _median(raw_prices),
        "psa9_price":       _median(psa9_prices),
        "psa10_price":      _median(psa10_prices),
        "raw_sales_count":  len(raw_prices),
        "psa9_sales_count": len(psa9_prices),
        "psa10_sales_count":len(psa10_prices),
        "source_url":       search_url,
    }


def run(input_path: str = str(INPUT_FILE),
        output_path: str = str(OUTPUT_FILE),
        workers: int = MAX_WORKERS,
        sample: int = SAMPLE_SIZE) -> pd.DataFrame:
    load_dotenv()
    app_id = os.environ.get("EBAY_APP_ID")
    if not app_id:
        raise RuntimeError("EBAY_APP_ID not set — cannot fetch eBay prices")

    global SAMPLE_SIZE
    SAMPLE_SIZE = sample

    cards_df = pd.read_csv(input_path)
    total = len(cards_df)
    print(f"Fetching eBay sold prices for {total} cards ({workers} workers)...")

    results = []
    completed = 0
    lock = __import__("threading").Lock()

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
                    "card_name":   row.get("card_name"),
                    "set_name":    row.get("set_name"),
                    "card_number": row.get("card_number"),
                    "raw_price": None, "psa9_price": None, "psa10_price": None,
                    "raw_sales_count": 0, "psa9_sales_count": 0, "psa10_sales_count": 0,
                    "source_url": None,
                    "error": str(e),
                }

            with lock:
                results.append(result)
                completed += 1
                if completed % 50 == 0 or completed == total:
                    pct = completed / total * 100
                    has_raw  = sum(1 for r in results if r.get("raw_price"))
                    has_psa  = sum(1 for r in results
                                   if r.get("psa9_price") or r.get("psa10_price"))
                    print(f"  {completed}/{total} ({pct:.0f}%)  —  "
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
    parser.add_argument("--workers", type=int, default=MAX_WORKERS,
                        help="Parallel threads (default: 3)")
    parser.add_argument("--sample",  type=int, default=SAMPLE_SIZE,
                        help="Sold listings to sample per price bucket (default: 20)")
    args = parser.parse_args()
    run(input_path=args.input, output_path=args.output,
        workers=args.workers, sample=args.sample)
