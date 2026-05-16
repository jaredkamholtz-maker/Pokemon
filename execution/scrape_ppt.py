"""
Scrape pokemonpricetracker.com/psa-analysis for card prices and PSA data.

Replaces fetch_ebay_prices.py + scrape_pokedata_population.py.

Requires Playwright — install with:
    pip install playwright
    playwright install chromium

Runs a real browser locally (bypasses IP block that affects cloud runners).
Extracts data from embedded Next.js RSC JSON in the page HTML — no clicking needed.

Output: .tmp/ppt_cards.csv
    card_name, set_name, card_number, printing, rarity,
    raw_price, psa9_price, psa10_price, psa10_chance,
    roi_pct, expected_profit, total_population, gem_rate
"""

import csv
import json
import re
import time
from pathlib import Path

OUTPUT_FILE = Path(".tmp/ppt_cards.csv")
BASE_URL = "https://www.pokemonpricetracker.com/psa-analysis"


def _extract_cards_from_html(html: str) -> list[dict]:
    """
    Parse Next.js RSC payload from self.__next_f.push(...) script tags.
    Returns a flat list of card dicts.
    """
    # Collect all push() payloads
    chunks = re.findall(r'self\.__next_f\.push\(\s*(\[.*?\])\s*\)', html, re.DOTALL)

    card_list: list[dict] = []
    seen_ids: set = set()

    for chunk in chunks:
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue

        # payload is [key, value] — value may be a JSON string or object
        if not isinstance(payload, list) or len(payload) < 2:
            continue
        value = payload[1]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass

        # Walk the structure looking for arrays of card objects
        _harvest_cards(value, card_list, seen_ids)

    return card_list


def _looks_like_card(obj: dict) -> bool:
    return (
        isinstance(obj, dict)
        and "rawPrice" in obj
        and "psaPrices" in obj
        and "name" in obj
    )


def _harvest_cards(node, card_list: list, seen_ids: set):
    """Recursively walk JSON, collect card objects."""
    if isinstance(node, list):
        # Check if this list IS the card array
        cards_in_node = [x for x in node if _looks_like_card(x)]
        if cards_in_node:
            for card in cards_in_node:
                uid = (card.get("name"), card.get("setName"), card.get("number"))
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    card_list.append(_parse_card(card))
        else:
            for item in node:
                _harvest_cards(item, card_list, seen_ids)
    elif isinstance(node, dict):
        if _looks_like_card(node):
            uid = (node.get("name"), node.get("setName"), node.get("number"))
            if uid not in seen_ids:
                seen_ids.add(uid)
                card_list.append(_parse_card(node))
        else:
            for v in node.values():
                _harvest_cards(v, card_list, seen_ids)


def _parse_card(card: dict) -> dict:
    psa_prices = card.get("psaPrices") or {}
    psa10_data = psa_prices.get("psa10") or {}
    psa9_data  = psa_prices.get("psa9")  or {}

    grading_probs = card.get("gradingProbabilities") or {}
    psa10_chance = grading_probs.get("psa10")
    psa9_chance  = grading_probs.get("psa9")

    return {
        "card_name":        card.get("name", ""),
        "set_name":         card.get("setName", ""),
        "card_number":      card.get("number", ""),
        "printing":         card.get("printing", ""),
        "rarity":           card.get("rarity", ""),
        "raw_price":        card.get("rawPrice"),
        "psa9_price":       psa9_data.get("price"),
        "psa10_price":      psa10_data.get("price"),
        "psa10_chance":     psa10_chance,
        "psa9_chance":      psa9_chance,
        "roi_pct":          card.get("roiPercentage"),
        "expected_profit":  card.get("potentialProfit"),
        "total_population": card.get("totalPopulation"),
        "gem_rate":         card.get("combinedGemRate"),
    }


def run(output_path: str = str(OUTPUT_FILE), headless: bool = True,
        use_cache: bool = False) -> list[dict]:
    """
    Fetch pokemonpricetracker.com/psa-analysis and extract all card data.

    Args:
        output_path: CSV destination
        headless: run Playwright without a visible browser (required in Codespace)
        use_cache: if True and .tmp/ppt_debug.html exists, parse that instead of fetching
    """
    from playwright.sync_api import sync_playwright

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    debug_path = Path(".tmp/ppt_debug.html")

    if use_cache and debug_path.exists():
        print(f"Using cached HTML from {debug_path}")
        html = debug_path.read_text(encoding="utf-8")
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            page = browser.new_page(ignore_https_errors=True)
            page.set_extra_http_headers({"User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )})

            print(f"Loading {BASE_URL} ...")
            page.goto(BASE_URL, wait_until="networkidle", timeout=60_000)
            time.sleep(3)

            html = page.content()
            browser.close()

        debug_path.parent.mkdir(parents=True, exist_ok=True)
        debug_path.write_text(html, encoding="utf-8")
        print(f"Page HTML saved to {debug_path} ({len(html):,} chars)")

    # --- parse embedded JSON ---
    print("Parsing embedded Next.js JSON...")
    results = _extract_cards_from_html(html)
    print(f"Extracted {len(results)} cards")

    if not results:
        print("No data found — check ppt_debug.html to inspect the page structure")
        return []

    # Print a sample
    for r in results[:3]:
        print(f"  {r['card_name']} | {r['set_name']} | raw=${r['raw_price']} "
              f"psa9=${r['psa9_price']} psa10=${r['psa10_price']} "
              f"psa10_chance={r['psa10_chance']}")

    # Save CSV
    fieldnames = [
        "card_name", "set_name", "card_number", "printing", "rarity",
        "raw_price", "psa9_price", "psa10_price", "psa10_chance", "psa9_chance",
        "roi_pct", "expected_profit", "total_population", "gem_rate",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"Saved {len(results)} cards → {output_path}")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run without visible browser (default: True)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Show browser window (requires X server)")
    parser.add_argument("--use-cache", action="store_true",
                        help="Parse .tmp/ppt_debug.html instead of fetching fresh")
    args = parser.parse_args()
    run(output_path=args.output, headless=args.headless, use_cache=args.use_cache)
