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
import time
from pathlib import Path

OUTPUT_FILE = Path(".tmp/ppt_cards.csv")
BASE_URL = "https://www.pokemonpricetracker.com/psa-analysis"

# JS injected before page load: intercepts fetch/XHR AND captures React Query / SWR cache
_INTERCEPT_SCRIPT = """
window._pptCaptures = [];

// Wrap fetch
const _origFetch = window.fetch;
window.fetch = async function(...args) {
    const resp = await _origFetch(...args);
    try {
        const clone = resp.clone();
        const text = await clone.text();
        window._pptCaptures.push({
            type: 'fetch',
            url: String(args[0]),
            status: resp.status,
            len: text.length,
            preview: text.slice(0, 200),
        });
    } catch(e) {}
    return resp;
};

// Wrap XHR
const _origOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function(m, url) {
    this._pptUrl = String(url);
    return _origOpen.apply(this, arguments);
};
XMLHttpRequest.prototype.send = function() {
    this.addEventListener('load', function() {
        try {
            window._pptCaptures.push({
                type: 'xhr',
                url: this._pptUrl,
                status: this.status,
                len: (this.responseText || '').length,
                preview: (this.responseText || '').slice(0, 200),
            });
        } catch(e) {}
    });
    return XMLHttpRequest.prototype.send.apply(this, arguments);
};
"""


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


def run(output_path: str = str(OUTPUT_FILE), headless: bool = True) -> list[dict]:
    from playwright.sync_api import sync_playwright

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    card_list: list[dict] = []
    seen_ids: set = set()
    console_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(ignore_https_errors=True)
        page.set_extra_http_headers({"User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )})

        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
        page.add_init_script(_INTERCEPT_SCRIPT)

        print(f"Loading {BASE_URL} ...")
        page.goto(BASE_URL, wait_until="networkidle", timeout=60_000)

        # Wait generously for deferred data loads
        print("Waiting 15s for client-side data to load...")
        time.sleep(15)

        # Take a screenshot so we can see what the page looks like
        screenshot_path = Path(".tmp/ppt_screenshot.png")
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"Screenshot saved to {screenshot_path}")

        # Read all captured network calls
        captures = page.evaluate("window._pptCaptures || []")

        # Try to read table data from the rendered DOM
        table_text = page.evaluate("""() => {
            const rows = document.querySelectorAll('table tr, [role="row"], [class*="row"], [class*="Row"]');
            return Array.from(rows).slice(0, 5).map(r => r.innerText.replace(/\\n/g, ' | ').trim());
        }""")

        # Check page title and a sample of visible text
        title = page.title()
        body_preview = page.evaluate("document.body.innerText.slice(0, 500)")

        browser.close()

    print(f"\nPage title: {title}")
    print(f"Body preview:\n{body_preview[:300]}\n")

    if console_errors:
        print(f"JS errors on page:")
        for e in console_errors[:5]:
            print(f"  {e}")

    print(f"\nNetwork calls intercepted: {len(captures)}")
    for cap in captures[:20]:
        has_price = "rawPrice" in cap.get("preview", "") or "price" in cap.get("preview", "").lower()
        print(f"  [{cap['type']}] {cap['status']} len={cap['len']:6d} {'*** PRICE DATA' if has_price else ''} {cap['url'][:100]}")

    print(f"\nRendered table rows (first 5): {table_text}")

    if not card_list and not captures:
        print("\nNothing captured. The page may require scrolling, interaction, or auth.")
        return []

    # Try to extract card data from any captured response containing rawPrice
    for cap in captures:
        preview = cap.get("preview", "")
        if "rawPrice" not in preview and "psaPrices" not in preview:
            continue
        # Would need full body — this is just a preview; for now report the URL
        print(f"\n*** Found rawPrice in response from: {cap['url']}")

    return card_list


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    args = parser.parse_args()
    run(output_path=args.output, headless=args.headless)
