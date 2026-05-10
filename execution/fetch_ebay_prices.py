"""
Fetch raw (ungraded) and PSA 9 / PSA 10 prices from eBay active listings.

Uses the eBay Browse API (OAuth) — modern, no aggressive rate limits.
findCompletedItems (Finding API) is rate-limited to ~5 calls total per day,
making it unusable at 300+ cards. Browse API handles 5,000+ calls/day.

Makes ONE API call per card (50 results), buckets by grade from title.

Output: .tmp/ebay_prices.csv  (card_name, set_name, card_number, raw_price,
                                psa9_price, psa10_price, raw_sales_count,
                                psa9_sales_count, psa10_sales_count, source_url)

Usage:
    python execution/fetch_ebay_prices.py
    python execution/fetch_ebay_prices.py --input .tmp/filtered_cards.csv
"""

import argparse
import base64
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

BROWSE_URL      = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_TOKEN_URL  = "https://api.ebay.com/identity/v1/oauth2/token"
INPUT_FILE      = Path(".tmp/filtered_cards.csv")
OUTPUT_FILE     = Path(".tmp/ebay_prices.csv")

MAX_WORKERS = 1      # sequential to stay within eBay per-second rate limit
RATE_DELAY  = 1.2    # seconds between requests

_token_cache: dict = {"token": None, "expires_at": 0.0}
_token_lock = threading.Lock()


def _get_oauth_token(app_id: str, cert_id: str) -> str | None:
    with _token_lock:
        if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
            return _token_cache["token"]
        credentials = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
        try:
            resp = _SESSION.post(
                EBAY_TOKEN_URL,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                _token_cache["token"] = data["access_token"]
                _token_cache["expires_at"] = time.time() + data.get("expires_in", 7200)
                print(f"  [OAuth] token acquired")
                return _token_cache["token"]
            print(f"  [OAuth] status={resp.status_code} body={resp.text[:200]}")
        except Exception as e:
            print(f"  [OAuth] error: {e}")
    return None


def _search_browse(keywords: str, token: str, limit: int = 50) -> list[dict]:
    backoffs = [5, 15, 45]
    for attempt, wait in enumerate(backoffs + [None], 1):
        try:
            resp = _SESSION.get(
                BROWSE_URL,
                params={"q": keywords, "limit": limit},
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                    "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DUS",
                },
                timeout=20,
            )
            if resp.status_code == 200:
                return resp.json().get("itemSummaries", [])
            if resp.status_code == 429 and wait is not None:
                print(f"  [Browse] 429 rate-limited, waiting {wait}s (attempt {attempt})...")
                time.sleep(wait)
                continue
            print(f"  [Browse] status={resp.status_code}")
        except Exception as e:
            print(f"  [Browse] error: {e}")
        break
    return []


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
                      card_number: str, token: str) -> dict:
    time.sleep(RATE_DELAY)
    keywords = f"{card_name} {set_name} pokemon"

    raw_prices, psa9_prices, psa10_prices = [], [], []

    items = _search_browse(keywords, token, limit=50)
    for item in items:
        title = item.get("title", "")
        try:
            price = float(item.get("price", {}).get("value", 0))
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
            + urllib.parse.urlencode({"_nkw": keywords, "LH_Complete": "1", "LH_Sold": "1"})
        ),
    }


def run(input_path: str = str(INPUT_FILE),
        output_path: str = str(OUTPUT_FILE),
        workers: int = MAX_WORKERS) -> pd.DataFrame:
    load_dotenv()
    app_id  = os.environ.get("EBAY_APP_ID")
    cert_id = os.environ.get("EBAY_CERT_ID")
    if not app_id:
        raise RuntimeError("EBAY_APP_ID not set — cannot fetch eBay prices")
    if not cert_id:
        raise RuntimeError("EBAY_CERT_ID not set — Browse API requires OAuth (EBAY_CERT_ID)")

    token = _get_oauth_token(app_id, cert_id)
    if not token:
        raise RuntimeError("Failed to get eBay OAuth token — check EBAY_APP_ID and EBAY_CERT_ID")

    cards_df = pd.read_csv(input_path)
    total = len(cards_df)
    print(f"Fetching eBay prices for {total} cards "
          f"(Browse API, {workers} workers, {RATE_DELAY}s delay)...")

    results = []
    completed = 0
    lock = threading.Lock()

    def _fetch(row):
        return fetch_card_prices(
            str(row.get("card_name", "")).strip(),
            str(row.get("set_name", "")).strip(),
            str(row.get("card_number", "")).strip(),
            token,
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
    parser = argparse.ArgumentParser(description="Fetch eBay prices for filtered cards")
    parser.add_argument("--input",   default=str(INPUT_FILE))
    parser.add_argument("--output",  default=str(OUTPUT_FILE))
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = parser.parse_args()
    run(input_path=args.input, output_path=args.output, workers=args.workers)
