"""
Scrape pokemonpricetracker.com/psa-analysis for card prices and PSA data.

Replaces fetch_ebay_prices.py + scrape_pokedata_population.py.

Requires Playwright — install with:
    pip install playwright
    playwright install chromium

Runs a real browser locally (bypasses IP block that affects cloud runners).

Output: .tmp/ppt_cards.csv
    card_name, set_name, raw_price, psa9_price, psa10_price,
    psa10_chance, expected_profit, expected_return, investment_cost
"""

import csv
import re
import time
from pathlib import Path

OUTPUT_FILE = Path(".tmp/ppt_cards.csv")
BASE_URL = "https://www.pokemonpricetracker.com/psa-analysis"


def _parse_price(text: str) -> float | None:
    """Extract a dollar amount from text like '$12.50' or '12.50'."""
    if not text:
        return None
    m = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
    return float(m.group()) if m else None


def _parse_pct(text: str) -> float | None:
    """Extract a percentage like '72%' → 0.72."""
    if not text:
        return None
    m = re.search(r"[\d.]+", text)
    return float(m.group()) / 100 if m else None


def run(output_path: str = str(OUTPUT_FILE), headless: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    results = []
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.set_extra_http_headers({"User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )})

        print(f"Loading {BASE_URL}...")
        page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
        time.sleep(2)

        # Find all rows in the main table
        rows = page.query_selector_all("table tbody tr, [role='row']")
        if not rows:
            # Try generic card/row containers if table structure differs
            rows = page.query_selector_all("[data-testid='card-row'], .card-row, .table-row")

        print(f"Found {len(rows)} cards on main page")

        for i, row in enumerate(rows, 1):
            try:
                # Extract text from the row cells
                cells = row.query_selector_all("td, [role='cell']")
                cell_texts = [c.inner_text().strip() for c in cells]
                print(f"[{i}/{len(rows)}] Row text: {cell_texts[:5]}", end=" ")

                if not cell_texts:
                    print("— skipping (no cells)")
                    continue

                # Build base record from main table columns:
                # Expected order: card name, set(?), raw price, PSA 10 chance, exp profit
                # Adjust indices based on what we actually see
                card_name = cell_texts[0] if len(cell_texts) > 0 else ""
                set_name  = ""  # may not be in main table
                raw_price_text = ""
                psa10_chance_text = ""
                exp_profit_text = ""

                for text in cell_texts[1:]:
                    if "$" in text and not raw_price_text:
                        raw_price_text = text
                    elif "%" in text and not psa10_chance_text:
                        psa10_chance_text = text
                    elif "$" in text and raw_price_text:
                        exp_profit_text = text

                record = {
                    "card_name":      card_name,
                    "set_name":       set_name,
                    "raw_price":      _parse_price(raw_price_text),
                    "psa10_chance":   _parse_pct(psa10_chance_text),
                    "expected_profit": _parse_price(exp_profit_text),
                    "psa9_price":     None,
                    "psa10_price":    None,
                    "investment_cost": None,
                    "expected_return": None,
                }

                # Click "Full Analysis" button in this row
                btn = row.query_selector("button, a[href*='analysis'], [role='button']")
                if not btn:
                    # Try finding by text
                    btn = row.query_selector_all("button")
                    btn = next((b for b in btn if "analysis" in b.inner_text().lower()), None)

                if btn:
                    btn.click()
                    time.sleep(1.5)

                    # Scrape the overlay
                    overlay = page.query_selector(
                        "[role='dialog'], .modal, .overlay, [data-testid='overlay'], "
                        ".analysis-modal, [class*='modal'], [class*='overlay'], [class*='dialog']"
                    )

                    if overlay:
                        overlay_text = overlay.inner_text()
                        print(f"→ overlay found ({len(overlay_text)} chars)")

                        # Parse overlay fields by looking for label/value pairs
                        lines = [l.strip() for l in overlay_text.splitlines() if l.strip()]
                        for j, line in enumerate(lines):
                            lower = line.lower()
                            next_val = lines[j + 1] if j + 1 < len(lines) else ""
                            if "raw" in lower and "price" in lower:
                                record["raw_price"] = _parse_price(next_val) or record["raw_price"]
                            elif "psa 9" in lower or "psa9" in lower:
                                record["psa9_price"] = _parse_price(next_val)
                            elif "psa 10" in lower or "psa10" in lower:
                                v = _parse_price(next_val)
                                if v:
                                    record["psa10_price"] = v
                            elif "investment" in lower and "cost" in lower:
                                record["investment_cost"] = _parse_price(next_val)
                            elif "expected" in lower and "return" in lower:
                                record["expected_return"] = _parse_price(next_val)
                    else:
                        print("→ no overlay found")

                    # Close overlay — try Escape key or a close button
                    page.keyboard.press("Escape")
                    time.sleep(0.5)
                    close_btn = page.query_selector(
                        "[aria-label='close'], [aria-label='Close'], "
                        "button[class*='close'], button[class*='Close']"
                    )
                    if close_btn:
                        close_btn.click()
                    time.sleep(0.5)
                else:
                    print("→ no button found")

                results.append(record)
                print(f"  raw={record['raw_price']} psa9={record['psa9_price']} psa10={record['psa10_price']}")

            except PWTimeout:
                print(f"  timeout — skipping")
            except Exception as e:
                print(f"  error: {e}")

        browser.close()

    # Save to CSV
    if results:
        fieldnames = ["card_name", "set_name", "raw_price", "psa9_price", "psa10_price",
                      "psa10_chance", "expected_profit", "expected_return", "investment_cost"]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"\nSaved {len(results)} cards → {output_path}")
    else:
        print("\nNo data scraped — check that the page loaded correctly")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUTPUT_FILE))
    parser.add_argument("--headless", action="store_true",
                        help="Run without visible browser (not recommended for debugging)")
    args = parser.parse_args()
    run(output_path=args.output, headless=args.headless)
