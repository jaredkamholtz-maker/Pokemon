"""
Scrape pokemonpricetracker.com/psa-analysis for card prices and PSA data.

Replaces fetch_ebay_prices.py + scrape_pokedata_population.py.

Requires Playwright — install with:
    pip install playwright
    playwright install chromium

Runs a real browser locally (bypasses IP block that affects cloud runners).
Paginates through all pages and optionally filters to target sets.

Output: .tmp/ppt_cards.csv
    card_name, set_name, card_number, rarity,
    raw_price, psa10_chance, expected_profit, roi_pct

Usage:
    python3 execution/scrape_ppt.py                        # top 100 cards (page 1 only)
    python3 execution/scrape_ppt.py --all-pages            # all pages (~10 min)
    python3 execution/scrape_ppt.py --all-pages --target-sets data/target_sets.csv
"""

import csv
import re
import time
from pathlib import Path

OUTPUT_FILE = Path(".tmp/ppt_cards.csv")
BASE_URL = "https://www.pokemonpricetracker.com/psa-analysis"

_EXTRACT_JS = """() => {
    const cards = document.querySelectorAll('div[class*="bg-card"][class*="text-card"]');
    return Array.from(cards).map(card => {
        const text = card.innerText || '';
        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);

        const roiMatch   = text.match(/([\\d,]+(?:\\.\\d+)?)%\\s*ROI/);
        const rawMatch   = text.match(/RAW PRICE:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/);
        const psa10Match = text.match(/PSA 10 CHANCE:[^\\n]*\\n\\s*([\\d.]+)%/);
        const profMatch  = text.match(/EXP\\.\\s*PROFIT:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/);
        // PSA grade prices — try multiple label variants PPT may use
        const psa9Match  = text.match(/PSA\\s*9\\s*(?:PRICE)?:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/i)
                        || text.match(/GRADE\\s*9\\s*(?:PRICE)?:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/i);
        const psa10PriceMatch = text.match(/PSA\\s*10\\s*(?:PRICE)?:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/i)
                             || text.match(/GRADE\\s*10\\s*(?:PRICE)?:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/i);

        const vfaIdx  = lines.indexOf('VIEW FULL ANALYSIS');
        const cardName = vfaIdx >= 0 ? (lines[vfaIdx + 1] || '') : '';

        const setLine = lines.find(l => l.includes('\\u00b7') || l.includes('·')) || '';
        const parts   = setLine.split(/\\s*[·\\u00b7]\\s*/).map(p => p.trim());

        const parse = s => s ? parseFloat(s.replace(/,/g, '')) : null;
        return {
            card_name:       cardName,
            set_name:        parts[0] || '',
            card_number:     (parts[1] || '').replace(/^#/, '').trim(),
            rarity:          parts[2] || '',
            roi_pct:         roiMatch       ? parse(roiMatch[1])       : null,
            raw_price:       rawMatch       ? parse(rawMatch[1])       : null,
            psa9_price:      psa9Match      ? parse(psa9Match[1])      : null,
            psa10_price:     psa10PriceMatch ? parse(psa10PriceMatch[1]) : null,
            psa10_chance:    psa10Match     ? parse(psa10Match[1]) / 100 : null,
            expected_profit: profMatch      ? parse(profMatch[1])      : null,
            _raw_text:       text,  // debug: remove once labels confirmed
        };
    });
}"""


def _load_target_sets(path: str) -> set[str]:
    """Return a set of lowercased set names from target_sets.csv."""
    p = Path(path)
    if not p.exists():
        print(f"  Warning: target sets file not found: {path}")
        return set()
    import csv as _csv
    with open(p, newline="", encoding="utf-8") as f:
        return {row["set_name"].strip() for row in _csv.DictReader(f) if row.get("set_name")}


def _norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _matches_target(card_set: str, target_sets: set[str]) -> bool:
    if not target_sets:
        return True
    ppt = _norm(card_set)
    for t in target_sets:
        tn = _norm(t)
        # Exact normalized match, or target is a long-enough substring of PPT name
        # (handles "XY - ROARING SKIES" matching target "Roaring Skies")
        # Short targets (≤3 chars like "xy", "151") require exact match to avoid false positives
        if tn == ppt or (len(tn) >= 4 and tn in ppt):
            return True
    return False


def _click_page(page, page_num: int) -> bool:
    """Click a page number button. Returns True if the button was found."""
    try:
        clicked = page.evaluate(f"""() => {{
            const btns = Array.from(document.querySelectorAll('button, a'));
            const btn = btns.find(b => b.innerText.trim() === '{page_num}');
            if (btn) {{ btn.click(); return true; }}
            return false;
        }}""")
        return clicked
    except Exception:
        return False


def run(
    output_path: str = str(OUTPUT_FILE),
    headless: bool = True,
    all_pages: bool = False,
    target_sets_path: str | None = None,
) -> list[dict]:
    from playwright.sync_api import sync_playwright

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    target_sets = _load_target_sets(target_sets_path) if target_sets_path else set()
    if target_sets:
        print(f"Filtering to {len(target_sets)} target sets")

    all_cards: list[dict] = []
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
        page.wait_for_selector('div[class*="bg-card"][class*="text-card"]', timeout=20_000)
        time.sleep(3)

        # Detect total pages
        total_pages = page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button, a'));
            const nums = btns.map(b => parseInt(b.innerText.trim())).filter(n => !isNaN(n) && n > 1);
            return nums.length ? Math.max(...nums) : 1;
        }""")
        pages_to_scrape = total_pages if all_pages else 1
        print(f"  Site has {total_pages} pages — scraping {pages_to_scrape}")

        for page_num in range(1, pages_to_scrape + 1):
            if page_num > 1:
                if not _click_page(page, page_num):
                    print(f"  Page {page_num}: button not found, stopping")
                    break
                time.sleep(3)
                try:
                    page.wait_for_function(
                        f"""() => document.querySelectorAll('div[class*="bg-card"]').length > 0""",
                        timeout=10_000,
                    )
                except Exception:
                    pass

            cards = page.evaluate(_EXTRACT_JS)
            # Debug: print raw text of first card on first page to confirm PSA price label names
            if page_num == 1 and cards:
                print("\n── First card raw text (debug) ──")
                print(cards[0].get("_raw_text", "")[:600])
                print("─────────────────────────────────\n")
            added = 0
            for card in cards:
                if not card.get("card_name") or not card.get("raw_price"):
                    continue
                if not _matches_target(card.get("set_name", ""), target_sets):
                    continue
                uid = (card["card_name"], card["set_name"], card["card_number"])
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    all_cards.append(card)
                    added += 1

            print(f"  Page {page_num}/{pages_to_scrape}: +{added} cards (total {len(all_cards)})")

        browser.close()

    print(f"\nExtracted {len(all_cards)} cards total")
    if not all_cards:
        print("No cards found. Try running without --target-sets to verify scraping works.")
        return []

    for r in all_cards[:3]:
        print(f"  {r['card_name']} | {r['set_name']} | raw=${r['raw_price']} "
              f"profit=${r['expected_profit']} roi={r['roi_pct']}%")

    fieldnames = ["card_name", "set_name", "card_number", "rarity",
                  "raw_price", "psa9_price", "psa10_price",
                  "psa10_chance", "expected_profit", "roi_pct"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_cards)
    print(f"Saved {len(all_cards)} cards → {output_path}")
    return all_cards


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--all-pages", action="store_true",
                        help="Scrape all pages (~10 min). Default: page 1 only.")
    parser.add_argument("--target-sets", default=None, metavar="PATH",
                        help="CSV with set_name column — filter to these sets only.")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    args = parser.parse_args()
    run(
        output_path=args.output,
        headless=args.headless,
        all_pages=args.all_pages,
        target_sets_path=args.target_sets,
    )
