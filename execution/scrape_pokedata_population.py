"""
Fetch PSA grade population data from PSA's public population report (psacard.com/pop).

Both pokedata.io and PSA's website are JavaScript SPAs. PSA is used here because
its population data is publicly accessible without a logged-in session.

Strategy (Playwright required):
  1. Load psacard.com/pop/search?q={card_name}+{set_name}.
  2. Wait for React to render search results; score links, click the best match.
  3. Wait for the card's pop report page to render the grade table.
  4. Parse grade 1-10 counts from the rendered DOM.
  5. Save HTML snapshots to .tmp/debug_psa/ on first card for inspection.

First-time setup (run once):
    playwright install chromium

Writes results to .tmp/pokedata_population.csv (same path for pipeline compat).

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
"""

import argparse
import re
import time
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from bs4 import BeautifulSoup

OUTPUT_FILE = Path(".tmp/pokedata_population.csv")
DEBUG_DIR = Path(".tmp/debug_psa")
BASE_URL = "https://www.psacard.com"
RATE_DELAY = 3.0
MAX_RETRIES = 2

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _save_debug(name: str, content: str) -> None:
    path = DEBUG_DIR / name
    if not path.exists():
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"  Saved debug → {path}")


def _score_link(text: str, card_name: str, set_name: str, num: str) -> float:
    text = text.lower()
    score = sum(1 for w in card_name.lower().split() if len(w) > 2 and w in text)
    score += sum(0.5 for w in set_name.lower().split() if len(w) > 2 and w in text)
    if num and num in text:
        score += 0.5
    return score


def _parse_grade_table(html: str) -> dict[int, int]:
    """
    Parse PSA grade counts from rendered page HTML.
    Tries multiple selector strategies since PSA's React structure can vary.
    """
    soup = BeautifulSoup(html, "html.parser")
    grade_counts: dict[int, int] = {}

    # Strategy 1: table rows where first cell is a grade number
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True)
        m = re.match(r"^(?:PSA\s*|Grade\s*)?(\d{1,2})$", label, re.I)
        if not m:
            continue
        grade = int(m.group(1))
        if not (1 <= grade <= 10):
            continue
        for cell in cells[1:]:
            raw = cell.get_text(strip=True).replace(",", "")
            try:
                grade_counts[grade] = grade_counts.get(grade, 0) + int(raw)
                break
            except ValueError:
                continue

    if grade_counts:
        return grade_counts

    # Strategy 2: look for elements with grade-related class names
    for el in soup.find_all(class_=re.compile(r"grade|pop|count", re.I)):
        text = el.get_text(" ", strip=True)
        for m in re.finditer(r"\b(10|[1-9])\b[^\d]*?(\d[\d,]*)", text):
            grade = int(m.group(1))
            try:
                count = int(m.group(2).replace(",", ""))
                if 1 <= grade <= 10 and count >= 0:
                    grade_counts[grade] = count
            except ValueError:
                continue

    return grade_counts


def fetch_population_playwright(card_name: str, set_name: str,
                                card_number: str) -> tuple[dict, str]:
    """
    Use Playwright to search PSA pop report, navigate to best result,
    and parse the rendered grade table. Returns (grade_counts, source_url).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "Playwright not installed.\n"
            "  pip install playwright && playwright install chromium"
        )

    num = card_number.split("/")[0].strip() if card_number and card_number != "nan" else ""
    query = f"{card_name} {set_name}" + (f" {num}" if num else "")
    search_url = f"{BASE_URL}/pop/search?q={quote(query)}"

    for attempt in range(MAX_RETRIES):
        try:
            with sync_playwright() as pw:
                import glob as _glob
                _chrome_paths = sorted(_glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
                _exe = _chrome_paths[-1] if _chrome_paths else None
                browser = pw.chromium.launch(
                    headless=True,
                    executable_path=_exe,
                )
                ctx = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                    ignore_https_errors=True,
                )
                page = ctx.new_page()
                # Spoof navigator.webdriver to avoid bot detection
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                # Capture ALL psacard.com responses to find the AJAX search endpoint
                api_log: list[dict] = []

                def on_response(resp):
                    if "psacard.com" not in resp.url:
                        return
                    ct = resp.headers.get("content-type", "")
                    entry = {"url": resp.url, "status": resp.status, "ct": ct}
                    try:
                        if "json" in ct:
                            entry["preview"] = str(resp.json())[:500]
                        elif "html" in ct or "text" in ct:
                            entry["preview"] = resp.text()[:500]
                    except Exception:
                        pass
                    api_log.append(entry)

                page.on("response", on_response)

                # Step 1: load search — use "load" not "networkidle" (PSA has
                # continuous analytics that prevent networkidle from firing)
                page.goto(search_url, wait_until="load", timeout=45000)
                try:
                    # Wait for actual card result links to appear
                    page.wait_for_selector(
                        "a[href*='/pop/pokemon-cards/']",
                        timeout=25000,
                    )
                except Exception:
                    pass
                # Extra settle time for late-rendering React components
                time.sleep(4)

                search_html = page.content()
                _save_debug("psa_search.html", search_html)

                # Save API calls so we can find the right endpoint
                import json
                _save_debug(
                    "psa_api_calls.json",
                    json.dumps(api_log, indent=2, default=str),
                )

                # Step 2: find best-matching pop link
                links = page.query_selector_all("a[href*='/pop/pokemon-cards/']")
                best_href = None
                best_score = -1.0
                for link in links:
                    try:
                        text = link.inner_text()
                        href = link.get_attribute("href") or ""
                        score = _score_link(text, card_name, set_name, num)
                        if score > best_score:
                            best_score = score
                            best_href = href
                    except Exception:
                        continue

                if not best_href or best_score <= 0:
                    browser.close()
                    return {}, ""

                pop_url = best_href if best_href.startswith("http") else f"{BASE_URL}{best_href}"

                # Step 3: navigate to card pop page
                page.goto(pop_url, wait_until="load", timeout=45000)
                try:
                    page.wait_for_selector("table, tr", timeout=15000)
                except Exception:
                    pass
                time.sleep(5)

                pop_html = page.content()
                _save_debug("psa_pop_page.html", pop_html)
                browser.close()

            grade_counts = _parse_grade_table(pop_html)
            return grade_counts, pop_url

        except Exception as e:
            print(f"  Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
            time.sleep(5 * (attempt + 1))

    return {}, ""


def fetch_population(card_name: str, set_name: str, card_number: str) -> dict:
    base = {
        "card_name": card_name,
        "set_name": set_name,
        "card_number": card_number,
        "total_graded": None,
        "psa10_count": None,
        "psa9_count": None,
        "gem_rate": None,
        "source_url": None,
        "error": None,
    }

    try:
        grade_counts, pop_url = fetch_population_playwright(card_name, set_name, card_number)

        if not pop_url:
            base["error"] = (
                f"No matching PSA pop page found for '{card_name} {set_name}'. "
                f"Check {DEBUG_DIR}/psa_search.html"
            )
            return base

        base["source_url"] = pop_url

        if grade_counts:
            total = sum(grade_counts.values())
            psa10 = grade_counts.get(10, 0)
            psa9 = grade_counts.get(9, 0)
            base["total_graded"] = total
            base["psa10_count"] = psa10
            base["psa9_count"] = psa9
            if total > 0:
                base["gem_rate"] = round((psa9 + psa10) / total, 4)
        else:
            base["error"] = (
                f"Pop page loaded but no grade counts parsed. "
                f"Check {DEBUG_DIR}/psa_pop_page.html"
            )

    except Exception as e:
        base["error"] = str(e)

    return base


def run(watchlist_path: str) -> pd.DataFrame:
    watchlist = pd.read_csv(watchlist_path)
    rows = []
    total = len(watchlist)

    for i, row in watchlist.iterrows():
        print(f"[{i+1}/{total}] Scraping population: {row['card_name']} ({row['set_name']})")
        pop = fetch_population(
            card_name=row["card_name"],
            set_name=row["set_name"],
            card_number=str(row.get("card_number", "")),
        )
        rows.append(pop)
        time.sleep(RATE_DELAY)

    df = pd.DataFrame(rows)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_FILE, index=False)
    print(f"\nSaved {len(df)} rows → {OUTPUT_FILE}")

    failed = df[df["error"].notna()]
    if not failed.empty:
        print(f"  {len(failed)} cards had errors:")
        for _, r in failed.iterrows():
            print(f"    - {r['card_name']}: {r['error']}")

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", default="data/watchlist.csv")
    args = parser.parse_args()
    run(args.watchlist)
