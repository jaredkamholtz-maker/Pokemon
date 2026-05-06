"""
Scrape PSA grade population data from pokedata.io for each card in the watchlist.

pokedata.io shows how many copies of each card have been submitted to PSA and
what grade they received. This script extracts:
  - total_graded: all submissions
  - psa10_count: grade 10 copies
  - psa9_count:  grade 9 copies
  - gem_rate:    (psa9 + psa10) / total

Writes results to .tmp/pokedata_population.csv

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
"""

import argparse
import re
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUTPUT_FILE = Path(".tmp/pokedata_population.csv")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
BASE_URL = "https://www.pokedata.io"
SEARCH_URL = f"{BASE_URL}/search"
RATE_DELAY = 2.0  # be respectful; pokedata.io is a small site
MAX_RETRIES = 3


def _get_with_retry(url: str, params: dict = None) -> requests.Response:
    """GET with exponential backoff on 429 / 5xx."""
    delay = 5.0
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
        if resp.status_code == 429 or resp.status_code >= 500:
            print(f"  Rate limited / server error ({resp.status_code}), sleeping {delay}s...")
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"Failed after {MAX_RETRIES} retries: {url}")


def search_card(card_name: str, set_name: str) -> str | None:
    """
    Search pokedata.io and return the URL of the best matching card page.
    Returns None if no match found.
    """
    query = f"{card_name} {set_name}"
    resp = _get_with_retry(SEARCH_URL, params={"q": query})
    soup = BeautifulSoup(resp.text, "html.parser")

    # pokedata.io search returns a list of cards; find the first matching link
    # Look for anchor tags that point to card detail pages (/cards/ or /pokemon/)
    card_link_patterns = ["/cards/", "/pokemon/", "/card/"]
    for pattern in card_link_patterns:
        links = soup.find_all("a", href=re.compile(re.escape(pattern)))
        if links:
            href = links[0].get("href", "")
            return href if href.startswith("http") else BASE_URL + href

    return None


def parse_population_page(url: str) -> dict:
    """
    Parse a pokedata.io card page and extract PSA grade distribution.
    Returns dict: total_graded, psa10_count, psa9_count, source_url
    """
    resp = _get_with_retry(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(separator=" ")

    grade_counts: dict[int, int] = {}

    # Strategy 1: look for a population table with grade columns
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            for i, cell in enumerate(cells):
                # Match cells that are a grade number (1-10)
                if re.fullmatch(r"(10|[1-9])", cell):
                    # The count is usually in the next cell
                    if i + 1 < len(cells):
                        count_text = cells[i + 1].replace(",", "")
                        if re.fullmatch(r"\d+", count_text):
                            grade_counts[int(cell)] = int(count_text)

    # Strategy 2: parse inline "Grade X: N" patterns from page text if table parse failed
    if not grade_counts:
        for match in re.finditer(r"(?:Grade\s+|PSA\s+)(\d{1,2})[:\s]+(\d[\d,]*)", text, re.I):
            grade = int(match.group(1))
            count = int(match.group(2).replace(",", ""))
            if 1 <= grade <= 10:
                grade_counts[grade] = grade_counts.get(grade, 0) + count

    # Strategy 3: look for data-* attributes or JSON-LD embedded data
    if not grade_counts:
        scripts = soup.find_all("script", type="application/json")
        for script in scripts:
            try:
                import json
                data = json.loads(script.string or "")
                # Try to find grade distribution in any nested structure
                data_str = json.dumps(data)
                for match in re.finditer(r'"grade"\s*:\s*(\d+).*?"count"\s*:\s*(\d+)', data_str):
                    grade_counts[int(match.group(1))] = int(match.group(2))
            except Exception:
                pass

    total_graded = sum(grade_counts.values())
    psa10_count = grade_counts.get(10, 0)
    psa9_count = grade_counts.get(9, 0)

    return {
        "total_graded": total_graded,
        "psa10_count": psa10_count,
        "psa9_count": psa9_count,
        "source_url": url,
        "parse_success": total_graded > 0,
    }


def fetch_population(card_name: str, set_name: str, card_number: str) -> dict:
    """Full pipeline for a single card: search → parse → return population dict."""
    base = {
        "card_name": card_name,
        "set_name": set_name,
        "card_number": card_number,
        "total_graded": None,
        "psa10_count": None,
        "psa9_count": None,
        "gem_rate": None,
        "source_url": None,
        "error": None,
    }

    try:
        card_url = search_card(card_name, set_name)
        if not card_url:
            base["error"] = "Card not found on pokedata.io"
            return base

        pop = parse_population_page(card_url)
        base.update(pop)

        if pop["total_graded"] and pop["total_graded"] > 0:
            gem_count = pop["psa10_count"] + pop["psa9_count"]
            base["gem_rate"] = round(gem_count / pop["total_graded"], 4)

        if not pop["parse_success"]:
            base["error"] = "Page found but population data could not be parsed"

    except Exception as e:
        base["error"] = str(e)

    return base


def run(watchlist_path: str) -> pd.DataFrame:
    watchlist = pd.read_csv(watchlist_path)
    rows = []
    total = len(watchlist)

    for i, row in watchlist.iterrows():
        print(f"[{i+1}/{total}] Scraping population: {row['card_name']} ({row['set_name']})")
        pop = fetch_population(
            card_name=row["card_name"],
            set_name=row["set_name"],
            card_number=str(row.get("card_number", "")),
        )
        rows.append(pop)
        time.sleep(RATE_DELAY)  # polite crawling between cards

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
