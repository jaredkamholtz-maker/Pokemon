"""
Fetch PSA grade population data from PSA's public population report (psacard.com/pop).

PSA's pop search page is server-side rendered. However, PSA uses Cloudflare Bot
Management which detects Python requests via TLS fingerprint and serves empty results.

Fix: use curl_cffi which impersonates Chrome's TLS fingerprint at the socket level,
bypassing Cloudflare's JA3/JA4 fingerprint checks.

First-time setup:
    pip install curl_cffi

Strategy:
  1. GET psacard.com/pop/search?q={card_name} with Chrome TLS impersonation.
  2. Parse #tableResults rows; score each link on card_name + set_name similarity.
  3. Follow the best-scoring link to the card's pop report page.
  4. Parse the grade table for grades 1-10 counts.
  5. Save HTML snapshots to .tmp/debug_psa/ on first card for inspection.

Writes results to .tmp/pokedata_population.csv (same path for pipeline compat).

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
"""

import argparse
import re
import time
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests
    _IMPERSONATE = "chrome120"
except ImportError:
    import requests
    _IMPERSONATE = None

OUTPUT_FILE = Path(".tmp/pokedata_population.csv")
DEBUG_DIR = Path(".tmp/debug_psa")
BASE_URL = "https://www.psacard.com"
RATE_DELAY = 3.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _make_session():
    if _IMPERSONATE:
        return requests.Session(impersonate=_IMPERSONATE)
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _save_debug(name: str, content: str) -> None:
    path = DEBUG_DIR / name
    if not path.exists():
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"  Saved debug → {path}")


def _score_link(text: str, card_name: str, set_name: str, num: str) -> float:
    text = text.lower()
    score = sum(1 for w in card_name.lower().split() if len(w) > 2 and w in text)
    score += sum(0.5 for w in set_name.lower().split() if len(w) > 2 and w in text)
    if num and num in text:
        score += 0.5
    return score


def _parse_grade_table(html: str) -> dict[int, int]:
    """Parse PSA grade counts from a rendered pop report page."""
    soup = BeautifulSoup(html, "html.parser")
    grade_counts: dict[int, int] = {}

    # Strategy 1: table rows where first cell is a grade number
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

    # Strategy 2: elements with grade-related class names
    for el in soup.find_all(class_=re.compile(r"grade|pop|count", re.I)):
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


def _search_psa(card_name: str, set_name: str, num: str) -> tuple[str, str]:
    """
    Search PSA pop report for a card. Returns (best_pop_url, search_html).
    Tries card-name-only first (more permissive), then card+set if needed.
    """
    session = _make_session()

    queries = [
        card_name,
        f"{card_name} {set_name}",
    ]
    if num:
        queries.insert(0, f"{card_name} {num}")

    for query in queries:
        search_url = f"{BASE_URL}/pop/search?q={quote(query)}"
        try:
            resp = session.get(search_url, timeout=20)
        except Exception as e:
            print(f"  Request failed for '{query}': {e}")
            continue

        if resp.status_code != 200:
            continue

        html = resp.text
        _save_debug("psa_search.html", html)

        soup = BeautifulSoup(html, "html.parser")

        # PSA results are in #tableResults; links go to /pop/pokemon-cards/...
        # or other /pop/... subpaths. Score all /pop/ links that are card pages.
        links = soup.find_all("a", href=re.compile(r"/pop/[a-z]"))
        best_href = None
        best_score = -1.0

        for link in links:
            href = link.get("href", "")
            # Skip pure navigation links (no year/slug depth)
            if href in ("/pop", "/pop/", "/pop/comics", "/pop/magazines",
                        "/pop/playersearch"):
                continue
            text = link.get_text(" ", strip=True)
            score = _score_link(text, card_name, set_name, num)
            if score > best_score:
                best_score = score
                best_href = href

        if best_href and best_score > 0:
            pop_url = best_href if best_href.startswith("http") else f"{BASE_URL}{best_href}"
            return pop_url, html

        # Also check if the page itself IS a pop report (direct redirect)
        if soup.find(id="tableGrades") or soup.find(class_=re.compile(r"pop-report|grade")):
            return search_url, html

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

    num = card_number.split("/")[0].strip() if card_number and card_number != "nan" else ""

    try:
        pop_url, search_html = _search_psa(card_name, set_name, num)

        if not pop_url:
            base["error"] = (
                f"No matching PSA pop page found for '{card_name} {set_name}'. "
                f"Check {DEBUG_DIR}/psa_search.html"
            )
            return base

        base["source_url"] = pop_url

        # Fetch the pop report page
        session = _make_session()
        try:
            pop_resp = session.get(pop_url, timeout=20)
            pop_html = pop_resp.text
        except Exception as e:
            base["error"] = f"Failed to fetch pop page {pop_url}: {e}"
            return base

        _save_debug("psa_pop_page.html", pop_html)

        grade_counts = _parse_grade_table(pop_html)

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
                f"Pop page loaded but no grade counts parsed. "
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
