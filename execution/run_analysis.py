"""
Full pipeline: discover cards → fetch prices → scrape population → calculate EV
              → push to Google Sheet → send email summary.

Runs card discovery from data/target_sets.csv by default (full market scan).
Pass --watchlist to analyze a specific list of cards instead.

Usage:
    python execution/run_analysis.py                          # full market scan
    python execution/run_analysis.py --skip-discovery         # reuse last discovered_cards.csv
    python execution/run_analysis.py --watchlist data/watchlist.csv  # fixed card list
    python execution/run_analysis.py --skip-sheets --skip-email
"""

import argparse
import os
import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

import discover_cards as discover_mod
import fetch_tcgplayer_prices as prices_mod
import scrape_pokedata_population as pop_mod
import calculate_flip_ev as ev_mod
import filter_cards_ai as ai_mod

DISCOVERED_PATH = ".tmp/discovered_cards.csv"


# ── Google Sheets ──────────────────────────────────────────────────────────────

def push_to_google_sheets(df: pd.DataFrame, spreadsheet_id: str, tab_name: str) -> str | None:
    try:
        import gspread
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = None

        if Path("token.json").exists():
            creds = Credentials.from_authorized_user_file("token.json", scopes)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                print("  Google credentials not set up — skipping Sheets output.")
                print("  Run: python execution/setup_google_auth.py to authenticate.")
                return None

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(spreadsheet_id)

        try:
            ws = sh.worksheet(tab_name)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows=500, cols=30)

        df_out = df.copy().fillna("")
        ws.update([df_out.columns.tolist()] + df_out.values.tolist())

        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        print(f"  Pushed {len(df)} rows to Google Sheets: {url}")
        return url

    except ImportError:
        print("  gspread not installed — skipping. Run: pip install gspread google-auth")
        return None
    except Exception as e:
        print(f"  Google Sheets error: {e}")
        return None


# ── Email ──────────────────────────────────────────────────────────────────────

def format_email_body(opportunities: pd.DataFrame, all_analyzed: int, today: str) -> str:
    lines = [
        f"Pokemon Card Flip Opportunities — {today}",
        f"Analyzed {all_analyzed} cards | {len(opportunities)} passed filters",
        "=" * 60,
        "",
    ]

    if opportunities.empty:
        lines.append("No cards met the ROI/gem-rate criteria today.")
        return "\n".join(lines)

    for rank, (_, row) in enumerate(opportunities.iterrows(), 1):
        lines.append(f"#{rank}  {row.get('card_name', '?')}  |  {row.get('set_name', '?')}")
        lines.append(
            f"    Raw: ${row.get('raw_price', 0):.2f}  →  "
            f"PSA9: ${row.get('psa9_price', 0):.2f}  "
            f"PSA10: ${row.get('psa10_price', 0):.2f}"
        )
        lines.append(
            f"    Gem rate: {float(row.get('gem_rate', 0)) * 100:.1f}%  |  "
            f"Pop: {int(row.get('total_graded', 0)):,}  |  "
            f"Expected profit: ${row.get('profit', 0):.2f}  |  "
            f"ROI: {float(row.get('roi', 0)) * 100:.1f}%"
        )
        if row.get("low_data"):
            lines.append("    ⚠ Low data (<50 graded) — gem rate may not be reliable")
        if row.get("source_url"):
            lines.append(f"    source: {row['source_url']}")
        lines.append("")

    return "\n".join(lines)


def send_email(body: str, subject: str) -> bool:
    load_dotenv()
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    email_to = os.environ.get("EMAIL_TO")

    if not all([smtp_host, smtp_user, smtp_pass, email_to]):
        print("  Email not configured — skipping. Set SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO in .env")
        return False

    # EMAIL_TO supports comma-separated addresses: you@example.com,client@example.com
    recipients = [addr.strip() for addr in email_to.split(",") if addr.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, recipients, msg.as_string())
        print(f"  Email sent to {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"  Email failed: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def run(
    watchlist: str | None = None,
    target_sets: str = "data/target_sets.csv",
    era: str | None = None,
    sets: list[str] | None = None,
    skip_discovery: bool = False,
    skip_sheets: bool = False,
    skip_email: bool = False,
):
    load_dotenv()
    today = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Pokemon Flip Analysis  —  {today}")
    print(f"{'='*60}\n")

    # Determine card source: fixed watchlist or market discovery (full / filtered)
    if watchlist:
        card_source = watchlist
        print(f"[MODE] Fixed watchlist: {watchlist}")
    else:
        card_source = DISCOVERED_PATH
        scope = f"era={era}" if era else (f"sets={sets}" if sets else "full universe")
        if skip_discovery and Path(DISCOVERED_PATH).exists():
            n = len(pd.read_csv(DISCOVERED_PATH))
            print(f"[MODE] Market scan ({scope}) — reusing {n} previously discovered cards")
        else:
            print(f"[0/?] Discovering cards ({scope}) from {target_sets}...")
            discover_mod.run(target_sets_path=target_sets, output_path=DISCOVERED_PATH,
                             era=era, sets=sets)
            n = len(pd.read_csv(DISCOVERED_PATH))
            print(f"  → {n} cards discovered\n")

    # AI pre-filter: drop commons/trainers/bulk before expensive scraping
    print("[PRE] AI filtering for PSA flip candidates...")
    card_df = pd.read_csv(card_source)
    filtered_df = ai_mod.filter_cards(card_df)
    filtered_path = ".tmp/filtered_cards.csv"
    filtered_df.to_csv(filtered_path, index=False)
    card_source = filtered_path

    total_cards = len(filtered_df)
    print(f"Analyzing {total_cards} cards...\n")

    # Step 1: Prices
    print("[1/3] Fetching prices from PriceCharting (parallel)...")
    prices_df = prices_mod.run(card_source)

    # Step 2: Population
    print("\n[2/3] Scraping population data from 130point.com (parallel)...")
    pop_df = pop_mod.run(card_source)

    # Step 3: EV calculation
    print("\n[3/3] Calculating expected value...")
    all_df = ev_mod.run(
        grading_fee=float(os.environ.get("GRADING_FEE", 25.0)),
        selling_fee_rate=float(os.environ.get("SELLING_FEE_RATE", 0.13)),
    )
    opportunities = ev_mod.get_opportunities(all_df)

    print(f"\n{'='*60}")
    print(f"{len(opportunities)} opportunities found out of {len(all_df)} analyzed")
    print(f"{'='*60}\n")

    # Google Sheets
    if not skip_sheets:
        spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
        if spreadsheet_id:
            tab_name = f"Flip Analysis {today}"
            push_to_google_sheets(all_df, spreadsheet_id, tab_name)
        else:
            print("  GOOGLE_SPREADSHEET_ID not set — skipping Sheets output.")

    # Email
    if not skip_email:
        body = format_email_body(opportunities, len(all_df), today)
        subject = f"[Pokemon Flip] {len(opportunities)} opportunities — {today}"
        send_email(body, subject)

    # Print top opportunities
    if opportunities.empty:
        print("No cards passed all filters today.")
    else:
        print(f"Top opportunities (sorted by ROI):\n")
        cols = ["card_name", "set_name", "raw_price", "psa10_price",
                "gem_rate", "total_graded", "profit", "roi"]
        print(opportunities[[c for c in cols if c in opportunities.columns]].to_string(index=False))

    print(f"\nFull analysis saved to: .tmp/flip_opportunities.csv")
    return opportunities


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Pokemon card flip analysis")
    parser.add_argument(
        "--watchlist",
        default=None,
        help="Path to a fixed card list CSV. Omit for full market scan (uses target_sets.csv).",
    )
    parser.add_argument(
        "--target-sets",
        default="data/target_sets.csv",
        help="Sets to scan for full market mode (default: data/target_sets.csv)",
    )
    parser.add_argument(
        "--era",
        default=None,
        help="Filter discovery to one era: vintage, sword-shield, scarlet-violet",
    )
    parser.add_argument(
        "--sets",
        default=None,
        help="Comma-separated set names to scan, e.g. '151,Evolving Skies,Obsidian Flames'",
    )
    parser.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Skip card discovery and reuse the last .tmp/discovered_cards.csv",
    )
    parser.add_argument("--skip-sheets", action="store_true", help="Skip Google Sheets output")
    parser.add_argument("--skip-email", action="store_true", help="Skip email notification")
    args = parser.parse_args()

    sets_list = [s.strip() for s in args.sets.split(",")] if args.sets else None
    run(
        watchlist=args.watchlist,
        target_sets=args.target_sets,
        era=args.era,
        sets=sets_list,
        skip_discovery=args.skip_discovery,
        skip_sheets=args.skip_sheets,
        skip_email=args.skip_email,
    )
