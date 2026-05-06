"""
Fetch PSA grade population data from PSA's public population report (psacard.com/pop).

Strategy: direct URL navigation — no search or JSONP needed.

PSA pop pages follow: /pop/pokemon-cards/{year}/{set-slug}/{card-slug}-{num}/{spec-id}/
We navigate to the set listing page, find the matching card link by scoring on
card name + number, then fetch the grade table from the card's pop report page.

First-time setup:
    pip install curl_cffi

Writes results to .tmp/pokedata_population.csv (same path for pipeline compat).

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
"""

import argparse
import re
import time
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

# PSA set slug + year for each set in the watchlist.
# Format: "Watchlist set_name" → (year, "psa-url-slug")
PSA_SET_MAP: dict[str, tuple[int, str]] = {
    "Base Set":         (1999, "base-set"),
    "Base Set 2":       (2000, "base-set-2"),
    "Jungle":           (1999, "jungle"),
    "Fossil":           (1999, "fossil"),
    "Team Rocket":      (2000, "team-rocket"),
    "Aquapolis":        (2003, "aquapolis"),
    "Celebrations":     (2021, "celebrations"),
    "Flashfire":        (2014, "flashfire"),
    "Celestial Storm":  (2018, "celestial-storm"),
    "Vivid Voltage":    (2020, "vivid-voltage"),
    "Darkness Ablaze":  (2020, "darkness-ablaze"),
    "Fusion Strike":    (2021, "fusion-strike"),
    "Evolving Skies":   (2021, "evolving-skies"),
    "151":              (2023, "scarlet-violet-151"),
    "Obsidian Flames":  (2023, "obsidian-flames"),
}


def _make_session():
    if _IMPERSONATE:
        return requests.Session(impersonate=_IMPERSONATE)
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _slugify(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^a-z0-9\s\-]", "", text)
    text = re.sub(r"[\s\-]+", "-", text)
    return text


def _save_debug(name: str, content: str) -> None:
    path = DEBUG_DIR / name
    if not path.exists():
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"  Saved debug → {path}")


def _score_link(text: str, href: str, card_name: str, num: str) -> float:
    combined = (text + " " + href).lower()
    score = sum(1.0 for w in card_name.lower().split() if len(w) > 2 and w in combined)
    if num and num in combined:
        score += 1.5  # card number is highly specific
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

    # Fallback: elements with grade-related class names
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


def _find_card_on_set_page(session, set_url: str, card_name: str,
                           num: str) -> str:
    """
    Fetch a PSA set listing page and return the URL of the best-matching card.
    PSA set pages list cards as links under /pop/pokemon-cards/{year}/{set}/{card}-{num}/
    """
    resp = session.get(set_url, timeout=20)
    print(f"  Set page status: {resp.status_code} ({set_url})")
    _save_debug("psa_set_page.html", resp.text)

    if resp.status_code != 200:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    # Card links are one level deeper in the URL hierarchy
    set_path = set_url.replace(BASE_URL, "").rstrip("/")
    pattern = re.compile(rf"^{re.escape(set_path)}/[^/]+/?$")

    best_href = ""
    best_score = -1.0

    for a in soup.find_all("a", href=pattern):
        href = a.get("href", "")
        text = a.get_text(" ", strip=True)
        score = _score_link(text, href, card_name, num)
        if score > best_score:
            best_score = score
            best_href = href

    if best_href and best_score > 0:
        return best_href if best_href.startswith("http") else f"{BASE_URL}{best_href}"
    return ""


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

    set_info = PSA_SET_MAP.get(set_name)
    if not set_info:
        base["error"] = (
            f"Set '{set_name}' not in PSA_SET_MAP. "
            "Add it to execution/scrape_pokedata_population.py."
        )
        return base

    year, set_slug = set_info
    set_url = f"{BASE_URL}/pop/pokemon-cards/{year}/{set_slug}"

    try:
        session = _make_session()

        # Step 1: find the card on the set listing page
        card_url = _find_card_on_set_page(session, set_url, card_name, num)

        if not card_url:
            # The set page may list cards one level deeper (variant page).
            # Try constructing URL directly: /pop/pokemon-cards/{year}/{set}/{card}-{num}/
            card_slug = _slugify(card_name)
            direct = f"{set_url}/{card_slug}-{num}" if num else f"{set_url}/{card_slug}"
            print(f"  Trying direct URL: {direct}")
            resp = session.get(direct, timeout=20)
            if resp.status_code == 200 and "grade" in resp.text.lower():
                card_url = direct
                _save_debug("psa_pop_page.html", resp.text)
                grade_counts = _parse_grade_table(resp.text)
            else:
                base["error"] = (
                    f"No matching card found on PSA set page {set_url}. "
                    f"Check {DEBUG_DIR}/psa_set_page.html"
                )
                return base
        else:
            # Step 2: fetch the card's pop report page
            print(f"  Card URL: {card_url}")
            pop_resp = session.get(card_url, timeout=20)
            _save_debug("psa_pop_page.html", pop_resp.text)
            grade_counts = _parse_grade_table(pop_resp.text)

        base["source_url"] = card_url

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
