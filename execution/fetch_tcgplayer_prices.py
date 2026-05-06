"""
Fetch raw (ungraded) and PSA 9 / PSA 10 prices from PriceCharting.com.

PriceCharting is a public price database for Pokemon cards (no API key needed).
It shows ungraded, PSA 9, and PSA 10 prices sourced from completed eBay sales.

URL pattern (individual card page):
  https://www.pricecharting.com/game/pokemon-{set-slug}/{card-slug}-{card-number}

The card number is critical — without it, PriceCharting returns a list/search
page with only Grade 7 and Grade 8 columns, not the full per-grade detail page.

Writes results to .tmp/tcgplayer_prices.csv (same filename for pipeline compat).

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
    text = str(text).lower().encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9\s\-]", "", text)
    text = re.sub(r"[\s\-]+", "-", text.strip())
    return text


def _card_number_slug(card_number: str) -> str:
    """Convert card number to URL-safe slug. '4/102' → '4', 'H29' → 'h29'."""
    if not card_number or card_number == "nan":
        return ""
    # Remove '/XXX' suffix (e.g. '4/102' → '4')
    num = card_number.split("/")[0].strip()
    return slugify(num)


def _get_page(url: str) -> requests.Response | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        return resp
    except Exception:
        return None


def _is_list_page(soup: BeautifulSoup) -> bool:
    """Return True if the page is a search/list page rather than a card detail page."""
    title = soup.find("title")
    if title and "| " in title.get_text() and "List" in title.get_text():
        return True
    # List page has a #games_table with many rows; card page has #price-data table
    if soup.find(id="games_table") and not soup.find(id="price-data"):
        return True
    return False


def _parse_price(text: str) -> float | None:
    if not text:
        return None
    cleaned = text.strip().replace(",", "").replace("$", "")
    m = re.search(r"(\d+\.?\d*)", cleaned)
    if m:
        val = float(m.group(1))
        return val if val > 0 else None
    return None


def _extract_prices_detail(soup: BeautifulSoup) -> dict:
    """
    Extract prices from an individual card detail page.

    PriceCharting card pages have a table with rows identified by id or class:
      used_price / loose-price → Ungraded
      grade-9 / psa-9         → PSA 9
      grade-10 / psa-10       → PSA 10

    Prices are in <span class="js-price"> or <td class="price">.
    """
    prices: dict[str, float | None] = {}

    # Strategy 1: look for rows by known id patterns
    row_id_map = {
        "ungraded": ["used_price", "loose_price", "used-price", "loose-price"],
        "grade_9":  ["grade-9", "grade-9-price", "psa-9", "psa-9-price", "graded-9"],
        "grade_10": ["grade-10", "grade-10-price", "psa-10", "psa-10-price", "graded-10"],
    }
    for key, ids in row_id_map.items():
        for row_id in ids:
            row = soup.find(id=row_id)
            if row:
                el = (
                    row.find(class_="js-price")
                    or row.find(class_="price")
                    or row.find("td", class_=re.compile(r"price"))
                    or row.find("td")
                )
                if el:
                    val = _parse_price(el.get_text(strip=True))
                    if val:
                        prices[key] = val
                        break

    # Strategy 2: scan ALL table rows for grade labels
    if len(prices) < 2:
        for row in soup.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = cells[0].get_text(" ", strip=True).lower()
            price_text = cells[-1].get_text(strip=True)

            if any(kw in label for kw in ("ungraded", "loose", "used")) and "ungraded" not in prices:
                val = _parse_price(price_text)
                if val:
                    prices["ungraded"] = val
            elif re.search(r"\bgrade\s*9\b|\bpsa\s*9\b|\b9\s*grade\b", label) and "grade_9" not in prices:
                val = _parse_price(price_text)
                if val:
                    prices["grade_9"] = val
            elif re.search(r"\bgrade\s*10\b|\bpsa\s*10\b|\bgem\b|\b10\s*grade\b", label) and "grade_10" not in prices:
                val = _parse_price(price_text)
                if val:
                    prices["grade_10"] = val

    return prices


def _find_best_list_link(soup: BeautifulSoup, card_name: str, set_name: str) -> str | None:
    """
    From a PriceCharting search/list page, find the URL of the best-matching
    individual card page by scoring rows on card name + set name similarity.
    """
    best_url = None
    best_score = -1

    for row in soup.select("#games_table tbody tr, .product tbody tr, table tbody tr"):
        link = row.find("a", href=re.compile(r"/game/"))
        if not link:
            continue
        text = link.get_text(strip=True).lower()
        # Also grab the console/set cell if present
        console_el = row.find(class_="console") or row.find(class_="set")
        console_text = console_el.get_text(strip=True).lower() if console_el else ""
        combined = text + " " + console_text

        score = 0
        for word in card_name.lower().split():
            if word in combined:
                score += 1
        for word in set_name.lower().split():
            if word in combined:
                score += 0.5

        if score > best_score:
            best_score = score
            href = link.get("href", "")
            best_url = href if href.startswith("http") else f"{BASE_URL}{href}"

    return best_url if best_score > 0 else None


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

    set_slug = f"pokemon-{slugify(set_name)}"
    card_slug = slugify(card_name)
    num_slug = _card_number_slug(card_number)

    # Build candidate URLs — try most-specific first
    candidates = []
    if num_slug:
        candidates.append(f"{BASE_URL}/game/{set_slug}/{card_slug}-{num_slug}")
    candidates.append(f"{BASE_URL}/game/{set_slug}/{card_slug}")

    soup = None
    used_url = None
    for url in candidates:
        resp = _get_page(url)
        if resp is None:
            continue
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            used_url = url
            break
        # 404 → try next candidate

    if soup is None:
        result["error"] = f"All URLs returned 404: {candidates}"
        return result

    result["source_url"] = used_url

    # Save debug page on first card
    if not DEBUG_FILE.exists():
        DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEBUG_FILE.write_text(soup.prettify())
        print(f"  Saved debug page → {DEBUG_FILE}")

    if _is_list_page(soup):
        # Follow the best-matching link to the individual card detail page
        detail_url = _find_best_list_link(soup, card_name, set_name)
        if detail_url:
            detail_resp = _get_page(detail_url)
            if detail_resp and detail_resp.status_code == 200:
                soup = BeautifulSoup(detail_resp.text, "html.parser")
                result["source_url"] = detail_url
                # Fall through to detail extraction below
            else:
                result["error"] = f"Found list link {detail_url} but it returned {detail_resp and detail_resp.status_code}"
                return result
        else:
            result["error"] = f"Landed on list page and no matching card link found. URL: {used_url}"
            return result

    else:
        prices = _extract_prices_detail(soup)
        result["raw_price"] = prices.get("ungraded")
        result["psa9_price"] = prices.get("grade_9")
        result["psa10_price"] = prices.get("grade_10")

        if not any(v is not None for v in [result["raw_price"], result["psa9_price"], result["psa10_price"]]):
            result["error"] = f"Detail page loaded but no prices parsed — check {DEBUG_FILE}"

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
