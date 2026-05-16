"""
Scrape pokemonpricetracker.com/psa-analysis for card prices and PSA data.

Replaces fetch_ebay_prices.py + scrape_pokedata_population.py.

Requires Playwright — install with:
    pip install playwright
    playwright install chromium

Runs a real browser locally (bypasses IP block that affects cloud runners).
Intercepts the card data API response that the browser fetches after page load.

Output: .tmp/ppt_cards.csv
    card_name, set_name, card_number, printing, rarity,
    raw_price, psa9_price, psa10_price, psa10_chance,
    roi_pct, expected_profit, total_population, gem_rate
"""

import csv
import json
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
    """Recursively walk a JSON structure collecting card objects."""
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


def run(output_path: str = str(OUTPUT_FILE), headless: bool = True) -> list[dict]:
    """
    Open pokemonpricetracker.com/psa-analysis in a browser, intercept the card
    data API response, and extract all cards.
    """
    from playwright.sync_api import sync_playwright

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []  # raw JSON responses that contain card data
    card_list: list[dict] = []
    seen_ids: set = set()

    def handle_response(response):
        url = response.url
        status = response.status
        ct = response.headers.get("content-type", "")
        # Log every non-trivial response to help identify where card data comes from
        if any(ext in url for ext in (".js", ".css", ".png", ".ico", ".woff")):
            return
        print(f"  [{status}] {ct[:30]:30s} {url[:100]}")
        if "json" not in ct and "text" not in ct:
            return
        try:
            text = response.text()
            if "rawPrice" not in text:
                return
            body = json.loads(text)
            _harvest_cards(body, card_list, seen_ids)
            print(f"  *** Captured {len(card_list)} cards from: {url}")
            captured.append(url)
        except Exception as e:
            print(f"  (parse error: {e})")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(ignore_https_errors=True)
        page.set_extra_http_headers({"User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )})

        page.on("response", handle_response)

        print(f"Loading {BASE_URL} ...")
        page.goto(BASE_URL, wait_until="networkidle", timeout=60_000)

        # Give the page extra time to finish any deferred API calls
        time.sleep(5)

        # Also wait for any pending network activity to settle
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        browser.close()

    print(f"Extracted {len(card_list)} cards total")

    if not card_list:
        print("No card data captured.")
        print("Tip: check that the site is reachable and not IP-blocking this machine.")
        return []

    # Sample output
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
    parser.add_argument("--headless", action="store_true", default=True,
                        help="Run without visible browser (default: True)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                        help="Show browser window (requires X server)")
    args = parser.parse_args()
    run(output_path=args.output, headless=args.headless)
