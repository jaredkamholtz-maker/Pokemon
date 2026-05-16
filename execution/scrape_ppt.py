"""
Scrape pokemonpricetracker.com/psa-analysis for card prices and PSA data.

Replaces fetch_ebay_prices.py + scrape_pokedata_population.py.

Requires Playwright — install with:
    pip install playwright
    playwright install chromium

Runs a real browser locally (bypasses IP block that affects cloud runners).
Extracts card data from the rendered DOM (cards load client-side).

Output: .tmp/ppt_cards.csv
    card_name, set_name, card_number, rarity,
    raw_price, psa10_chance, expected_profit, roi_pct
"""

import csv
import json
import time
from pathlib import Path

OUTPUT_FILE = Path(".tmp/ppt_cards.csv")
BASE_URL = "https://www.pokemonpricetracker.com/psa-analysis"

_EXTRACT_JS = """() => {
    const cards = document.querySelectorAll('div[class*="bg-card"][class*="text-card"]');
    return Array.from(cards).map(card => {
        const text = card.innerText || '';
        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);

        // ROI % — e.g. "1908% ROI"
        const roiMatch = text.match(/([\\d,]+(?:\\.\\d+)?)%\\s*ROI/);

        // Raw price — label then value on next line
        const rawMatch = text.match(/RAW PRICE:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/);

        // PSA 10 chance — e.g. "PSA 10 CHANCE:\\n6%"
        const psa10Match = text.match(/PSA 10 CHANCE:[^\\n]*\\n\\s*([\\d.]+)%/);

        // Expected profit — e.g. "EXP. PROFIT:\\n$954.16"
        const profitMatch = text.match(/EXP\\.\\s*PROFIT:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/);

        // Card name: line after "VIEW FULL ANALYSIS"
        const vfaIdx = lines.indexOf('VIEW FULL ANALYSIS');
        const cardName = vfaIdx >= 0 ? (lines[vfaIdx + 1] || '') : '';

        // Set info: "AQUAPOLIS · #H11/H32 · HOLO RARE"
        const setLine = lines.find(l => l.includes('\\u00b7') || l.includes('·')) || '';
        const parts = setLine.split(/\\s*[·\\u00b7]\\s*/).map(p => p.trim());
        const setName   = parts[0] || '';
        const cardNum   = (parts[1] || '').replace(/^#/, '').trim();
        const rarity    = parts[2] || '';

        const parse = s => s ? parseFloat(s.replace(/,/g, '')) : null;

        return {
            card_name:       cardName,
            set_name:        setName,
            card_number:     cardNum,
            rarity:          rarity,
            roi_pct:         roiMatch  ? parse(roiMatch[1])  : null,
            raw_price:       rawMatch  ? parse(rawMatch[1])  : null,
            psa10_chance:    psa10Match ? parse(psa10Match[1]) / 100 : null,
            expected_profit: profitMatch ? parse(profitMatch[1]) : null,
        };
    });
}"""


def run(output_path: str = str(OUTPUT_FILE), headless: bool = True) -> list[dict]:
    """
    Open pokemonpricetracker.com/psa-analysis, wait for cards to render,
    then extract all 100 cards from the DOM.
    """
    from playwright.sync_api import sync_playwright

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

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

        # Wait for at least one card to appear in the DOM
        try:
            page.wait_for_selector('div[class*="bg-card"][class*="text-card"]', timeout=20_000)
        except Exception:
            print("  Card elements didn't appear within 20s — trying anyway")

        time.sleep(3)

        results = page.evaluate(_EXTRACT_JS)
        browser.close()

    # Filter out empty/malformed entries
    results = [r for r in results if r.get("card_name") and r.get("raw_price")]
    print(f"Extracted {len(results)} cards")

    if not results:
        print("No cards found. Run with --no-headless to see what the browser shows.")
        return []

    # Sample output
    for r in results[:3]:
        print(f"  {r['card_name']} | {r['set_name']} | raw=${r['raw_price']} "
              f"profit=${r['expected_profit']} roi={r['roi_pct']}% "
              f"psa10={r['psa10_chance']}")

    fieldnames = [
        "card_name", "set_name", "card_number", "rarity",
        "raw_price", "psa10_chance", "expected_profit", "roi_pct",
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
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    args = parser.parse_args()
    run(output_path=args.output, headless=args.headless)
