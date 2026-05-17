"""
Full pipeline entry point for Raspberry Pi (or any local machine).

Usage:
    python3 run.py                  # scrape PPT + analyze + email
    python3 run.py --all-pages      # scrape all 46 PPT pages
    python3 run.py --skip-images    # skip eBay photo analysis
    python3 run.py --skip-email     # analyze but don't send email
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from execution.scrape_ppt import run as scrape
from execution.run_analysis import run as analyze


def main():
    parser = argparse.ArgumentParser(description="Pokemon PSA flip analyzer")
    parser.add_argument("--all-pages", action="store_true",
                        help="Scrape all PPT pages instead of just page 1")
    parser.add_argument("--target-sets", default=None, metavar="PATH",
                        help="CSV with set_name column to filter cards")
    parser.add_argument("--skip-images", action="store_true",
                        help="Skip eBay photo analysis step")
    parser.add_argument("--skip-email", action="store_true",
                        help="Don't send email (still saves results to .tmp/)")
    parser.add_argument("--skip-sheets", action="store_true",
                        help="Don't push to Google Sheets")
    args = parser.parse_args()

    print("Step 1/2: Scraping pokemonpricetracker.com...")
    cards = scrape(all_pages=args.all_pages, target_sets_path=args.target_sets)
    if not cards:
        print("No cards scraped. Exiting.")
        sys.exit(1)

    print("\nStep 2/2: Running analysis...")
    analyze(
        skip_images=args.skip_images,
        skip_email=args.skip_email,
        skip_sheets=args.skip_sheets,
    )


if __name__ == "__main__":
    main()
