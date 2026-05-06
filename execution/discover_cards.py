"""
Discover all Pokemon cards across target sets using PokeData.io.

Reads data/target_sets.csv, queries PokeData.io for every card in each set,
and writes .tmp/discovered_cards.csv with columns: card_name, set_name, card_number.

This replaces the static watchlist for broad market scanning.

Usage:
    python execution/discover_cards.py
    python execution/discover_cards.py --target-sets data/target_sets.csv
"""

import argparse
import time
from pathlib import Path

import pandas as pd
import requests

POKEDATA_BASE = "https://pokedata.io"
OUTPUT_FILE = Path(".tmp/discovered_cards.csv")
RATE_DELAY = 0.5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


def get_all_sets() -> list[dict]:
    resp = requests.get(f"{POKEDATA_BASE}/api/sets", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("sets", data.get("data", []))


def find_set_id(target_name: str, all_sets: list[dict]) -> int | None:
    """
    Find PokeData.io set ID for a target set name.
    Uses length-ratio scoring to avoid partial matches (e.g. 'Base Set' → 'Base Set 2').
    """
    target = target_name.lower().strip()

    for s in all_sets:
        if s.get("name", "").lower().strip() == target:
            return s["id"]

    best_id = None
    best_score = 0.0
    for s in all_sets:
        name = s.get("name", "").lower().strip()
        ratio = min(len(target), len(name)) / max(len(target), len(name), 1)
        if ratio < 0.75:
            continue
        in_target = target in name
        in_name = name in target
        score = ratio + (0.3 if in_target or in_name else 0.0)
        if score > best_score:
            best_score = score
            best_id = s["id"]

    return best_id


def get_cards_for_set(set_id: int) -> list[dict]:
    resp = requests.get(
        f"{POKEDATA_BASE}/api/cards",
        params={"set_id": set_id, "limit": 1000},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("cards", data.get("data", []))


def run(target_sets_path: str = "data/target_sets.csv",
        output_path: str = str(OUTPUT_FILE)) -> pd.DataFrame:
    target_df = pd.read_csv(target_sets_path)
    set_names = target_df["set_name"].tolist()

    print(f"Fetching PokeData.io set catalog...")
    all_sets = get_all_sets()
    print(f"  {len(all_sets)} sets in catalog")

    rows = []
    for set_name in set_names:
        set_id = find_set_id(set_name, all_sets)
        if not set_id:
            print(f"  [SKIP] '{set_name}' not found in PokeData.io catalog")
            continue

        print(f"  Fetching {set_name} (set_id={set_id})...", end=" ", flush=True)
        try:
            cards = get_cards_for_set(set_id)
            print(f"{len(cards)} cards")
            for card in cards:
                rows.append({
                    "card_name": card.get("name", ""),
                    "set_name": set_name,
                    "card_number": card.get("number", ""),
                })
            time.sleep(RATE_DELAY)
        except Exception as e:
            print(f"ERROR: {e}")

    df = pd.DataFrame(rows)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"\nDiscovered {len(df)} cards across {len(set_names)} target sets → {output_path}")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discover all cards in target sets")
    parser.add_argument("--target-sets", default="data/target_sets.csv")
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    args = parser.parse_args()
    run(target_sets_path=args.target_sets, output_path=args.output)
