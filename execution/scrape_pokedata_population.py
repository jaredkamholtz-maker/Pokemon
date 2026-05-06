"""
Fetch PSA grade population data from pokedata.io for each card in the watchlist.

pokedata.io is a Next.js app that loads all card data via internal API calls
after the page renders. This script uses Playwright to load the page and
intercept those API responses directly, rather than parsing the HTML shell.

First-time setup (run once):
    pip install playwright
    playwright install chromium
    sudo playwright install-deps chromium

Writes results to .tmp/pokedata_population.csv

Usage:
    python execution/scrape_pokedata_population.py --watchlist data/watchlist.csv
"""

import argparse
import json
import re
import time
from pathlib import Path

import pandas as pd

OUTPUT_FILE = Path(".tmp/pokedata_population.csv")
API_LOG_FILE = Path(".tmp/debug_pokedata_api_calls.json")
BASE_URL = "https://www.pokedata.io"
RATE_DELAY = 4.0
MAX_RETRIES = 2


def slugify(text: str) -> str:
    """Convert card/set name to URL slug matching pokedata.io conventions."""
    text = text.lower().encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9\s\-]", "", text)
    text = re.sub(r"[\s\-]+", "-", text.strip())
    return text


def build_card_url(card_name: str, set_name: str) -> str:
    return f"{BASE_URL}/card/{slugify(set_name)}/{slugify(card_name)}"


def fetch_card_api_data(url: str) -> tuple[list[dict], str]:
    """
    Load a pokedata.io card page with Playwright, intercept all JSON API
    responses, and return them alongside the final page HTML.

    Returns (api_responses, page_html).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "Playwright not installed.\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
            "  sudo playwright install-deps chromium"
        )

    api_responses = []

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

                def on_response(response):
                    # Capture any JSON response that looks like card/pricing data
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type:
                        try:
                            body = response.json()
                            api_responses.append({
                                "url": response.url,
                                "status": response.status,
                                "data": body,
                            })
                        except Exception:
                            pass

                page.on("response", on_response)
                page.goto(url, wait_until="networkidle", timeout=30000)

                # Wait for loading spinners to disappear (data loaded)
                try:
                    page.wait_for_selector(
                        ".MuiCircularProgress-root",
                        state="hidden",
                        timeout=12000,
                    )
                except Exception:
                    pass  # spinner may never appear or may not go away

                # Extra settle time for late XHR calls
                time.sleep(3)

                html = page.content()
                browser.close()
            return api_responses, html

        except Exception as e:
            print(f"  Attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
            time.sleep(5 * (attempt + 1))

    return [], ""


def extract_population_from_api(api_responses: list[dict]) -> dict | None:
    """
    Search intercepted API responses for grade population data.
    Returns dict with psa grade counts, or None if not found.
    """
    grade_counts: dict[int, int] = {}

    for resp in api_responses:
        url = resp.get("url", "")
        data = resp.get("data", {})
        data_str = json.dumps(data)

        # Look for responses that mention population / grades
        pop_keywords = ("population", "pop_count", "grade_count", "psa_grade",
                        "gem_rate", "psa10", "psa9", "graded")
        if not any(kw in data_str.lower() for kw in pop_keywords):
            continue

        # Strategy 1: look for a dict keyed by grade number
        # e.g. {"1": 5, "2": 10, ... "10": 200}
        if isinstance(data, dict):
            for key, val in data.items():
                if re.fullmatch(r"(10|[1-9])", str(key)) and isinstance(val, (int, float)):
                    grade_counts[int(key)] = int(val)

        # Strategy 2: look for list of {grade: X, count: Y} objects
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    grade = item.get("grade") or item.get("psa_grade")
                    count = item.get("count") or item.get("pop_count") or item.get("population")
                    if grade is not None and count is not None:
                        try:
                            grade_counts[int(grade)] = int(count)
                        except (ValueError, TypeError):
                            pass

        # Strategy 3: regex scan of raw JSON for grade patterns
        if not grade_counts:
            for m in re.finditer(
                r'"(?:grade|psa_grade)"\s*:\s*"?(10|[1-9])"?[^}]*?"(?:count|pop(?:ulation)?|total)"\s*:\s*(\d+)',
                data_str, re.I
            ):
                grade_counts[int(m.group(1))] = int(m.group(2))

        if grade_counts:
            break  # found it

    return grade_counts if grade_counts else None


def extract_population_from_sales(api_responses: list[dict]) -> dict | None:
    """
    Fallback: aggregate grade counts from individual sale records.
    pokedata.io shows eBay sales with grade info — aggregate those.
    """
    grade_counts: dict[int, int] = {}

    for resp in api_responses:
        data = resp.get("data", {})

        # Handle list of sale records or paginated result
        records = []
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            for key in ("results", "data", "sales", "items", "records"):
                if isinstance(data.get(key), list):
                    records = data[key]
                    break

        for record in records:
            if not isinstance(record, dict):
                continue
            grade_val = record.get("psa_grade") or record.get("grade")
            if grade_val is None:
                continue
            try:
                grade = int(float(str(grade_val)))
                if 1 <= grade <= 10:
                    grade_counts[grade] = grade_counts.get(grade, 0) + 1
            except (ValueError, TypeError):
                pass

    return grade_counts if grade_counts else None


def fetch_population(card_name: str, set_name: str, card_number: str) -> dict:
    """Full pipeline for a single card."""
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
        api_responses, html = fetch_card_api_data(url)

        # Log all API calls on first card for debugging
        if not API_LOG_FILE.exists() and api_responses:
            API_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Save URL + first 500 chars of each response to keep file small
            log = [{"url": r["url"], "status": r["status"],
                    "preview": json.dumps(r["data"])[:500]} for r in api_responses]
            API_LOG_FILE.write_text(json.dumps(log, indent=2))
            print(f"  Saved API call log → {API_LOG_FILE}")

        if not api_responses and not html:
            base["error"] = "Page failed to load after retries"
            return base

        # Try dedicated population endpoint first, fall back to sales aggregation
        grade_counts = extract_population_from_api(api_responses)
        if not grade_counts:
            grade_counts = extract_population_from_sales(api_responses)

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
                "Page loaded but no population data found in API responses. "
                f"Check {API_LOG_FILE} to inspect what the page returned."
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
