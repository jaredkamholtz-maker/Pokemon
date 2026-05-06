"""
AI pre-filter: use Claude to identify cards with PSA flip potential
before expensive price/population scraping.

Sends discovered cards to Claude in batches of 300. Claude keeps only
cards likely to have a meaningful PSA 9/10 grading premium — holos,
full arts, chase rares, high-demand Pokemon. Commons, trainers, and
bulk rares are excluded.

Typical result: 3,000 cards → 400-600 high-potential cards, cutting
scraping time by 80-85%.

Requires ANTHROPIC_API_KEY in .env.

Usage:
    python execution/filter_cards_ai.py
    python execution/filter_cards_ai.py --input .tmp/discovered_cards.csv
"""

import argparse
import json
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 300

SYSTEM_PROMPT = """You are a Pokemon card grading investment expert.

Given a numbered list of cards, return the indices of cards worth buying raw and submitting to PSA for grading.

KEEP cards that are likely to have a PSA 9/10 price premium:
- Holofoil rares (all sets — holo rares always carry a grading premium)
- Full art, alternate art, illustration rare (IR), special illustration rare (SIR)
- Secret rares, ultra rares, hyper rares, rainbow rares
- VMAX, VSTAR, ex, GX, EX, Mega (M), BREAK evolution cards
- Cards featuring high-demand Pokemon: Charizard, Pikachu, Mewtwo, Umbreon, Eevee, Espeon, Vaporeon, Jolteon, Rayquaza, Lugia, Ho-Oh, Gengar, Blastoise, Venusaur, Mew, Sylveon, Giratina, Arceus
- Any card whose number exceeds the printed set total (secret rare indicator, e.g. 201/165)
- Known chase cards from any set

EXCLUDE cards with no meaningful grading upside:
- Common and uncommon cards (circle/diamond rarity symbols)
- Basic Energy cards
- Trainer, Supporter, Stadium cards (unless full art or secret rare)
- Standard rares unlikely to have grading premium

Return ONLY a JSON array of 0-based indices to keep. No explanation, no text — just the array.
Example: [0, 2, 5, 11, 14]"""


def filter_cards(df: pd.DataFrame) -> pd.DataFrame:
    """Filter a card DataFrame to PSA-worthy candidates using Claude."""
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  ANTHROPIC_API_KEY not set — skipping AI pre-filter, using all cards")
        return df

    try:
        from anthropic import Anthropic
    except ImportError:
        print("  anthropic not installed — skipping AI pre-filter. Run: pip install anthropic")
        return df

    client = Anthropic(api_key=api_key)
    keep_indices: list[int] = []
    total_batches = (len(df) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"  AI filtering {len(df)} cards in {total_batches} batches...")

    for batch_num in range(total_batches):
        start = batch_num * BATCH_SIZE
        batch_df = df.iloc[start:start + BATCH_SIZE]
        batch_rows = batch_df.reset_index(drop=True)

        card_list = "\n".join(
            f"{i}. {row['card_name']} | {row['set_name']} | #{row.get('card_number', '?')}"
            for i, (_, row) in enumerate(batch_rows.iterrows())
        )

        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                # Cache system prompt across all batches — saves tokens and latency
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                          "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": card_list}],
            )
            text = response.content[0].text.strip()
            bracket_start = text.find("[")
            bracket_end = text.rfind("]") + 1
            indices = json.loads(text[bracket_start:bracket_end])
            valid = [i for i in indices if 0 <= i < len(batch_rows)]
            for idx in valid:
                keep_indices.append(batch_df.index[idx])
            print(f"    Batch {batch_num + 1}/{total_batches}: "
                  f"kept {len(valid)}/{len(batch_rows)}")
        except Exception as e:
            # On any error, keep the whole batch — never silently drop cards
            print(f"    Batch {batch_num + 1}/{total_batches}: error ({e}) — keeping all")
            keep_indices.extend(batch_df.index.tolist())

    filtered = df.loc[keep_indices].reset_index(drop=True)
    print(f"  AI filter: {len(df)} → {len(filtered)} cards "
          f"({len(df) - len(filtered)} excluded as low-value)")
    return filtered


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI pre-filter cards for PSA flip potential")
    parser.add_argument("--input", default=".tmp/discovered_cards.csv")
    parser.add_argument("--output", default=".tmp/filtered_cards.csv")
    args = parser.parse_args()

    load_dotenv()
    df = pd.read_csv(args.input)
    filtered = filter_cards(df)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    filtered.to_csv(args.output, index=False)
    print(f"Saved {len(filtered)} filtered cards → {args.output}")
