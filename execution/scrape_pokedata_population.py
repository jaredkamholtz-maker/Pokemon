"""
Scrape PSA grade population data from pokedata.io for each card in the watchlist.

pokedata.io blocks plain HTTP requests, so this script uses Playwright (headless
Chromium) to render the page like a real browser before parsing the HTML.

First-time setup (run once):
    pip install playwright
    playwright install chromium

Writes results to .tmp/pokedata_population.csv

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
"""

import argparse
import re
import time
from pathlib import Path

import pandas as pd

OUTPUT_FILE = Path(".tmp/pokedata_population.csv")
BASE_URL = "https://www.pokedata.io"
RATE_DELAY = 3.0  # seconds between page loads
MAX_RETRIES = 3


def slugify(text: str) -> str:
    """Convert card/set name to a URL-safe slug matching pokedata.io conventions."""
    text = text.lower()
    # Replace é/è etc with e
    text = text.encode("ascii", "ignore").decode()
    # Remove anything that's not alphanumeric, space, or hyphen
    text = re.sub(r"[^a-z0-9\s\-]", "", text)
    # Collapse spaces/hyphens to single hyphen
    text = re.sub(r"[\s\-]+", "-", text.strip())
    return text


def build_card_url(card_name: str, set_name: str) -> str:
    """Construct the pokedata.io card page URL from name + set."""
    return f"{BASE_URL}/card/{slugify(set_name)}/{slugify(card_name)}"


def fetch_page_html(url: str) -> str | None:
    """Use Playwright headless Chromium to fetch a fully-rendered page."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise ImportError(
            "Playwright not installed. Run:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    for attempt in range(MAX_RETRIES):
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                )
                page = ctx.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                browser.close()
                return html
        except PWTimeout:
            print(f"  Timeout on attempt {attempt + 1}/{MAX_RETRIES}: {url}")
            time.sleep(5 * (attempt + 1))
        except Exception as e:
            print(f"  Error on attempt {attempt + 1}/{MAX_RETRIES}: {e}")
            time.sleep(5 * (attempt + 1))

    return None


DEBUG_HTML_FILE = Path(".tmp/debug_pokedata_page.html")


def parse_population_html(html: str, source_url: str) -> dict:
    """
    Parse a pokedata.io card page and extract PSA grade distribution.
    Tries multiple strategies since the page layout may vary.
    Saves raw HTML to .tmp/debug_pokedata_page.html on first parse failure for inspection.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    grade_counts: dict[int, int] = {}

    # Strategy 1: look for table rows with grade numbers
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = [td.get_text(strip=True).replace(",", "") for td in row.find_all(["td", "th"])]
            for i, cell in enumerate(cells):
                if re.fullmatch(r"(10|[1-9])", cell) and i + 1 < len(cells):
                    count_str = cells[i + 1]
                    if re.fullmatch(r"\d+", count_str):
                        grade_counts[int(cell)] = int(count_str)

    # Strategy 2: scan all text for "PSA 10: N" or "Grade 10 N" patterns
    if not grade_counts:
        text = soup.get_text(separator=" ")
        for m in re.finditer(
            r"(?:PSA\s+|Grade\s+)(10|[1-9])\D{0,5}?([\d,]+)", text, re.I
        ):
            grade = int(m.group(1))
            count = int(m.group(2).replace(",", ""))
            if count < 10_000_000:  # sanity cap
                grade_counts[grade] = grade_counts.get(grade, 0) + count

    # Strategy 3: look for elements with data attributes or aria labels
    if not grade_counts:
        for el in soup.find_all(attrs={"data-grade": True}):
            grade = int(el.get("data-grade", 0))
            count_text = el.get_text(strip=True).replace(",", "")
            if re.fullmatch(r"\d+", count_text) and 1 <= grade <= 10:
                grade_counts[grade] = int(count_text)

    total_graded = sum(grade_counts.values())
    psa10_count = grade_counts.get(10, 0)
    psa9_count = grade_counts.get(9, 0)
    parse_success = total_graded > 0

    # Save raw HTML on first failure so the parser can be debugged
    if not parse_success and not DEBUG_HTML_FILE.exists():
        DEBUG_HTML_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEBUG_HTML_FILE.write_text(html, encoding="utf-8")
        print(f"  Saved debug HTML → {DEBUG_HTML_FILE}")

    return {
        "total_graded": total_graded,
        "psa10_count": psa10_count,
        "psa9_count": psa9_count,
        "source_url": source_url,
        "parse_success": parse_success,
    }


def fetch_population(card_name: str, set_name: str, card_number: str) -> dict:
    """Full pipeline for a single card: build URL → render → parse → return."""
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

    url = build_card_url(card_name, set_name)
    base["source_url"] = url

    try:
        html = fetch_page_html(url)
        if not html:
            base["error"] = f"Failed to load page after {MAX_RETRIES} retries"
            return base

        pop = parse_population_html(html, url)
        base.update(pop)

        if pop["total_graded"] and pop["total_graded"] > 0:
            gem_count = pop["psa10_count"] + pop["psa9_count"]
            base["gem_rate"] = round(gem_count / pop["total_graded"], 4)
        elif not pop["parse_success"]:
            base["error"] = "Page loaded but population data could not be parsed (layout may have changed)"

    except ImportError as e:
        base["error"] = str(e)
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
