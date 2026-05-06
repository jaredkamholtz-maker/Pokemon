"""
Fetch raw (ungraded) and PSA 9 / PSA 10 market prices from TCGPlayer API v2.

For each card in the watchlist this script:
  1. Searches for the base ungraded product.
  2. Searches for "[card_name] PSA 10" and "[card_name] PSA 9" variants.
  3. Pulls market prices for each matching product.
  4. Writes results to .tmp/tcgplayer_prices.csv

Usage:
    python execution/fetch_tcgplayer_prices.py --watchlist data/watchlist.csv
    python execution/fetch_tcgplayer_prices.py  # uses default path
"""

import argparse
import csv
import time
from pathlib import Path

import pandas as pd
import requests

from auth_tcgplayer import auth_headers

BASE_URL = "https://api.tcgplayer.com/v2"
OUTPUT_FILE = Path(".tmp/tcgplayer_prices.csv")
RATE_DELAY = 0.3  # seconds between API calls to be polite


def search_products(query: str, set_name: str = "") -> list[dict]:
    """Search TCGPlayer catalog for Pokemon cards matching query."""
    params = {
        "productLineName": "Pokemon",
        "productName": query,
        "limit": 20,
        "offset": 0,
    }
    if set_name:
        params["setName"] = set_name

    resp = requests.get(
        f"{BASE_URL}/catalog/products",
        headers=auth_headers(),
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def get_prices(product_ids: list[int]) -> dict[int, dict]:
    """Return marketPrice keyed by productId for a batch of product IDs."""
    if not product_ids:
        return {}

    ids_str = ",".join(str(i) for i in product_ids)
    resp = requests.get(
        f"{BASE_URL}/pricing/product/{ids_str}",
        headers=auth_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return {r["productId"]: r for r in results}


def best_market_price(products: list[dict], prices: dict[int, dict]) -> float | None:
    """Return the lowest non-None marketPrice across a set of matching products."""
    candidates = []
    for p in products:
        pid = p.get("productId")
        if pid and pid in prices:
            mp = prices[pid].get("marketPrice")
            if mp:
                candidates.append(float(mp))
    return min(candidates) if candidates else None


def fetch_card_prices(card_name: str, set_name: str, card_number: str) -> dict:
    """
    Fetch raw, PSA 9, and PSA 10 market prices for a single card.
    Returns a dict with keys: card_name, set_name, raw_price, psa9_price, psa10_price
    """
    result = {
        "card_name": card_name,
        "set_name": set_name,
        "card_number": card_number,
        "raw_price": None,
        "psa9_price": None,
        "psa10_price": None,
        "raw_product_id": None,
        "error": None,
    }

    try:
        # --- Raw (ungraded) price ---
        raw_products = search_products(card_name, set_name)
        # Filter out graded listings (they contain "PSA", "BGS", "CGC" in name)
        grade_keywords = ("PSA", "BGS", "CGC", "SGC")
        raw_only = [
            p for p in raw_products
            if not any(kw in p.get("name", "").upper() for kw in grade_keywords)
        ]

        raw_ids = [p["productId"] for p in raw_only if p.get("productId")]
        if raw_ids:
            raw_prices = get_prices(raw_ids)
            result["raw_price"] = best_market_price(raw_only, raw_prices)
            if raw_only:
                result["raw_product_id"] = raw_only[0].get("productId")
        time.sleep(RATE_DELAY)

        # --- PSA 10 price ---
        psa10_query = f"{card_name} PSA 10"
        psa10_products = search_products(psa10_query, set_name)
        psa10_products = [p for p in psa10_products if "PSA 10" in p.get("name", "")]
        psa10_ids = [p["productId"] for p in psa10_products if p.get("productId")]
        if psa10_ids:
            psa10_prices_data = get_prices(psa10_ids)
            result["psa10_price"] = best_market_price(psa10_products, psa10_prices_data)
        time.sleep(RATE_DELAY)

        # --- PSA 9 price ---
        psa9_query = f"{card_name} PSA 9"
        psa9_products = search_products(psa9_query, set_name)
        # Be specific: must contain "PSA 9" but NOT "PSA 10"
        psa9_products = [
            p for p in psa9_products
            if "PSA 9" in p.get("name", "") and "PSA 10" not in p.get("name", "")
        ]
        psa9_ids = [p["productId"] for p in psa9_products if p.get("productId")]
        if psa9_ids:
            psa9_prices_data = get_prices(psa9_ids)
            result["psa9_price"] = best_market_price(psa9_products, psa9_prices_data)
        time.sleep(RATE_DELAY)

    except requests.HTTPError as e:
        result["error"] = f"HTTP {e.response.status_code}: {e}"
    except Exception as e:
        result["error"] = str(e)

    return result


def run(watchlist_path: str) -> pd.DataFrame:
    watchlist = pd.read_csv(watchlist_path)
    required_cols = {"card_name", "set_name", "card_number"}
    missing = required_cols - set(watchlist.columns)
    if missing:
        raise ValueError(f"watchlist.csv missing columns: {missing}")

    rows = []
    total = len(watchlist)
    for i, row in watchlist.iterrows():
        print(f"[{i+1}/{total}] Fetching prices: {row['card_name']} ({row['set_name']})")
        prices = fetch_card_prices(
            card_name=row["card_name"],
            set_name=row["set_name"],
            card_number=str(row.get("card_number", "")),
        )
        rows.append(prices)

    df = pd.DataFrame(rows)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_FILE, index=False)
    print(f"\nSaved {len(df)} rows → {OUTPUT_FILE}")

    failed = df[df["error"].notna()]
    if not failed.empty:
        print(f"  {len(failed)} cards had errors:")
        for _, r in failed.iterrows():
            print(f"    - {r['card_name']}: {r['error']}")

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", default="data/watchlist.csv")
    args = parser.parse_args()
    run(args.watchlist)
