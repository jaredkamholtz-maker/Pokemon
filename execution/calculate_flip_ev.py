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


def calculate_breakeven(
    raw_price: float,
    psa9_price: float,
    psa10_price: float,
    grading_fee: float,
    selling_fee_rate: float,
) -> dict:
    """
    When population data is unavailable, compute the minimum gem rate needed
    for the flip to break even. Assumes graded copies split 60% PSA10 / 40% PSA9.

    breakeven_gem_rate < 0.10  → attractive even assuming poor grading luck
    breakeven_gem_rate < 0.20  → reasonable bet with decent cards
    breakeven_gem_rate > 0.40  → high-risk, only works with very gem-worthy copies
    """
    cost = raw_price + grading_fee
    # Weighted PSA price assuming 60/40 PSA10/PSA9 split among gem grades
    psa_weighted = 0.6 * psa10_price + 0.4 * (psa9_price or psa10_price * 0.7)
    psa10_premium = psa10_price - raw_price

    if psa_weighted <= raw_price:
        return {
            "breakeven_gem_rate": None,
            "psa10_premium": round(psa10_premium, 2) if psa10_price else None,
            "error_ev": "PSA10 price not higher than raw — no spread",
        }

    # Solve: gem * psa_weighted * (1-fee) + (1-gem) * raw * (1-fee) = cost
    # gem = (cost/(1-fee) - raw) / (psa_weighted - raw)
    fee_adj = 1 - selling_fee_rate
    numerator = cost / fee_adj - raw_price
    denominator = psa_weighted - raw_price
    breakeven = numerator / denominator if denominator > 0 else None

    return {
        "breakeven_gem_rate": round(breakeven, 4) if breakeven is not None else None,
        "psa10_premium": round(psa10_premium, 2) if psa10_price else None,
        "gem_rate": None,
        "total_graded": None,
        "profit": None,
        "roi": None,
        "error_ev": None,
    }


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
        return {"error_ev": "no population data"}

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
    grading_fee = args.grading_fee if args.grading_fee else load_env_float("GRADING_FEE", 25.0)

    if "psa10_premium" not in df.columns:
        return df.iloc[0:0].copy()  # empty — no spread data at all

    # Track 1: full EV with population data
    has_pop = df["roi"].notna() & df["gem_rate"].notna() & df["total_graded"].notna()
    mask_full = (
        has_pop
        & (df["roi"] >= min_roi)
        & (df["gem_rate"] >= min_gem_rate)
        & (df["total_graded"] >= min_pop)
        & (df["raw_price"] <= max_raw)
        & (df["psa10_price"].notna())
        & (df["psa9_price"].notna())
        & (df["error_ev"].isna())
        & (df["psa10_premium"].fillna(0) > grading_fee * 2)
    )

    # Track 2: breakeven analysis when population data is unavailable
    # Surface cards where you need < 15% gem rate to break even — very low bar
    max_breakeven = load_env_float("MAX_BREAKEVEN_GEM_RATE", 0.15)
    has_breakeven = df["breakeven_gem_rate"].notna() if "breakeven_gem_rate" in df.columns else pd.Series(False, index=df.index)
    mask_breakeven = (
        has_breakeven
        & ~has_pop
        & (df["breakeven_gem_rate"] <= max_breakeven)
        & (df["raw_price"] <= max_raw)
        & (df["psa10_price"].notna())
        & (df["psa10_premium"].fillna(0) > grading_fee * 2)
        & (df["error_ev"].isna())
    )

    return df[mask_full | mask_breakeven].copy()


def run(grading_fee: float = None, selling_fee_rate: float = None, min_roi: float = None) -> pd.DataFrame:
    load_dotenv()
    grading_fee = grading_fee or load_env_float("GRADING_FEE", 25.0)
    selling_fee_rate = selling_fee_rate or load_env_float("SELLING_FEE_RATE", 0.13)

    prices = pd.read_csv(PRICES_FILE)
    pop = pd.read_csv(POP_FILE)

    # Left join: keep all price rows, attach pop data where available
    df = pd.merge(prices, pop, on=["card_name", "set_name", "card_number"],
                  how="left", suffixes=("_price", "_pop"))

    ev_rows = []
    for _, row in df.iterrows():
        # Skip if missing critical prices
        if pd.isna(row.get("raw_price")) or pd.isna(row.get("psa10_price")):
            ev_rows.append({"error_ev": "missing prices"})
            continue

        has_pop = not pd.isna(row.get("total_graded")) and not pd.isna(row.get("psa9_count"))

        if not has_pop:
            # No population data — compute breakeven gem rate from prices alone
            ev = calculate_breakeven(
                raw_price=float(row["raw_price"]),
                psa9_price=float(row["psa9_price"]) if not pd.isna(row.get("psa9_price")) else None,
                psa10_price=float(row["psa10_price"]),
                grading_fee=grading_fee,
                selling_fee_rate=selling_fee_rate,
            )
        else:
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

    # Ensure key columns exist
    for col in ("roi", "breakeven_gem_rate", "gem_rate"):
        if col not in df.columns:
            df[col] = pd.NA

    # Sort: cards with real ROI first (desc), then by breakeven_gem_rate asc
    # (lower breakeven = less gem rate needed = safer opportunity)
    df["_sort_roi"] = df["roi"].fillna(0)
    df["_sort_be"] = df["breakeven_gem_rate"].fillna(1.0)
    df_sorted = df.sort_values(
        ["_sort_roi", "_sort_be"],
        ascending=[False, True],
        na_position="last",
    ).drop(columns=["_sort_roi", "_sort_be"])

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
