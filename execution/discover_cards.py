"""
Discover all Pokemon cards across target sets using the Pokemon TCG API.

api.pokemontcg.io is a free, public API with no bot detection and no API key
required (though adding one via POKEMONTCG_API_KEY raises rate limits from
1,000 to 20,000 req/day). It returns clean card data including name, number,
rarity — exactly what we need to feed into the AI pre-filter.

Reads data/target_sets.csv, queries the API for every card in each set,
and writes .tmp/discovered_cards.csv: card_name, set_name, card_number, rarity.

Usage:
    python execution/discover_cards.py
    python execution/discover_cards.py --era scarlet-violet
    python execution/discover_cards.py --sets "151,Evolving Skies"
"""

import argparse
import os
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome124")
    _USE_CFFI = True
except ImportError:
    import requests as _req_fallback
    _SESSION = _req_fallback.Session()
    _USE_CFFI = False

API_BASE = "https://api.pokemontcg.io/v2"
OUTPUT_FILE = Path(".tmp/discovered_cards.csv")
PAGE_SIZE = 250
RATE_DELAY = 0.3

# Maps our set_name → Pokemon TCG API set ID.
# Full list at: https://api.pokemontcg.io/v2/sets
SET_ID_MAP: dict[str, str] = {
    # XY era
    "XY":               "xy1",
    "Flashfire":        "xy2",
    "Furious Fists":    "xy3",
    "Phantom Forces":   "xy4",
    "Primal Clash":     "xy5",
    "Roaring Skies":    "xy6",
    "Ancient Origins":  "xy7",
    "BREAKthrough":     "xy8",
    "BREAKpoint":       "xy9",
    "Generations":      "g1",
    "Fates Collide":    "xy10",
    "Steam Siege":      "xy11",
    "Evolutions":       "xy12",
    # Sword & Shield era
    "Darkness Ablaze":  "swsh3",
    "Vivid Voltage":    "swsh4",
    "Celebrations":     "cel25",
    "Evolving Skies":   "swsh7",
    "Fusion Strike":    "swsh8",
    "Brilliant Stars":  "swsh9",
    "Lost Origin":      "swsh11",
    "Silver Tempest":   "swsh12",
    "Crown Zenith":     "swsh12pt5",
    # Scarlet & Violet era
    "Paldea Evolved":       "sv2",
    "Obsidian Flames":      "sv3",
    "151":                  "sv3pt5",
    "Paradox Rift":         "sv4",
    "Temporal Forces":      "sv5",
    "Twilight Masquerade":  "sv6",
    "Surging Sparks":       "sv8",
}


def _get_headers() -> dict:
    load_dotenv()
    headers = {"Accept": "application/json"}
    api_key = os.environ.get("POKEMONTCG_API_KEY")
    if api_key:
        headers["X-Api-Key"] = api_key
    return headers


def get_cards_for_set(set_id: str, max_retries: int = 3) -> list[dict]:
    """Fetch all cards for a set ID, handling pagination and retrying on timeout."""
    headers = _get_headers()
    cards = []
    page = 1
    while True:
        resp = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = _SESSION.get(
                    f"{API_BASE}/cards",
                    params={"q": f"set.id:{set_id}", "pageSize": PAGE_SIZE, "page": page,
                            "select": "id,name,number,rarity"},
                    headers=headers,
                    timeout=30,
                )
                break  # success
            except Exception as e:
                wait = attempt * 5
                if attempt < max_retries:
                    print(f"  Timeout on page {page} (attempt {attempt}/{max_retries}) — retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"  Request failed after {max_retries} attempts on page {page}: {e}")

        if resp is None:
            break

        if resp.status_code == 429:
            print(f"  Rate limited — waiting 10s...")
            time.sleep(10)
            continue
        if resp.status_code != 200:
            print(f"  API returned {resp.status_code}: {resp.text[:100]}")
            break

        data = resp.json()
        batch = data.get("data", [])
        cards.extend(batch)
        total = data.get("totalCount", len(cards))
        if len(cards) >= total or not batch:
            break
        page += 1
        time.sleep(RATE_DELAY)

    return cards


def run(target_sets_path: str = "data/target_sets.csv",
        output_path: str = str(OUTPUT_FILE),
        era: str | None = None,
        sets: list[str] | None = None) -> pd.DataFrame:
    load_dotenv()
    target_df = pd.read_csv(target_sets_path)

    if sets:
        target_df = target_df[target_df["set_name"].isin(sets)]
        if target_df.empty:
            raise ValueError(f"None of the requested sets found in {target_sets_path}: {sets}")
    elif era:
        if "era" not in target_df.columns:
            raise ValueError(f"'era' column missing from {target_sets_path}")
        target_df = target_df[target_df["era"] == era]
        if target_df.empty:
            raise ValueError(f"No sets found for era '{era}'")

    set_names = target_df["set_name"].tolist()

    has_key = bool(os.environ.get("POKEMONTCG_API_KEY"))
    print(f"Querying Pokemon TCG API (api_key={'yes' if has_key else 'no — 1k req/day limit'})")

    rows = []
    skipped = []
    for set_name in set_names:
        set_id = SET_ID_MAP.get(set_name)
        if not set_id:
            print(f"  [SKIP] '{set_name}' — no set ID mapping. Add to SET_ID_MAP in discover_cards.py")
            skipped.append(set_name)
            continue

        print(f"  {set_name} ({set_id})...", end=" ", flush=True)
        cards = get_cards_for_set(set_id)
        print(f"{len(cards)} cards")

        for card in cards:
            rows.append({
                "card_name":   card.get("name", ""),
                "set_name":    set_name,
                "card_number": card.get("number", ""),
                "rarity":      card.get("rarity", ""),
            })
        time.sleep(RATE_DELAY)

    if skipped:
        print(f"\n  {len(skipped)} sets skipped (no ID mapping): {skipped}")

    df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["card_name", "set_name", "card_number", "rarity"])
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"\nDiscovered {len(df)} cards across {len(set_names) - len(skipped)} sets → {output_path}")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Discover all cards in target sets via Pokemon TCG API")
    parser.add_argument("--target-sets", default="data/target_sets.csv")
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--era", default=None,
                        help="Filter by era (mega-evolution, sword-shield, scarlet-violet)")
    parser.add_argument("--sets", default=None,
                        help="Comma-separated set names, e.g. '151,Evolving Skies'")
    args = parser.parse_args()
    sets_list = [s.strip() for s in args.sets.split(",")] if args.sets else None
    run(target_sets_path=args.target_sets, output_path=args.output,
        era=args.era, sets=sets_list)
