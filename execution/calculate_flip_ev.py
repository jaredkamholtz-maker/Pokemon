"""
Calculate expected value and ROI for buying a raw Pokemon card, grading it (PSA),
and reselling it.

Inputs (CSVs in .tmp/):
  - tcgplayer_prices.csv    (from fetch_tcgplayer_prices.py)
  - pokedata_population.csv (from scrape_pokedata_population.py)

Output:
  - .tmp/flip_opportunities.csv  — all cards with EV columns, sorted by ROI desc

Usage:
    python execution/calculate_flip_ev.py
    python execution/calculate_flip_ev.py --grading-fee 25 --min-roi 0.15
"""

import argparse
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

PRICES_FILE = Path(".tmp/tcgplayer_prices.csv")
POP_FILE = Path(".tmp/pokedata_population.csv")
OUTPUT_FILE = Path(".tmp/flip_opportunities.csv")


def load_env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    return float(val) if val else default


def calculate_ev(
    raw_price: float,
    psa9_price: float,
    psa10_price: float,
    total_graded: int,
    psa9_count: int,
    psa10_count: int,
    grading_fee: float,
    selling_fee_rate: float,
) -> dict:
    """
    Core EV calculation. All prices in USD.

    Returns a dict with:
      gem_rate, psa10_rate, psa9_rate, expected_revenue,
      cost, selling_fee, profit, roi, recommendation
    """
    if not total_graded or total_graded == 0:
        return {"error": "no population data"}

    psa10_rate = psa10_count / total_graded
    psa9_rate = psa9_count / total_graded
    below_gem_rate = 1.0 - psa10_rate - psa9_rate
    gem_rate = psa10_rate + psa9_rate

    # Cards graded below 9 are worth approximately raw market price (conservative estimate)
    below_gem_value = raw_price if raw_price else 0.0

    expected_revenue = (
        (psa10_rate * psa10_price)
        + (psa9_rate * psa9_price)
        + (below_gem_rate * below_gem_value)
    )

    cost = raw_price + grading_fee
    selling_fee = expected_revenue * selling_fee_rate
    profit = expected_revenue - cost - selling_fee
    roi = profit / cost if cost > 0 else 0.0

    # Upside-only scenario: what if YOU only submit near-mint copies?
    # Model: assume you can achieve gem rate of min(gem_rate * 1.5, 0.95)
    optimistic_gem_rate = min(gem_rate * 1.5, 0.95)
    opt_psa10 = optimistic_gem_rate * 0.6  # rough split: ~60% of gems are 10s
    opt_psa9 = optimistic_gem_rate * 0.4
    opt_revenue = (
        (opt_psa10 * psa10_price)
        + (opt_psa9 * psa9_price)
        + ((1 - optimistic_gem_rate) * below_gem_value)
    )
    opt_profit = opt_revenue - cost - (opt_revenue * selling_fee_rate)
    opt_roi = opt_profit / cost if cost > 0 else 0.0

    return {
        "gem_rate": round(gem_rate, 4),
        "psa10_rate": round(psa10_rate, 4),
        "psa9_rate": round(psa9_rate, 4),
        "expected_revenue": round(expected_revenue, 2),
        "cost": round(cost, 2),
        "selling_fee": round(selling_fee, 2),
        "profit": round(profit, 2),
        "roi": round(roi, 4),
        "optimistic_roi": round(opt_roi, 4),
        "psa10_premium": round(psa10_price - raw_price, 2) if psa10_price else None,
        "error": None,
    }


def apply_filters(df: pd.DataFrame, args) -> pd.DataFrame:
    """Return rows that pass all flip-viability filters."""
    load_dotenv()
    min_roi = args.min_roi if args.min_roi is not None else load_env_float("MIN_ROI", 0.20)
    min_gem_rate = load_env_float("MIN_GEM_RATE", 0.30)
    min_pop = int(load_env_float("MIN_POP_COUNT", 50))
    max_raw = load_env_float("MAX_RAW_PRICE", 500.0)

    mask = (
        df["roi"].notna()
        & (df["roi"] >= min_roi)
        & (df["gem_rate"] >= min_gem_rate)
        & (df["total_graded"] >= min_pop)
        & (df["raw_price"] <= max_raw)
        & (df["psa10_price"].notna())
        & (df["psa9_price"].notna())
        & (df["error_ev"].isna())
    )

    # Must have meaningful spread: PSA 10 premium > 2× grading fee
    grading_fee = args.grading_fee if args.grading_fee else load_env_float("GRADING_FEE", 25.0)
    mask &= df["psa10_premium"].fillna(0) > grading_fee * 2

    return df[mask].copy()


def run(grading_fee: float = None, selling_fee_rate: float = None, min_roi: float = None) -> pd.DataFrame:
    load_dotenv()
    grading_fee = grading_fee or load_env_float("GRADING_FEE", 25.0)
    selling_fee_rate = selling_fee_rate or load_env_float("SELLING_FEE_RATE", 0.13)

    prices = pd.read_csv(PRICES_FILE)
    pop = pd.read_csv(POP_FILE)

    # Merge on card_name + set_name
    df = pd.merge(prices, pop, on=["card_name", "set_name", "card_number"], how="inner", suffixes=("_price", "_pop"))

    ev_rows = []
    for _, row in df.iterrows():
        # Skip if missing critical prices
        if pd.isna(row.get("raw_price")) or pd.isna(row.get("psa10_price")):
            ev_rows.append({"error_ev": "missing prices"})
            continue
        if pd.isna(row.get("total_graded")) or pd.isna(row.get("psa9_count")):
            ev_rows.append({"error_ev": "missing population"})
            continue

        ev = calculate_ev(
            raw_price=float(row["raw_price"]),
            psa9_price=float(row["psa9_price"]) if not pd.isna(row.get("psa9_price")) else float(row["raw_price"]) * 1.2,
            psa10_price=float(row["psa10_price"]),
            total_graded=int(row["total_graded"]),
            psa9_count=int(row["psa9_count"]),
            psa10_count=int(row["psa10_count"]),
            grading_fee=grading_fee,
            selling_fee_rate=selling_fee_rate,
        )
        ev_rows.append(ev)

    ev_df = pd.DataFrame(ev_rows)
    df = pd.concat([df.reset_index(drop=True), ev_df.reset_index(drop=True)], axis=1)

    # Flag low-data cards
    df["low_data"] = df["total_graded"].fillna(0) < 50

    # Sort by ROI descending
    df_sorted = df.sort_values("roi", ascending=False, na_position="last")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    df_sorted.to_csv(OUTPUT_FILE, index=False)
    print(f"Saved full analysis ({len(df_sorted)} cards) → {OUTPUT_FILE}")

    return df_sorted


class _Args:
    def __init__(self, grading_fee=None, min_roi=None):
        self.grading_fee = grading_fee
        self.min_roi = min_roi


def get_opportunities(df: pd.DataFrame, grading_fee: float = None, min_roi: float = None) -> pd.DataFrame:
    """Filter df to only the cards that pass all viability criteria."""
    args = _Args(grading_fee=grading_fee, min_roi=min_roi)
    return apply_filters(df, args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--grading-fee", type=float, default=None,
                        help="PSA grading fee in USD (overrides .env GRADING_FEE)")
    parser.add_argument("--selling-fee-rate", type=float, default=None,
                        help="Platform selling fee rate e.g. 0.13 (overrides .env)")
    parser.add_argument("--min-roi", type=float, default=None,
                        help="Minimum ROI filter e.g. 0.20 for 20%%")
    args = parser.parse_args()

    result = run(
        grading_fee=args.grading_fee,
        selling_fee_rate=args.selling_fee_rate,
        min_roi=args.min_roi,
    )

    opps = get_opportunities(result, grading_fee=args.grading_fee, min_roi=args.min_roi)
    if opps.empty:
        print("\nNo cards passed all filters today.")
    else:
        print(f"\n{len(opps)} opportunity cards found:")
        cols = ["card_name", "set_name", "raw_price", "psa9_price", "psa10_price",
                "gem_rate", "total_graded", "profit", "roi"]
        print(opps[[c for c in cols if c in opps.columns]].to_string(index=False))
