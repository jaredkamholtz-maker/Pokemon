"""
Fetch cards with ungraded auction prices and PSA graded prices from PokeData.io.

For each set in target_sets.csv:
  1. Gets all cards via /api/cards?set_id=X
  2. For each card, fetches price data (ungraded, PSA 9, PSA 10) from the card detail API
  3. Filters to: has ungraded price AND (PSA 9 > MIN_GRADED_PRICE OR PSA 10 > MIN_GRADED_PRICE)

Output: .tmp/price_candidates.csv
  card_name, set_name, card_number, card_id, raw_price, psa9_price, psa10_price, source_url

Usage:
    python execution/fetch_pokedata_prices.py
    python execution/fetch_pokedata_prices.py --era scarlet-violet
    python execution/fetch_pokedata_prices.py --sets "151,Evolving Skies"
    python execution/fetch_pokedata_prices.py --min-graded-price 60
"""

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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

POKEDATA_BASE = "https://pokedata.io"
OUTPUT_FILE = Path(".tmp/price_candidates.csv")
DEBUG_DIR = Path(".tmp/debug_pokedata")
RATE_DELAY = 0.3  # seconds between card-level API calls

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# Aliases: our set name → PokeData.io set name(s) to try
ALIASES: dict[str, list[str]] = {
    "151": ["Pokemon Card 151", "Scarlet & Violet 151", "Pokemon 151", "SV 151", "151"],
    "Base Set 2": ["Base Set 2", "Base Set Two"],
}


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None, accept_json: bool = True):
    headers = {**HEADERS, "Accept": "application/json" if accept_json else "text/html,*/*"}
    try:
        return _SESSION.get(url, params=params, headers=headers, timeout=20, allow_redirects=True)
    except Exception:
        return None


# ── Set catalog ────────────────────────────────────────────────────────────────

def get_all_sets() -> list[dict]:
    resp = _get(f"{POKEDATA_BASE}/api/sets")
    if not resp or resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch set catalog: {resp and resp.status_code}")
    data = resp.json()
    return data if isinstance(data, list) else data.get("sets", data.get("data", []))


def find_set_id(target_name: str, all_sets: list[dict]) -> int | None:
    target = target_name.lower().strip()
    by_name = {s.get("name", "").lower().strip(): s["id"] for s in all_sets}

    if target in by_name:
        return by_name[target]

    for alias in ALIASES.get(target_name, []):
        if alias.lower().strip() in by_name:
            return by_name[alias.lower().strip()]

    # Length-ratio fuzzy match (prefer longer names to avoid short subsets winning)
    best_id, best_score = None, 0.0
    for name, sid in by_name.items():
        if len(name) < len(target) * 0.9:
            continue
        ratio = min(len(target), len(name)) / max(len(target), len(name), 1)
        if ratio < 0.75:
            continue
        score = ratio + (0.2 if target in name else 0.0)
        if score > best_score:
            best_score, best_id = score, sid

    return best_id


def get_cards_for_set(set_id: int) -> list[dict]:
    resp = _get(f"{POKEDATA_BASE}/api/cards", params={"set_id": set_id, "limit": 1000})
    if not resp or resp.status_code != 200:
        return []
    data = resp.json()
    return data if isinstance(data, list) else data.get("cards", data.get("data", []))


# ── Price fetching ─────────────────────────────────────────────────────────────

def _log_structure(card: dict, label: str = "card") -> None:
    """Log card object keys on first call to help debug API response shape."""
    debug_path = DEBUG_DIR / "card_structure.json"
    if not debug_path.exists():
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(json.dumps(card, indent=2, default=str))
        print(f"  [DEBUG] {label} structure → {debug_path}")


def _extract_prices_from_card(card: dict) -> dict:
    """
    Try to extract raw/PSA9/PSA10 prices from a card object.
    PokeData.io may use different field names — we try all known variants.
    Returns dict with raw_price, psa9_price, psa10_price (any may be None).
    """
    def _first(*keys):
        for k in keys:
            v = card.get(k)
            if v is not None:
                try:
                    f = float(v)
                    return f if f > 0 else None
                except (TypeError, ValueError):
                    pass
        return None

    return {
        "raw_price":  _first("ungraded_price", "raw_price", "market_price", "price", "avg_price",
                              "ungraded", "loose_price", "recent_price"),
        "psa9_price":  _first("psa9_price", "grade_9_price", "psa_9_price", "grade9", "psa9",
                               "graded_9_price", "grade_9"),
        "psa10_price": _first("psa10_price", "grade_10_price", "psa_10_price", "grade10", "psa10",
                               "graded_10_price", "grade_10", "gem_price"),
    }


def get_card_detail(card_id: int | str) -> dict | None:
    """Fetch individual card detail from PokeData.io API."""
    for path in [f"/api/cards/{card_id}", f"/api/card/{card_id}"]:
        resp = _get(f"{POKEDATA_BASE}{path}")
        if resp and resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                pass
    return None


def _passes_price_filter(
    raw: float,
    psa9: float | None,
    psa10: float | None,
    min_graded_price: float,
    grading_fee: float,
    min_roi: float,
) -> bool:
    """
    Card must pass BOTH conditions to be included:
      1. PSA 9 or PSA 10 price > min_graded_price
      2. Best graded price yields >= min_roi profit after grading fee
         i.e. (best_graded - raw - grading_fee) / (raw + grading_fee) >= min_roi
    """
    best = max(psa9 or 0, psa10 or 0)
    if best <= min_graded_price:
        return False
    cost = raw + grading_fee
    roi = (best - cost) / cost if cost > 0 else 0
    return roi >= min_roi


def fetch_card_prices(
    card: dict,
    set_name: str,
    min_graded_price: float,
    grading_fee: float,
    min_roi: float,
) -> dict | None:
    """
    Fetch prices for a single card. Returns a result dict if the card passes
    both price filters, else None.
    """
    card_id = card.get("id") or card.get("card_id")
    card_name = card.get("name", "")
    card_number = card.get("number", "")

    source_url = f"{POKEDATA_BASE}/card/{card_id}" if card_id else ""

    # 1. Try prices already in the card listing object
    prices = _extract_prices_from_card(card)

    # 2. If missing, fetch the card detail endpoint
    if not any(prices.values()) and card_id:
        detail = get_card_detail(card_id)
        if detail:
            _log_structure(detail, "card_detail")
            prices = _extract_prices_from_card(detail)
            for price_key in ("prices", "market_prices", "price_data"):
                nested = detail.get(price_key)
                if isinstance(nested, dict):
                    nested_prices = _extract_prices_from_card(nested)
                    for k, v in nested_prices.items():
                        if v and not prices.get(k):
                            prices[k] = v

    if not any(prices.values()):
        _log_structure(card, "card_listing")

    raw = prices.get("raw_price")
    psa9 = prices.get("psa9_price")
    psa10 = prices.get("psa10_price")

    if raw is None:
        return None

    if not _passes_price_filter(raw, psa9, psa10, min_graded_price, grading_fee, min_roi):
        return None

    cost = raw + grading_fee
    best = max(psa9 or 0, psa10 or 0)
    roi = round((best - cost) / cost, 4) if cost > 0 else None

    return {
        "card_name": card_name,
        "set_name": set_name,
        "card_number": card_number,
        "card_id": card_id,
        "raw_price": raw,
        "psa9_price": psa9,
        "psa10_price": psa10,
        "roi_at_best_grade": roi,
        "source_url": source_url,
    }


# ── Main run ───────────────────────────────────────────────────────────────────

def run(
    target_sets_path: str = "data/target_sets.csv",
    output_path: str = str(OUTPUT_FILE),
    era: str | None = None,
    sets: list[str] | None = None,
    min_graded_price: float = 60.0,
    grading_fee: float = 25.0,
    min_roi: float = 0.50,
    max_workers: int = 5,
) -> pd.DataFrame:
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

    print(f"Fetching PokeData.io set catalog...")
    all_sets = get_all_sets()
    print(f"  {len(all_sets)} sets in catalog")

    # Collect all cards from target sets
    all_cards: list[tuple[dict, str]] = []  # (card_dict, set_name)
    for set_name in set_names:
        set_id = find_set_id(set_name, all_sets)
        if not set_id:
            close = [s.get("name") for s in all_sets
                     if set_name.lower()[:4] in s.get("name", "").lower()][:5]
            hint = f" — closest: {close}" if close else ""
            print(f"  [SKIP] '{set_name}' not found{hint}")
            continue
        cards = get_cards_for_set(set_id)
        print(f"  {set_name}: {len(cards)} cards")
        for c in cards:
            all_cards.append((c, set_name))
        time.sleep(0.3)

    print(f"\nFetching prices for {len(all_cards)} cards "
          f"(filter: PSA 9/10 > ${min_graded_price:.0f} AND ≥{min_roi*100:.0f}% ROI after ${grading_fee:.0f} grading fee)...")

    results = []
    lock = threading.Lock()
    counter = {"done": 0, "kept": 0}

    def _fetch(card: dict, set_name: str) -> None:
        row = fetch_card_prices(card, set_name, min_graded_price, grading_fee, min_roi)
        time.sleep(RATE_DELAY)
        with lock:
            counter["done"] += 1
            if row:
                results.append(row)
                counter["kept"] += 1
            if counter["done"] % 200 == 0 or counter["done"] == len(all_cards):
                print(f"  [{counter['done']}/{len(all_cards)}] checked "
                      f"— {counter['kept']} candidates so far")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_fetch, card, sname) for card, sname in all_cards]
        for f in as_completed(futures):
            f.result()

    df = pd.DataFrame(results) if results else pd.DataFrame(
        columns=["card_name", "set_name", "card_number", "card_id",
                 "raw_price", "psa9_price", "psa10_price", "source_url"])

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"\n{len(df)} cards pass price filter → {output_path}")

    if df.empty:
        print("\n  NOTE: 0 cards passed the price filter.")
        print("  Most likely PokeData.io's /api/cards endpoint doesn't include prices.")
        print(f"  Check {DEBUG_DIR}/card_structure.json to see available fields,")
        print("  then update _extract_prices_from_card() with the correct field names.")

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch cards with PSA flip potential from PokeData.io")
    parser.add_argument("--target-sets", default="data/target_sets.csv")
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--era", default=None)
    parser.add_argument("--sets", default=None,
                        help="Comma-separated set names, e.g. '151,Evolving Skies'")
    parser.add_argument("--min-graded-price", type=float, default=60.0,
                        help="Min PSA 9 or PSA 10 price to include a card (default: $60)")
    parser.add_argument("--grading-fee", type=float, default=25.0,
                        help="PSA grading fee in USD (default: $25)")
    parser.add_argument("--min-roi", type=float, default=0.50,
                        help="Min ROI after grading fee, e.g. 0.50 = 50%% (default: 0.50)")
    args = parser.parse_args()
    sets_list = [s.strip() for s in args.sets.split(",")] if args.sets else None
    run(
        target_sets_path=args.target_sets,
        output_path=args.output,
        era=args.era,
        sets=sets_list,
        min_graded_price=args.min_graded_price,
        grading_fee=args.grading_fee,
        min_roi=args.min_roi,
    )
