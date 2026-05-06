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

# Explicit aliases for sets whose names differ between our target_sets.csv
# and PokeData.io's catalog. Add entries here when [SKIP] is logged.
ALIASES: dict[str, list[str]] = {
    "151":        ["Scarlet & Violet 151", "Pokemon 151", "SV 151", "151"],
    "Base Set 2": ["Base Set 2", "Base Set Two"],
}


def get_all_sets() -> list[dict]:
    resp = requests.get(f"{POKEDATA_BASE}/api/sets", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("sets", data.get("data", []))


def find_set_id(target_name: str, all_sets: list[dict]) -> int | None:
    """
    Find PokeData.io set ID for a target set name.
    Tries exact match first, then aliases, then length-ratio fuzzy match.
    Penalises candidates that are shorter than the target to avoid
    'Base Set' (8 chars) winning over 'Base Set 2' (10 chars).
    """
    target = target_name.lower().strip()
    set_by_name = {s.get("name", "").lower().strip(): s["id"] for s in all_sets}

    # 1. Exact match
    if target in set_by_name:
        return set_by_name[target]

    # 2. Known aliases
    for alias in ALIASES.get(target_name, []):
        alias_lower = alias.lower().strip()
        if alias_lower in set_by_name:
            return set_by_name[alias_lower]

    # 3. Fuzzy ratio match — require candidate length >= target length * 0.9
    #    to avoid shorter subsets winning (e.g. 'Base Set' for target 'Base Set 2')
    best_id = None
    best_score = 0.0
    for name, sid in set_by_name.items():
        if len(name) < len(target) * 0.9:
            continue  # candidate too short, likely a subset
        ratio = min(len(target), len(name)) / max(len(target), len(name), 1)
        if ratio < 0.75:
            continue
        score = ratio + (0.2 if target in name else 0.0)
        if score > best_score:
            best_score = score
            best_id = sid

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
        output_path: str = str(OUTPUT_FILE),
        era: str | None = None,
        sets: list[str] | None = None) -> pd.DataFrame:
    target_df = pd.read_csv(target_sets_path)

    if sets:
        # Exact set names take priority
        target_df = target_df[target_df["set_name"].isin(sets)]
        if target_df.empty:
            raise ValueError(f"None of the requested sets found in {target_sets_path}: {sets}")
    elif era:
        if "era" not in target_df.columns:
            raise ValueError(f"'era' column missing from {target_sets_path}")
        target_df = target_df[target_df["era"] == era]
        if target_df.empty:
            available = target_df["era"].unique().tolist() if "era" in target_df.columns else []
            raise ValueError(f"No sets found for era '{era}'. Available: {available}")

    set_names = target_df["set_name"].tolist()

    print(f"Fetching PokeData.io set catalog...")
    all_sets = get_all_sets()
    print(f"  {len(all_sets)} sets in catalog")

    rows = []
    for set_name in set_names:
        set_id = find_set_id(set_name, all_sets)
        if not set_id:
            # Show closest matches to help diagnose alias issues
            close = [s.get("name") for s in all_sets
                     if set_name.lower()[:4] in s.get("name", "").lower()][:5]
            hint = f" — closest: {close}" if close else ""
            print(f"  [SKIP] '{set_name}' not found in PokeData.io catalog{hint}")
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
    parser.add_argument("--era", default=None,
                        help="Filter by era (vintage, sword-shield, scarlet-violet)")
    parser.add_argument("--sets", default=None,
                        help="Comma-separated set names, e.g. '151,Evolving Skies'")
    args = parser.parse_args()
    sets_list = [s.strip() for s in args.sets.split(",")] if args.sets else None
    run(target_sets_path=args.target_sets, output_path=args.output,
        era=args.era, sets=sets_list)
