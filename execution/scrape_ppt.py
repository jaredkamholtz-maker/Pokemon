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
    raw_price, psa9_price, psa10_price, psa10_chance, expected_profit, roi_pct

Usage:
    python3 execution/scrape_ppt.py                        # top 100 cards (page 1 only)
    python3 execution/scrape_ppt.py --all-pages            # all pages (~10 min)
    python3 execution/scrape_ppt.py --all-pages --target-sets data/target_sets.csv
    python3 execution/scrape_ppt.py --detail-prices        # also fetch PSA 9/10 prices from detail pages
"""

import csv
import re
import time
from pathlib import Path

OUTPUT_FILE = Path(".tmp/ppt_cards.csv")
BASE_URL = "https://www.pokemonpricetracker.com/psa-analysis"

# Extracts summary data + the "VIEW FULL ANALYSIS" link from each card on the listing page
_EXTRACT_JS = """() => {
    const cards = document.querySelectorAll('div[class*="bg-card"][class*="text-card"]');
    return Array.from(cards).map(card => {
        const text = card.innerText || '';
        const lines = text.split('\\n').map(l => l.trim()).filter(l => l);

        const roiMatch   = text.match(/([\\d,]+(?:\\.\\d+)?)%\\s*ROI/);
        const rawMatch   = text.match(/RAW PRICE:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/);
        const psa10Match = text.match(/PSA 10 CHANCE:[^\\n]*\\n\\s*([\\d.]+)%/);
        const profMatch  = text.match(/EXP\\.\\s*PROFIT:[^\\n]*\\n\\s*\\$?([\\d,]+(?:\\.\\d+)?)/);

        const vfaIdx  = lines.indexOf('VIEW FULL ANALYSIS');
        const cardName = vfaIdx >= 0 ? (lines[vfaIdx + 1] || '') : '';

        // Capture the detail page URL — walk up from the VFA text node to find the nearest <a>
        let detailUrl = '';
        const allEls = Array.from(card.querySelectorAll('*'));
        for (const el of allEls) {
            if (el.children.length === 0 && (el.innerText || '').trim() === 'VIEW FULL ANALYSIS') {
                // Walk up to find enclosing <a>
                let ancestor = el;
                for (let i = 0; i < 6; i++) {
                    if (!ancestor) break;
                    if (ancestor.tagName === 'A' && ancestor.href) { detailUrl = ancestor.href; break; }
                    ancestor = ancestor.parentElement;
                }
                break;
            }
        }
        // Fallback: any <a> in the card whose href contains the card slug
        if (!detailUrl) {
            const anyLink = Array.from(card.querySelectorAll('a[href]'))
                .find(a => a.href && !a.href.endsWith('/psa-analysis'));
            if (anyLink) detailUrl = anyLink.href;
        }

        const setLine = lines.find(l => l.includes('\\u00b7') || l.includes('·')) || '';
        const parts   = setLine.split(/\\s*[·\\u00b7]\\s*/).map(p => p.trim());

        const parse = s => s ? parseFloat(s.replace(/,/g, '')) : null;
        return {
            card_name:       cardName,
            set_name:        parts[0] || '',
            card_number:     (parts[1] || '').replace(/^#/, '').trim(),
            rarity:          parts[2] || '',
            roi_pct:         roiMatch  ? parse(roiMatch[1])       : null,
            raw_price:       rawMatch  ? parse(rawMatch[1])       : null,
            psa10_chance:    psa10Match ? parse(psa10Match[1]) / 100 : null,
            expected_profit: profMatch  ? parse(profMatch[1])     : null,
            detail_url:      detailUrl,
        };
    });
}"""


def _parse_detail_prices(text: str) -> dict:
    """Parse PSA 9 and PSA 10 prices from a detail page's raw text."""
    parse = lambda s: float(s.replace(',', '')) if s else None

    # Try common label patterns — will refine once we see the real page
    psa9_match = (
        re.search(r'PSA\s*9\s*(?:PRICE|AVG|SALE|VALUE)?[:\s]*\$?([\d,]+(?:\.\d+)?)', text, re.I)
        or re.search(r'GRADE\s*9[:\s]*\$?([\d,]+(?:\.\d+)?)', text, re.I)
    )
    psa10_match = (
        re.search(r'PSA\s*10\s*(?:PRICE|AVG|SALE|VALUE)?[:\s]*\$?([\d,]+(?:\.\d+)?)', text, re.I)
        or re.search(r'GRADE\s*10[:\s]*\$?([\d,]+(?:\.\d+)?)', text, re.I)
    )
    return {
        "psa9_price":  parse(psa9_match.group(1))  if psa9_match  else None,
        "psa10_price": parse(psa10_match.group(1)) if psa10_match else None,
    }


def _fetch_detail_prices(page, cards: list[dict]) -> list[dict]:
    """Click VIEW FULL ANALYSIS for each card, scrape PSA prices, go back."""
    debug_printed = False

    for i, card in enumerate(cards):
        card_name = card["card_name"]
        try:
            # Click the VFA element for this card by matching card name in text
            escaped = card_name.replace("'", "\\'")
            clicked = page.evaluate(f"""() => {{
                const allCards = document.querySelectorAll('div[class*="bg-card"][class*="text-card"]');
                for (const c of allCards) {{
                    if ((c.innerText || '').includes('{escaped}')) {{
                        const vfa = Array.from(c.querySelectorAll('*')).find(
                            el => el.children.length === 0 && (el.innerText || '').trim() === 'VIEW FULL ANALYSIS'
                        );
                        if (vfa) {{ vfa.click(); return true; }}
                    }}
                }}
                return false;
            }}""")

            if not clicked:
                print(f"  [{i+1}/{len(cards)}] {card_name}: VFA button not found")
                card["psa9_price"] = None
                card["psa10_price"] = None
                continue

            # Wait for navigation away from the listing page
            page.wait_for_function(
                f"() => !window.location.pathname.endsWith('/psa-analysis') && window.location.pathname !== '/'",
                timeout=10_000,
            )
            page.wait_for_load_state("networkidle", timeout=15_000)
            time.sleep(1)

            detail_url = page.url
            text = page.evaluate("() => document.body.innerText || ''")

            if not debug_printed:
                print(f"\n── Detail page raw text (debug: {detail_url}) ──")
                print(text[:1200])
                print("──────────────────────────────────────────\n")
                debug_printed = True

            prices = _parse_detail_prices(text)
            card["psa9_price"]  = prices["psa9_price"]
            card["psa10_price"] = prices["psa10_price"]
            card["detail_url"]  = detail_url
            print(f"  [{i+1}/{len(cards)}] {card_name}: PSA9=${prices['psa9_price']} PSA10=${prices['psa10_price']}")

        except Exception as e:
            print(f"  [{i+1}/{len(cards)}] {card_name}: error — {e}")
            card["psa9_price"] = None
            card["psa10_price"] = None

        finally:
            # Go back to listing page and wait for cards to render
            try:
                page.go_back(wait_until="networkidle", timeout=15_000)
                page.wait_for_selector('div[class*="bg-card"][class*="text-card"]', timeout=10_000)
                time.sleep(1)
            except Exception:
                # If back fails, reload the listing page
                page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
                page.wait_for_selector('div[class*="bg-card"][class*="text-card"]', timeout=10_000)
                time.sleep(2)

    return cards


def _load_target_sets(path: str) -> set[str]:
    """Return a set of set names from target_sets.csv."""
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
    detail_prices: bool = False,
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
                        """() => document.querySelectorAll('div[class*="bg-card"]').length > 0""",
                        timeout=10_000,
                    )
                except Exception:
                    pass

            cards = page.evaluate(_EXTRACT_JS)
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

        # Visit each card's detail page to get PSA 9/10 prices
        if detail_prices and all_cards:
            print(f"\nFetching PSA 9/10 prices from {len(all_cards)} detail pages (~{len(all_cards)*3//60+1} min)...")
            all_cards = _fetch_detail_prices(page, all_cards)

        browser.close()

    print(f"\nExtracted {len(all_cards)} cards total")
    if not all_cards:
        print("No cards found. Try running without --target-sets to verify scraping works.")
        return []

    for r in all_cards[:3]:
        print(f"  {r['card_name']} | {r['set_name']} | raw=${r['raw_price']} "
              f"psa9=${r.get('psa9_price')} psa10=${r.get('psa10_price')} "
              f"profit=${r['expected_profit']} roi={r['roi_pct']}%")

    fieldnames = ["card_name", "set_name", "card_number", "rarity",
                  "raw_price", "psa9_price", "psa10_price",
                  "psa10_chance", "expected_profit", "roi_pct", "detail_url"]
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
    parser.add_argument("--detail-prices", action="store_true",
                        help="Visit each card's full analysis page to fetch PSA 9/10 prices.")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    args = parser.parse_args()
    run(
        output_path=args.output,
        headless=args.headless,
        all_pages=args.all_pages,
        target_sets_path=args.target_sets,
        detail_prices=args.detail_prices,
    )
