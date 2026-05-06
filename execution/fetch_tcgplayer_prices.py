"""
Fetch raw (ungraded) and PSA 9 / PSA 10 prices from PriceCharting.com.

PriceCharting is a public price database for Pokemon cards (no API key needed).
It shows ungraded, PSA 9, and PSA 10 prices sourced from completed eBay sales.

URL pattern: https://www.pricecharting.com/game/pokemon-{set-slug}/{card-slug}

For each card in the watchlist this script:
  1. Constructs the PriceCharting URL from card name and set name.
  2. Fetches the page and extracts ungraded, PSA 9, and PSA 10 market prices.
  3. Saves a debug page on the first card if prices cannot be parsed.
  4. Writes results to .tmp/tcgplayer_prices.csv (same filename for pipeline compat).

Usage:
    python execution/fetch_tcgplayer_prices.py --watchlist data/watchlist.csv
    python execution/fetch_tcgplayer_prices.py
"""

import argparse
import re
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUTPUT_FILE = Path(".tmp/tcgplayer_prices.csv")
DEBUG_FILE = Path(".tmp/debug_pricecharting_page.html")
BASE_URL = "https://www.pricecharting.com"
RATE_DELAY = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def slugify(text: str) -> str:
    text = text.lower().encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9\s\-]", "", text)
    text = re.sub(r"[\s\-]+", "-", text.strip())
    return text


def set_to_slug(set_name: str) -> str:
    """Convert set name to PriceCharting URL slug (prefixed with 'pokemon-')."""
    return f"pokemon-{slugify(set_name)}"


def _parse_price(text: str) -> float | None:
    """Extract a dollar amount from text like '$1,234.56' or 'N/A'."""
    if not text:
        return None
    m = re.search(r"\$?([\d,]+\.?\d*)", text.replace(",", ""))
    if m:
        try:
            val = float(m.group(1).replace(",", ""))
            return val if val > 0 else None
        except ValueError:
            pass
    return None


def fetch_card_prices(card_name: str, set_name: str, card_number: str) -> dict:
    result = {
        "card_name": card_name,
        "set_name": set_name,
        "card_number": card_number,
        "raw_price": None,
        "psa9_price": None,
        "psa10_price": None,
        "source_url": None,
        "error": None,
    }

    set_slug = set_to_slug(set_name)
    card_slug = slugify(card_name)
    url = f"{BASE_URL}/game/{set_slug}/{card_slug}"
    result["source_url"] = url

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)

        if resp.status_code == 404:
            # Try with card number appended (PriceCharting sometimes uses it)
            if card_number and card_number != "nan":
                url2 = f"{BASE_URL}/game/{set_slug}/{card_slug}-{slugify(card_number)}"
                resp = requests.get(url2, headers=HEADERS, timeout=15)
                result["source_url"] = url2

        if resp.status_code == 404:
            result["error"] = f"Card not found on PriceCharting — tried {url}"
            return result

        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Save first page for debugging if needed
        if not DEBUG_FILE.exists():
            DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
            DEBUG_FILE.write_text(resp.text)
            print(f"  Saved debug page → {DEBUG_FILE}")

        # PriceCharting price table: rows with id like "price-new", "used-price",
        # "grade-9-price", "grade-10-price" or similar.
        # Also tries the #completed-auctions table as fallback.
        prices = _extract_prices(soup)

        result["raw_price"] = prices.get("ungraded")
        result["psa9_price"] = prices.get("grade_9")
        result["psa10_price"] = prices.get("grade_10")

        if not any(v is not None for v in [result["raw_price"], result["psa9_price"], result["psa10_price"]]):
            result["error"] = f"Page loaded but no prices found — check {DEBUG_FILE}"

    except requests.HTTPError as e:
        result["error"] = f"HTTP {e.response.status_code}: {url}"
    except Exception as e:
        result["error"] = str(e)

    return result


def _extract_prices(soup: BeautifulSoup) -> dict:
    """
    Extract ungraded, PSA 9, and PSA 10 prices from a PriceCharting card page.

    PriceCharting uses a table with rows like:
      <tr id="used_price">  → ungraded / loose price
      <tr id="grade-9-price"> or similar → PSA 9
      <tr id="grade-10-price"> → PSA 10

    Prices appear in <td> or <span> with class "price" or "js-price".
    """
    prices: dict[str, float | None] = {}

    # Strategy 1: look for rows by known id patterns
    id_map = {
        "ungraded": ["used_price", "ungraded-price", "loose_price"],
        "grade_9":  ["grade-9-price", "psa-9-price", "graded-9"],
        "grade_10": ["grade-10-price", "psa-10-price", "graded-10"],
    }

    for key, ids in id_map.items():
        for row_id in ids:
            row = soup.find(id=row_id)
            if row:
                price_el = (
                    row.find(class_="price")
                    or row.find(class_="js-price")
                    or row.find("td", class_=re.compile(r"price"))
                    or row.find("span")
                    or row.find("td")
                )
                if price_el:
                    val = _parse_price(price_el.get_text(strip=True))
                    if val:
                        prices[key] = val
                        break

    # Strategy 2: scan all table rows for grade-related labels
    if len(prices) < 2:
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(strip=True).lower()
            price_text = cells[-1].get_text(strip=True)

            if any(kw in label for kw in ("ungraded", "loose", "used")) and "ungraded" not in prices:
                val = _parse_price(price_text)
                if val:
                    prices["ungraded"] = val
            elif "grade 9" in label or "psa 9" in label or "grade-9" in label:
                if "grade_9" not in prices:
                    val = _parse_price(price_text)
                    if val:
                        prices["grade_9"] = val
            elif "grade 10" in label or "psa 10" in label or "grade-10" in label or "gem" in label:
                if "grade_10" not in prices:
                    val = _parse_price(price_text)
                    if val:
                        prices["grade_10"] = val

    return prices


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
        time.sleep(RATE_DELAY)

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
