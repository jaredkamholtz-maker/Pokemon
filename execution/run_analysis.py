"""
Full pipeline: load watchlist → fetch prices → scrape population → calculate EV
              → push to Google Sheet → send email summary.

This is the entry point. Run this daily (cron/manual) to get fresh opportunities.

Usage:
    python execution/run_analysis.py
    python execution/run_analysis.py --watchlist data/watchlist.csv --skip-sheets
    python execution/run_analysis.py --skip-email
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

# Add execution/ to path so sibling scripts are importable
sys.path.insert(0, str(Path(__file__).parent))

import fetch_tcgplayer_prices as prices_mod
import scrape_pokedata_population as pop_mod
import calculate_flip_ev as ev_mod


# ── Google Sheets ──────────────────────────────────────────────────────────────

def push_to_google_sheets(df: pd.DataFrame, spreadsheet_id: str, tab_name: str) -> str | None:
    """
    Write df to a Google Sheet. Returns the sheet URL or None if credentials missing.
    Requires credentials.json + token.json (Google OAuth2).
    """
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

        # Write header + data
        df_out = df.copy()
        df_out = df_out.fillna("")
        ws.update([df_out.columns.tolist()] + df_out.values.tolist())

        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        print(f"  Pushed {len(df)} rows to Google Sheets: {url}")
        return url

    except ImportError:
        print("  gspread not installed — skipping Sheets output. Run: pip install gspread google-auth")
        return None
    except Exception as e:
        print(f"  Google Sheets error: {e}")
        return None


# ── Email ──────────────────────────────────────────────────────────────────────

def format_email_body(opportunities: pd.DataFrame, all_analyzed: int, today: str) -> str:
    """Build a plain-text email body summarizing top flip opportunities."""
    lines = [
        f"Pokemon Card Flip Opportunities — {today}",
        f"Analyzed {all_analyzed} cards | {len(opportunities)} passed filters",
        "=" * 60,
        "",
    ]

    if opportunities.empty:
        lines.append("No cards met the ROI/gem-rate criteria today.")
        return "\n".join(lines)

    display_cols = [
        "card_name", "set_name", "raw_price", "psa9_price", "psa10_price",
        "gem_rate", "total_graded", "profit", "roi", "low_data"
    ]
    cols = [c for c in display_cols if c in opportunities.columns]

    for rank, (_, row) in enumerate(opportunities.iterrows(), 1):
        lines.append(f"#{rank}  {row.get('card_name', '?')}  |  {row.get('set_name', '?')}")
        lines.append(f"    Raw: ${row.get('raw_price', '?'):.2f}  →  PSA9: ${row.get('psa9_price', 0):.2f}  PSA10: ${row.get('psa10_price', 0):.2f}")
        lines.append(f"    Gem rate: {float(row.get('gem_rate', 0)) * 100:.1f}%  |  Pop: {int(row.get('total_graded', 0)):,}  |  Expected profit: ${row.get('profit', 0):.2f}  |  ROI: {float(row.get('roi', 0)) * 100:.1f}%")
        if row.get("low_data"):
            lines.append("    ⚠ Low data (<50 graded) — gem rate may not be reliable")
        if row.get("source_url"):
            lines.append(f"    pokedata: {row['source_url']}")
        lines.append("")

    return "\n".join(lines)


def send_email(body: str, subject: str) -> bool:
    """Send email via SMTP. Returns True on success."""
    load_dotenv()
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", 587))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    email_to = os.environ.get("EMAIL_TO")

    if not all([smtp_host, smtp_user, smtp_pass, email_to]):
        print("  Email not configured — skipping. Set SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO in .env")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, email_to, msg.as_string())
        print(f"  Email sent to {email_to}")
        return True
    except Exception as e:
        print(f"  Email failed: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def run(watchlist_path: str, skip_sheets: bool = False, skip_email: bool = False):
    load_dotenv()
    today = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Pokemon Flip Analysis  —  {today}")
    print(f"{'='*60}\n")

    # Step 1: Prices
    print("[1/4] Fetching prices from PriceCharting...")
    prices_df = prices_mod.run(watchlist_path)

    # Step 2: Population
    print("\n[2/4] Scraping pokedata.io population...")
    pop_df = pop_mod.run(watchlist_path)

    # Step 3: EV calculation
    print("\n[3/4] Calculating expected value...")
    all_df = ev_mod.run(
        grading_fee=float(os.environ.get("GRADING_FEE", 25.0)),
        selling_fee_rate=float(os.environ.get("SELLING_FEE_RATE", 0.13)),
    )
    opportunities = ev_mod.get_opportunities(all_df)

    # Step 4: Outputs
    print(f"\n[4/4] Delivering results — {len(opportunities)} opportunities found...")

    # Google Sheets
    if not skip_sheets:
        spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
        if spreadsheet_id:
            tab_name = f"Flip Analysis {today}"
            sheet_url = push_to_google_sheets(all_df, spreadsheet_id, tab_name)
        else:
            print("  GOOGLE_SPREADSHEET_ID not set — skipping Sheets output.")

    # Email
    if not skip_email:
        body = format_email_body(opportunities, len(all_df), today)
        subject = f"[Pokemon Flip] {len(opportunities)} opportunities — {today}"
        send_email(body, subject)

    # Always print to stdout
    if opportunities.empty:
        print("\nNo cards passed all filters today.")
    else:
        print(f"\nTop opportunities (sorted by ROI):\n")
        cols = ["card_name", "set_name", "raw_price", "psa10_price", "gem_rate",
                "total_graded", "profit", "roi"]
        print(opportunities[[c for c in cols if c in opportunities.columns]].to_string(index=False))

    print(f"\nFull analysis saved to: .tmp/flip_opportunities.csv")
    return opportunities


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Pokemon card flip analysis pipeline")
    parser.add_argument("--watchlist", default="data/watchlist.csv",
                        help="Path to watchlist CSV (default: data/watchlist.csv)")
    parser.add_argument("--skip-sheets", action="store_true",
                        help="Skip Google Sheets output")
    parser.add_argument("--skip-email", action="store_true",
                        help="Skip email notification")
    args = parser.parse_args()

    run(
        watchlist_path=args.watchlist,
        skip_sheets=args.skip_sheets,
        skip_email=args.skip_email,
    )
