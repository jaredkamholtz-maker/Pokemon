"""
Fetch PSA grade population data from 130point.com.

130point.com aggregates PSA/BGS/CGC population data publicly without requiring
authentication. PSA's own website (psacard.com) blocks all automated requests.

Strategy:
  1. Search 130point.com for the card + set name.
  2. Follow the best-matching result link to the card's grade breakdown page.
  3. Parse the PSA grade distribution table (grades 1-10 + counts).
  4. Save debug HTML to .tmp/debug_pop/ on first card.

Writes results to .tmp/pokedata_population.csv (same path for pipeline compat).

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
    python execution/scrape_pokedata_population.py --watchlist .tmp/discovered_cards.csv
"""

import argparse
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests
    _IMPERSONATE = "chrome120"
except ImportError:
    import requests
    _IMPERSONATE = None

OUTPUT_FILE = Path(".tmp/pokedata_population.csv")
DEBUG_DIR = Path(".tmp/debug_pop")
BASE_URL = "https://130point.com"
RATE_DELAY = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _slugify(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", "-", text)
    return text


def _save_debug(name: str, content: str) -> None:
    path = DEBUG_DIR / name
    if not path.exists():
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _score_link(text: str, href: str, card_name: str, set_name: str, num: str) -> float:
    combined = (text + " " + href).lower()
    score = sum(1.0 for w in card_name.lower().split() if len(w) > 2 and w in combined)
    score += sum(0.3 for w in set_name.lower().split() if len(w) > 2 and w in combined)
    if num and num in combined:
        score += 1.5
    return score


def _parse_grade_table(html: str) -> dict[int, int]:
    """Parse PSA grade counts. 130point shows a grade breakdown table."""
    soup = BeautifulSoup(html, "html.parser")
    grade_counts: dict[int, int] = {}

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True)
        m = re.match(r"^(?:PSA\s*|Grade\s*)?(\d{1,2})$", label, re.I)
        if not m:
            continue
        grade = int(m.group(1))
        if not (1 <= grade <= 10):
            continue
        for cell in cells[1:]:
            raw = cell.get_text(strip=True).replace(",", "")
            try:
                grade_counts[grade] = grade_counts.get(grade, 0) + int(raw)
                break
            except ValueError:
                continue

    if grade_counts:
        return grade_counts

    for el in soup.find_all(class_=re.compile(r"grade|pop|count|psa", re.I)):
        text = el.get_text(" ", strip=True)
        for m in re.finditer(r"\b(10|[1-9])\b[^\d]*?(\d[\d,]*)", text):
            grade = int(m.group(1))
            try:
                count = int(m.group(2).replace(",", ""))
                if 1 <= grade <= 10 and count >= 0:
                    grade_counts[grade] = count
            except ValueError:
                continue

    return grade_counts


def _make_session():
    if _IMPERSONATE:
        return requests.Session(impersonate=_IMPERSONATE)
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _search_130point(card_name: str, set_name: str, num: str) -> tuple[str, str]:
    session = _make_session()

    search_queries = [f"{card_name} {set_name}", card_name]
    if num:
        search_queries.insert(0, f"{card_name} {num} {set_name}")

    for query in search_queries:
        try:
            resp = session.get(f"{BASE_URL}/", params={"q": query}, timeout=15)
        except Exception as e:
            continue

        if resp.status_code != 200:
            continue

        _save_debug("pop_search.html", resp.text)
        soup = BeautifulSoup(resp.text, "html.parser")

        best_href = ""
        best_score = -1.0
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if not any(kw in href.lower() for kw in ["/card", "/sets", "/sales", "pokemon"]):
                continue
            text = a.get_text(" ", strip=True)
            score = _score_link(text, href, card_name, set_name, num)
            if score > best_score:
                best_score = score
                best_href = href

        if best_href and best_score > 0:
            url = best_href if best_href.startswith("http") else f"{BASE_URL}{best_href}"
            return url, resp.text

    return "", ""


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

    num = ""
    if card_number and str(card_number) != "nan":
        num = str(card_number).split("/")[0].strip()

    try:
        card_url, _ = _search_130point(card_name, set_name, num)

        if not card_url:
            base["error"] = (
                f"No match on 130point.com for '{card_name} {set_name}'. "
                f"Check {DEBUG_DIR}/pop_search.html"
            )
            return base

        base["source_url"] = card_url

        session = _make_session()
        pop_resp = session.get(card_url, timeout=15)
        _save_debug("pop_card_page.html", pop_resp.text)

        grade_counts = _parse_grade_table(pop_resp.text)

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
                f"Card page loaded but no grade counts parsed. "
                f"Check {DEBUG_DIR}/pop_card_page.html"
            )

    except Exception as e:
        base["error"] = str(e)

    return base


def run(watchlist_path: str, max_workers: int = 5) -> pd.DataFrame:
    watchlist = pd.read_csv(watchlist_path)
    total = len(watchlist)
    results: list = [None] * total
    counter = {"done": 0}
    lock = threading.Lock()

    def _fetch(i: int, row) -> None:
        pop = fetch_population(
            card_name=row["card_name"],
            set_name=row["set_name"],
            card_number=str(row.get("card_number", "")),
        )
        results[i] = pop
        with lock:
            counter["done"] += 1
            if counter["done"] % 50 == 0 or counter["done"] == total:
                print(f"  [{counter['done']}/{total}] population fetched")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_fetch, i, row)
            for i, (_, row) in enumerate(watchlist.iterrows())
        ]
        for f in as_completed(futures):
            f.result()

    df = pd.DataFrame(results)
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
