"""
Fetch PSA grade population data from pokedata.io for each card in the watchlist.

Strategy (no headless browser needed):
  1. Fetch the current Next.js buildId from the pokedata.io homepage.
  2. For each card, GET /_next/data/{buildId}/card/{set-slug}/{card-slug}.json
     which returns server-rendered page props as JSON.
  3. Recursively search props for PSA grade count data.
  4. If props don't contain population, probe direct /api/ endpoints using
     any card_id/set_id found in the props.
  5. Save first card's full props to .tmp/debug_pokedata_card_props.json
     so the data structure can be inspected if extraction fails.

Writes results to .tmp/pokedata_population.csv

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
"""

import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

OUTPUT_FILE = Path(".tmp/pokedata_population.csv")
DEBUG_FILE = Path(".tmp/debug_pokedata_card_props.json")
BASE_URL = "https://www.pokedata.io"
RATE_DELAY = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.pokedata.io/",
}

_build_id: str | None = None


def get_build_id() -> str:
    """Fetch the current Next.js buildId from the pokedata.io homepage."""
    global _build_id
    if _build_id:
        return _build_id

    resp = requests.get(BASE_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if not script or not script.string:
        raise RuntimeError("Could not find __NEXT_DATA__ in pokedata.io homepage")

    data = json.loads(script.string)
    _build_id = data["buildId"]
    return _build_id


def slugify(text: str) -> str:
    text = text.lower().encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9\s\-]", "", text)
    text = re.sub(r"[\s\-]+", "-", text.strip())
    return text


def fetch_card_props(set_slug: str, card_slug: str) -> dict:
    """Fetch card page props via Next.js JSON data route (no browser needed)."""
    build_id = get_build_id()
    url = f"{BASE_URL}/_next/data/{build_id}/card/{set_slug}/{card_slug}.json"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return resp.json().get("pageProps", {})


def _search_for_grade_data(obj, depth: int = 0) -> dict[int, int]:
    """Recursively search a nested JSON structure for PSA grade counts."""
    if depth > 8 or not obj:
        return {}

    if isinstance(obj, dict):
        # Pattern A: {"1": 50, "2": 30, ..., "10": 200} — grade keys directly on dict
        numeric_keys = {
            k: v for k, v in obj.items()
            if re.fullmatch(r"(10|[1-9])", str(k)) and isinstance(v, (int, float))
        }
        if len(numeric_keys) >= 3:
            return {int(k): int(v) for k, v in numeric_keys.items()}

        # Pattern B: recurse into keys that suggest population data first
        pop_keys = [k for k in obj if any(
            kw in k.lower() for kw in ("psa", "pop", "grade", "gem", "graded")
        )]
        for k in pop_keys:
            result = _search_for_grade_data(obj[k], depth + 1)
            if result:
                return result

        # Recurse all other values
        for v in obj.values():
            result = _search_for_grade_data(v, depth + 1)
            if result:
                return result

    elif isinstance(obj, list):
        # Pattern C: [{grade: X, count: Y}, ...] list of grade objects
        from_list: dict[int, int] = {}
        for item in obj:
            if isinstance(item, dict):
                grade = (item.get("grade") or item.get("psa_grade")
                         or item.get("psaGrade") or item.get("Grade"))
                count = (item.get("count") or item.get("pop") or item.get("population")
                         or item.get("pop_count") or item.get("popCount") or item.get("Count"))
                if grade is not None and count is not None:
                    try:
                        g = int(float(str(grade)))
                        if 1 <= g <= 10:
                            from_list[g] = int(count)
                    except (ValueError, TypeError):
                        pass
        if len(from_list) >= 3:
            return from_list

        # Recurse into list items
        for item in obj:
            result = _search_for_grade_data(item, depth + 1)
            if result:
                return result

    return {}


def _try_direct_api(card_id) -> dict[int, int]:
    """Probe known direct API patterns using the card_id from page props."""
    if not card_id:
        return {}

    candidates = [
        f"{BASE_URL}/api/psa?card_id={card_id}",
        f"{BASE_URL}/api/population?card_id={card_id}",
        f"{BASE_URL}/api/cards/{card_id}/psa",
        f"{BASE_URL}/api/cards/{card_id}/population",
        f"{BASE_URL}/api/cards/{card_id}/grades",
        f"{BASE_URL}/api/grades?card_id={card_id}",
    ]
    for url in candidates:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                result = _search_for_grade_data(resp.json())
                if result:
                    return result
        except Exception:
            pass
    return {}


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

    set_slug = slugify(set_name)
    card_slug = slugify(card_name)
    base["source_url"] = f"{BASE_URL}/card/{set_slug}/{card_slug}"

    try:
        props = fetch_card_props(set_slug, card_slug)

        # Save first card's full props for debugging the data structure
        if not DEBUG_FILE.exists():
            DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
            DEBUG_FILE.write_text(json.dumps(props, indent=2, default=str))
            print(f"  Saved card props → {DEBUG_FILE}")

        if not props:
            base["error"] = "Card page 404 — slug may not match pokedata.io URL convention"
            return base

        grade_counts = _search_for_grade_data(props)

        if not grade_counts:
            card_id = (props.get("cardId") or props.get("card_id")
                       or props.get("id") or props.get("cardInfo", {}).get("id"))
            grade_counts = _try_direct_api(card_id)

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
                f"Population data not found in page props or direct API. "
                f"Inspect {DEBUG_FILE} to identify the correct structure."
            )

    except Exception as e:
        base["error"] = str(e)

    return base


def run(watchlist_path: str) -> pd.DataFrame:
    watchlist = pd.read_csv(watchlist_path)
    rows = []
    total = len(watchlist)

    print("Fetching pokedata.io buildId...")
    try:
        build_id = get_build_id()
        print(f"  buildId: {build_id}")
    except Exception as e:
        print(f"  WARNING: Could not fetch buildId: {e}")

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
