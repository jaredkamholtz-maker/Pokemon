"""
Scrape pokemonpricetracker.com/psa-analysis for card prices and PSA data.

Replaces fetch_ebay_prices.py + scrape_pokedata_population.py.

Requires Playwright — install with:
    pip install playwright
    playwright install chromium

Runs a real browser locally (bypasses IP block that affects cloud runners).

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


def _parse_card(card: dict) -> dict:
    psa_prices = card.get("psaPrices") or {}
    psa10_data = psa_prices.get("psa10") or {}
    psa9_data  = psa_prices.get("psa9")  or {}
    grading_probs = card.get("gradingProbabilities") or {}

    return {
        "card_name":        card.get("name", ""),
        "set_name":         card.get("setName", ""),
        "card_number":      card.get("number", ""),
        "printing":         card.get("printing", ""),
        "rarity":           card.get("rarity", ""),
        "raw_price":        card.get("rawPrice"),
        "psa9_price":       psa9_data.get("price"),
        "psa10_price":      psa10_data.get("price"),
        "psa10_chance":     grading_probs.get("psa10"),
        "psa9_chance":      grading_probs.get("psa9"),
        "roi_pct":          card.get("roiPercentage"),
        "expected_profit":  card.get("potentialProfit"),
        "total_population": card.get("totalPopulation"),
        "gem_rate":         card.get("combinedGemRate"),
    }


def _looks_like_card(obj) -> bool:
    return (
        isinstance(obj, dict)
        and "rawPrice" in obj
        and "psaPrices" in obj
        and "name" in obj
    )


def _harvest_cards(node, card_list: list, seen_ids: set):
    if isinstance(node, list):
        cards = [x for x in node if _looks_like_card(x)]
        if cards:
            for card in cards:
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


def _extract_from_next_f(html: str) -> list[dict]:
    """
    Parse __next_f.push() payloads using bracket-counting (not regex).
    """
    card_list: list[dict] = []
    seen_ids: set = set()
    search_str = "self.__next_f.push("
    pos = 0

    while True:
        idx = html.find(search_str, pos)
        if idx == -1:
            break

        arg_start = idx + len(search_str)
        depth = 0
        in_string = False
        escape = False
        i = arg_start

        while i < len(html):
            c = html[i]
            if escape:
                escape = False
            elif in_string:
                if c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c in "([{":
                    depth += 1
                elif c in ")]}":
                    if depth == 0:
                        break
                    depth -= 1
            i += 1

        raw = html[arg_start:i]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            pos = idx + 1
            continue

        if not isinstance(payload, list) or len(payload) < 2:
            pos = idx + 1
            continue

        value = payload[1]
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pos = idx + 1
                continue

        _harvest_cards(value, card_list, seen_ids)
        pos = idx + 1

    return card_list


def run(output_path: str = str(OUTPUT_FILE), headless: bool = True) -> list[dict]:
    from playwright.sync_api import sync_playwright

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    card_list: list[dict] = []
    seen_ids: set = set()

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
        time.sleep(5)

        # --- Strategy 1: read window.__next_f via evaluate ---
        next_f_raw = page.evaluate("""() => {
            try {
                return JSON.stringify(window.__next_f || []);
            } catch(e) { return '[]'; }
        }""")
        try:
            next_f = json.loads(next_f_raw)
            print(f"window.__next_f has {len(next_f)} entries")
            for entry in next_f:
                _harvest_cards(entry, card_list, seen_ids)
        except Exception as e:
            print(f"  window.__next_f read error: {e}")

        # --- Strategy 2: parse the page HTML __next_f push calls ---
        if not card_list:
            html = page.content()
            cards_from_html = _extract_from_next_f(html)
            card_list.extend(cards_from_html)
            seen_ids.update(
                (c["card_name"], c["set_name"], c["card_number"]) for c in cards_from_html
            )

        # --- Diagnostics if still empty ---
        if not card_list:
            html = page.content()

            # Search for any price-related field names in the page
            price_fields = set(re.findall(r'"([a-zA-Z]*[Pp]rice[a-zA-Z]*)":', html))
            print(f"  Price-related fields in HTML: {price_fields or 'none'}")

            # Show first 300 chars of each __next_f push to understand structure
            push_idx = 0
            search_str = "self.__next_f.push("
            pos = 0
            while push_idx < 3:
                idx = html.find(search_str, pos)
                if idx == -1:
                    break
                print(f"  push[{push_idx}]: {html[idx:idx+150]!r}")
                pos = idx + 1
                push_idx += 1

            # Try reading window globals for any card-shaped data
            globals_info = page.evaluate("""() => {
                const keys = Object.keys(window).filter(k =>
                    !['location','document','navigator','history','screen',
                      'performance','console','fetch','XMLHttpRequest',
                      '__next','__NEXT_DATA__'].includes(k)
                    && typeof window[k] !== 'function'
                    && typeof window[k] !== 'undefined'
                );
                const result = {};
                for (const k of keys.slice(0, 30)) {
                    try {
                        const v = JSON.stringify(window[k]);
                        if (v && v.length < 500) result[k] = v;
                    } catch(e) {}
                }
                // Also check __NEXT_DATA__ specifically
                if (window.__NEXT_DATA__) {
                    result['__NEXT_DATA__'] = JSON.stringify(window.__NEXT_DATA__).slice(0, 500);
                }
                return result;
            }""")
            print(f"  Window globals: {globals_info}")

        browser.close()

    print(f"Extracted {len(card_list)} cards total")

    if not card_list:
        print("Could not find card data. Check the diagnostic output above.")
        return []

    for r in card_list[:3]:
        print(f"  {r['card_name']} | {r['set_name']} | raw=${r['raw_price']} "
              f"psa9=${r['psa9_price']} psa10=${r['psa10_price']}")

    fieldnames = [
        "card_name", "set_name", "card_number", "printing", "rarity",
        "raw_price", "psa9_price", "psa10_price", "psa10_chance", "psa9_chance",
        "roi_pct", "expected_profit", "total_population", "gem_rate",
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(card_list)
    print(f"Saved {len(card_list)} cards → {output_path}")

    return card_list


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    args = parser.parse_args()
    run(output_path=args.output, headless=args.headless)
