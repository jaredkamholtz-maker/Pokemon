"""
Fetch PSA grade population data from pokedata.io for each card in the watchlist.

Strategy (no headless browser):
  1. GET /api/sets?tcg=Pokemon to find the set_id for each set.
  2. GET /api/cards?set_id={id} (or similar) to find the card_id.
  3. GET /api/psa?card_id={id} (or similar) to get grade population counts.
  4. Log all probed endpoints + responses on first card to debug_pokedata_api.json
     so that working endpoints can be identified if the guesses miss.

The /api/sets endpoint is confirmed working (captured during earlier Playwright run).
Card and population endpoints are inferred from the URL pattern and probed systematically.

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

OUTPUT_FILE = Path(".tmp/pokedata_population.csv")
DEBUG_FILE = Path(".tmp/debug_pokedata_api.json")
BASE_URL = "https://www.pokedata.io"
RATE_DELAY = 1.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
    "Referer": "https://www.pokedata.io/",
}

# Cache set list for the run so we only fetch it once
_sets_cache: list[dict] | None = None


def get_sets() -> list[dict]:
    global _sets_cache
    if _sets_cache is not None:
        return _sets_cache
    resp = requests.get(f"{BASE_URL}/api/sets?tcg=Pokemon", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    _sets_cache = resp.json()
    return _sets_cache


def find_set_id(set_name: str) -> int | None:
    sets = get_sets()
    target = set_name.lower().strip()

    # 1. Exact match
    for s in sets:
        if s.get("name", "").lower().strip() == target:
            return s["id"]

    # 2. Best partial match scored by length similarity.
    # Using a ratio prevents "Base Set" from matching "Base Set 2"
    # when the actual "Base Set" entry exists elsewhere in the list.
    best_id = None
    best_ratio = 0.0
    for s in sets:
        name = s.get("name", "").lower().strip()
        if target in name or name in target:
            ratio = min(len(target), len(name)) / max(len(target), len(name))
            if ratio > best_ratio:
                best_ratio = ratio
                best_id = s["id"]

    return best_id


def _api_get(url: str, probe_log: list) -> dict | list | None:
    """GET a URL; log to probe_log; return parsed JSON or None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        entry = {"url": url, "status": resp.status_code, "preview": ""}
        if resp.status_code == 200:
            data = resp.json()
            entry["preview"] = json.dumps(data)[:400]
            probe_log.append(entry)
            return data
        probe_log.append(entry)
    except Exception as e:
        probe_log.append({"url": url, "status": "error", "preview": str(e)})
    return None


def find_card_id(set_id: int, card_name: str, card_number: str, probe_log: list) -> int | None:
    """
    Fetch all cards for a set from /api/cards?set_id={id} and filter locally.
    The API ignores name/q query params — filtering must be done client-side.
    Returns the card's internal `id` field.
    """
    url = f"{BASE_URL}/api/cards?set_id={set_id}"
    data = _api_get(url, probe_log)
    if not data or not isinstance(data, list):
        return None

    card_name_lower = card_name.lower().strip()
    target_num = card_number.split("/")[0].strip() if card_number and card_number != "nan" else ""

    # Score each card: name match + optional number match
    best_id = None
    best_score = -1
    for card in data:
        if not isinstance(card, dict):
            continue
        name = card.get("name", "").lower().strip()
        num = str(card.get("num") or card.get("number") or "").strip()

        if name == card_name_lower:
            score = 2
        elif card_name_lower in name or name in card_name_lower:
            score = 1
        else:
            continue

        # Bonus for matching card number
        if target_num and num == target_num:
            score += 1

        if score > best_score:
            best_score = score
            best_id = card.get("id") or card.get("card_id")

    return best_id


def fetch_population_by_card_id(card_id: int, probe_log: list) -> dict[int, int]:
    """Try known API patterns to get PSA grade counts for a card_id."""
    candidates = [
        # stat_url observed in card data — most likely candidate
        f"{BASE_URL}/api/cards/stats?card_id={card_id}",
        f"{BASE_URL}/api/cards/stats?id={card_id}",
        # Other likely patterns
        f"{BASE_URL}/api/psa?card_id={card_id}",
        f"{BASE_URL}/api/population?card_id={card_id}",
        f"{BASE_URL}/api/grades?card_id={card_id}",
        f"{BASE_URL}/api/cards/{card_id}/stats",
        f"{BASE_URL}/api/cards/{card_id}/psa",
        f"{BASE_URL}/api/cards/{card_id}/population",
        f"{BASE_URL}/api/cards/{card_id}/grades",
        f"{BASE_URL}/api/psa/{card_id}",
    ]

    for url in candidates:
        data = _api_get(url, probe_log)
        if data is None:
            continue
        grade_counts = _extract_grade_counts(data)
        if grade_counts:
            return grade_counts

    return {}


def _extract_grade_counts(data) -> dict[int, int]:
    """Parse grade counts from various JSON structures."""
    if not data:
        return {}

    # Pattern A: {"1": 50, "2": 30, ..., "10": 200}
    if isinstance(data, dict):
        numeric = {k: v for k, v in data.items()
                   if re.fullmatch(r"(10|[1-9])", str(k)) and isinstance(v, (int, float))}
        if len(numeric) >= 3:
            return {int(k): int(v) for k, v in numeric.items()}

    # Pattern B: [{grade: X, count: Y}, ...]
    records = data if isinstance(data, list) else (
        data.get("results") or data.get("grades") or data.get("population") or []
    )
    if isinstance(records, list):
        counts: dict[int, int] = {}
        for item in records:
            if not isinstance(item, dict):
                continue
            grade = (item.get("grade") or item.get("psa_grade") or
                     item.get("psaGrade") or item.get("Grade"))
            count = (item.get("count") or item.get("pop") or item.get("population") or
                     item.get("pop_count") or item.get("total"))
            if grade is not None and count is not None:
                try:
                    g = int(float(str(grade)))
                    if 1 <= g <= 10:
                        counts[g] = int(count)
                except (ValueError, TypeError):
                    pass
        if len(counts) >= 3:
            return counts

    return {}


def fetch_population(card_name: str, set_name: str, card_number: str,
                     save_debug: bool = False) -> dict:
    base = {
        "card_name": card_name,
        "set_name": set_name,
        "card_number": card_number,
        "total_graded": None,
        "psa10_count": None,
        "psa9_count": None,
        "gem_rate": None,
        "source_url": f"{BASE_URL}/card/{set_name.lower().replace(' ', '-')}/{card_name.lower().replace(' ', '-')}",
        "error": None,
    }

    probe_log: list = []

    try:
        set_id = find_set_id(set_name)
        if set_id is None:
            base["error"] = f"Set '{set_name}' not found in pokedata.io /api/sets"
            return base

        card_id = find_card_id(set_id, card_name, card_number, probe_log)

        grade_counts: dict[int, int] = {}
        if card_id:
            grade_counts = fetch_population_by_card_id(card_id, probe_log)
        else:
            base["error"] = f"Card '{card_name}' not found via set_id={set_id} API endpoints"

        if save_debug:
            DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
            DEBUG_FILE.write_text(json.dumps(probe_log, indent=2))
            print(f"  Saved API probe log → {DEBUG_FILE}")

        if grade_counts:
            total = sum(grade_counts.values())
            psa10 = grade_counts.get(10, 0)
            psa9 = grade_counts.get(9, 0)
            base["total_graded"] = total
            base["psa10_count"] = psa10
            base["psa9_count"] = psa9
            if total > 0:
                base["gem_rate"] = round((psa9 + psa10) / total, 4)
            base["error"] = None
        elif not base["error"]:
            base["error"] = (
                f"card_id={card_id} found but no population data returned. "
                f"Check {DEBUG_FILE} for probed endpoints."
            )

    except Exception as e:
        base["error"] = str(e)

    return base


def run(watchlist_path: str) -> pd.DataFrame:
    watchlist = pd.read_csv(watchlist_path)
    rows = []
    total = len(watchlist)

    print("Loading pokedata.io set list...")
    try:
        sets = get_sets()
        print(f"  {len(sets)} sets loaded.")
    except Exception as e:
        print(f"  WARNING: Could not load set list: {e}")

    for i, row in watchlist.iterrows():
        print(f"[{i+1}/{total}] Scraping population: {row['card_name']} ({row['set_name']})")
        save_debug = not DEBUG_FILE.exists()
        pop = fetch_population(
            card_name=row["card_name"],
            set_name=row["set_name"],
            card_number=str(row.get("card_number", "")),
            save_debug=save_debug,
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
