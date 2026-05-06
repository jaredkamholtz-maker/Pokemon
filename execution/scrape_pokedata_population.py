"""
Fetch PSA grade population data from PSA's public population report (psacard.com/pop).

pokedata.io's /api/population endpoint requires a logged-in session and returns
500 errors without one. PSA's own pop report is the authoritative source and is
publicly accessible without authentication.

Strategy:
  1. GET psacard.com/pop/search?q={card_name}+{set_name} to find the card.
  2. Score result links by name + set word matches; follow the best one.
  3. Parse the grade table on the individual pop report page.
  4. Save debug HTML on first card to .tmp/debug_psa/ for inspection.

Writes results to .tmp/pokedata_population.csv (same path for pipeline compat).

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
DEBUG_DIR = Path(".tmp/debug_psa")
BASE_URL = "https://www.psacard.com"
RATE_DELAY = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _get(url: str, params: dict = None) -> requests.Response | None:
    try:
        return requests.get(url, params=params, headers=HEADERS,
                            timeout=15, allow_redirects=True)
    except Exception:
        return None


def _save_debug(name: str, content: str) -> None:
    path = DEBUG_DIR / name
    if not path.exists():
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"  Saved debug → {path}")


def search_psa(card_name: str, set_name: str, card_number: str) -> str | None:
    """
    Search PSA pop report and return URL of the best-matching card's pop page.
    Returns None if no suitable result found.
    """
    num = card_number.split("/")[0].strip() if card_number and card_number != "nan" else ""
    query = f"{card_name} {set_name}" + (f" {num}" if num else "")

    resp = _get(f"{BASE_URL}/pop/search", params={"q": query})
    if not resp or resp.status_code != 200:
        return None

    _save_debug("psa_search.html", resp.text)

    soup = BeautifulSoup(resp.text, "html.parser")
    card_words = [w for w in card_name.lower().split() if len(w) > 2]
    set_words = [w for w in set_name.lower().split() if len(w) > 2]

    best_url = None
    best_score = -1.0

    for link in soup.find_all("a", href=re.compile(r"/pop/pokemon", re.I)):
        text = link.get_text(" ", strip=True).lower()
        score = sum(w in text for w in card_words)
        score += sum(w in text for w in set_words) * 0.5
        if num and num in text:
            score += 0.5

        if score > best_score:
            best_score = score
            href = link["href"]
            best_url = href if href.startswith("http") else f"{BASE_URL}{href}"

    return best_url if best_score > 0 else None


def fetch_pop_counts(url: str) -> dict[int, int]:
    """Fetch a PSA pop card page and extract grade 1-10 counts from the table."""
    resp = _get(url)
    if not resp or resp.status_code != 200:
        return {}

    _save_debug("psa_pop_page.html", resp.text)

    soup = BeautifulSoup(resp.text, "html.parser")
    grade_counts: dict[int, int] = {}

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        label = cells[0].get_text(strip=True)
        m = re.match(r"(?:PSA\s*|Grade\s*)?(\d{1,2})$", label.strip(), re.I)
        if not m:
            continue

        grade = int(m.group(1))
        if not (1 <= grade <= 10):
            continue

        for cell in cells[1:]:
            count_text = cell.get_text(strip=True).replace(",", "")
            try:
                count = int(count_text)
                grade_counts[grade] = grade_counts.get(grade, 0) + count
                break
            except ValueError:
                continue

    return grade_counts


def fetch_population(card_name: str, set_name: str, card_number: str) -> dict:
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
        pop_url = search_psa(card_name, set_name, card_number)

        if not pop_url:
            base["error"] = (
                f"Not found in PSA pop search for '{card_name} {set_name}'. "
                f"Check {DEBUG_DIR}/psa_search.html for what PSA returned."
            )
            return base

        base["source_url"] = pop_url
        grade_counts = fetch_pop_counts(pop_url)

        if grade_counts:
            total = sum(grade_counts.values())
            psa10 = grade_counts.get(10, 0)
            psa9 = grade_counts.get(9, 0)
            base["total_graded"] = total
            base["psa10_count"] = psa10
            base["psa9_count"] = psa9
            if total > 0:
                base["gem_rate"] = round((psa9 + psa10) / total, 4)
        else:
            base["error"] = (
                f"Pop page loaded but no grade counts parsed. URL: {pop_url}. "
                f"Check {DEBUG_DIR}/psa_pop_page.html"
            )

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
