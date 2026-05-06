"""
Fetch PSA grade population data from PSA's public population report (psacard.com/pop).

PSA's search results are loaded via a JSONP API endpoint (urls.specSearch) that is
embedded in the search page's JavaScript. We extract that URL, call it directly,
score results, then fetch the individual card pop page.

First-time setup:
    pip install curl_cffi

Strategy:
  1. GET psacard.com/pop/search to extract the JSONP endpoint URL from page JS.
  2. Call the JSONP endpoint with the card name as search term.
  3. Score JSON results by card_name + set_name similarity.
  4. GET the best-matching pop report page.
  5. Parse grade 1-10 counts from the grade table.
  6. Save debug snapshots to .tmp/debug_psa/ on first card.

Writes results to .tmp/pokedata_population.csv (same path for pipeline compat).

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
"""

import argparse
import json
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
}

# Cached JSONP endpoint URL (extracted once from page JS, reused for all cards)
_spec_search_url: str = ""


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


def _get_spec_search_url(session) -> str:
    """Extract PSA's JSONP search endpoint URL from the pop search page JS."""
    global _spec_search_url
    if _spec_search_url:
        return _spec_search_url

    resp = session.get(f"{BASE_URL}/pop/search", timeout=20)
    _save_debug("psa_search_page.html", resp.text)

    # Look for: specSearch: 'https://...' or specSearch: "/..."
    m = re.search(r"specSearch\s*:\s*['\"]([^'\"]+)['\"]", resp.text)
    if m:
        url = m.group(1)
        _spec_search_url = url if url.startswith("http") else f"{BASE_URL}{url}"
        print(f"  PSA specSearch URL: {_spec_search_url}")
        return _spec_search_url

    # Fallback: look for the full urls object
    m = re.search(r"var\s+urls\s*=\s*(\{[^}]+\})", resp.text)
    if m:
        _save_debug("psa_urls_block.txt", m.group(0))

    return ""


def _call_jsonp(session, endpoint: str, term: str) -> list:
    """Call PSA's JSONP search endpoint and return the parsed result list."""
    resp = session.get(
        endpoint,
        params={"term": term, "includePopOnly": "true", "callback": "cb"},
        timeout=20,
        headers={"Referer": f"{BASE_URL}/pop/search"},
    )
    _save_debug("psa_jsonp_response.txt", resp.text[:5000])

    # JSONP wrapper: cb([...]) → [...]
    text = resp.text.strip()
    m = re.match(r"^[^(]+\((.*)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        # Maybe plain JSON
        try:
            return json.loads(text)
        except Exception:
            return []
    try:
        return json.loads(m.group(1))
    except Exception:
        return []


def _score_result(result: dict, card_name: str, set_name: str, num: str) -> float:
    """Score a PSA API result dict against card_name + set_name."""
    # Common fields: description, title, setName, name, category, spec
    combined = " ".join(str(v) for v in result.values()).lower()
    score = sum(1.0 for w in card_name.lower().split() if len(w) > 2 and w in combined)
    score += sum(0.5 for w in set_name.lower().split() if len(w) > 2 and w in combined)
    if num and num in combined:
        score += 0.5
    return score


def _score_link(text: str, card_name: str, set_name: str, num: str) -> float:
    text = text.lower()
    score = sum(1 for w in card_name.lower().split() if len(w) > 2 and w in text)
    score += sum(0.5 for w in set_name.lower().split() if len(w) > 2 and w in text)
    if num and num in text:
        score += 0.5
    return score


def _parse_grade_table(html: str) -> dict[int, int]:
    """Parse PSA grade counts from a pop report page."""
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
        session = _make_session()

        # Step 1: get JSONP endpoint
        spec_url = _get_spec_search_url(session)
        if not spec_url:
            base["error"] = (
                "Could not extract specSearch URL from PSA page. "
                f"Check {DEBUG_DIR}/psa_search_page.html"
            )
            return base

        # Step 2: call JSONP API with card name
        results = _call_jsonp(session, spec_url, card_name)
        _save_debug("psa_jsonp_results.json", json.dumps(results, indent=2, default=str))

        if not results:
            base["error"] = (
                f"JSONP search returned no results for '{card_name}'. "
                f"Check {DEBUG_DIR}/psa_jsonp_response.txt"
            )
            return base

        # Step 3: score results and pick best
        best_result = None
        best_score = -1.0
        for r in results:
            score = _score_result(r, card_name, set_name, num)
            if score > best_score:
                best_score = score
                best_result = r

        if not best_result or best_score <= 0:
            base["error"] = f"No result scored > 0 for '{card_name} {set_name}'"
            _save_debug("psa_jsonp_results.json", json.dumps(results[:5], indent=2, default=str))
            return base

        # Step 4: extract pop URL from the best result
        # PSA results typically have a 'popUrl', 'url', 'href', or similar field
        pop_url = None
        for key in ("popUrl", "url", "href", "link", "popHref", "setUrl"):
            val = best_result.get(key, "")
            if val:
                pop_url = val if val.startswith("http") else f"{BASE_URL}{val}"
                break

        if not pop_url:
            # Some results embed a specId — construct URL from it
            spec_id = best_result.get("specId") or best_result.get("id")
            if spec_id:
                pop_url = f"{BASE_URL}/pop/show?specid={spec_id}"

        if not pop_url:
            base["error"] = (
                f"Best result found but no pop URL field. "
                f"Keys: {list(best_result.keys())}"
            )
            return base

        base["source_url"] = pop_url

        # Step 5: fetch pop report page
        pop_resp = session.get(pop_url, timeout=20)
        _save_debug("psa_pop_page.html", pop_resp.text)

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
