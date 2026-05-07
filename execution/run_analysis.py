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

def _fmt_price(val) -> str:
    try:
        return f"${float(val):.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(val) -> str:
    try:
        return f"{float(val) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def format_email_body(opportunities: pd.DataFrame, all_analyzed: int, today: str) -> tuple[str, str]:
    """Return (html_body, plain_text_body) for the email."""
    subject_line = f"Pokemon Card Flip Opportunities — {today}"
    summary = f"Analyzed {all_analyzed} cards &nbsp;|&nbsp; <strong>{len(opportunities)} passed filters</strong>"

    if opportunities.empty:
        html = f"""<html><body style="font-family:sans-serif;color:#222;">
<h2>{subject_line}</h2>
<p>{summary}</p>
<p>No cards met the ROI/gem-rate criteria today.</p>
</body></html>"""
        plain = f"{subject_line}\nAnalyzed {all_analyzed} cards | {len(opportunities)} passed filters\n\nNo cards met the ROI/gem-rate criteria today."
        return html, plain

    rows_html = []
    rows_plain = []
    for rank, (_, row) in enumerate(opportunities.iterrows(), 1):
        name = row.get("card_name", "?")
        set_name = row.get("set_name", "?")
        raw = _fmt_price(row.get("raw_price"))
        psa9 = _fmt_price(row.get("psa9_price"))
        psa10 = _fmt_price(row.get("psa10_price"))
        profit = _fmt_price(row.get("profit"))
        roi = _fmt_pct(row.get("roi"))
        gem = _fmt_pct(row.get("gem_rate"))
        url = row.get("source_url") or ""
        low = row.get("low_data")

        name_cell = f'<a href="{url}" style="color:#1a73e8;text-decoration:none;">{name}</a>' if url else name
        low_badge = ' <span style="color:#b45309;font-size:11px;">⚠ low data</span>' if low else ""

        rows_html.append(f"""<tr style="border-bottom:1px solid #e5e7eb;">
  <td style="padding:10px 14px;font-weight:500;">{rank}. {name_cell}{low_badge}<br>
    <span style="font-size:12px;color:#6b7280;">{set_name}</span></td>
  <td style="padding:10px 14px;text-align:right;">{raw}</td>
  <td style="padding:10px 14px;text-align:right;">{psa9}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:600;color:#15803d;">{psa10}</td>
  <td style="padding:10px 14px;text-align:right;">{gem}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:600;color:#15803d;">{profit}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:700;color:#15803d;">{roi}</td>
</tr>""")

        link_text = f"  {url}" if url else ""
        low_note = "  ⚠ low data" if low else ""
        rows_plain.append(
            f"#{rank} {name} | {set_name}\n"
            f"  Raw: {raw}  PSA9: {psa9}  PSA10: {psa10}  Gem: {gem}  Profit: {profit}  ROI: {roi}"
            f"{low_note}{link_text}"
        )

    table_rows = "\n".join(rows_html)
    html = f"""<html><body style="font-family:sans-serif;color:#222;max-width:900px;margin:0 auto;">
<h2 style="color:#1e293b;">{subject_line}</h2>
<p style="color:#64748b;">{summary}</p>
<table style="width:100%;border-collapse:collapse;font-size:14px;">
  <thead>
    <tr style="background:#f1f5f9;text-align:left;">
      <th style="padding:10px 14px;">Card</th>
      <th style="padding:10px 14px;text-align:right;">Raw</th>
      <th style="padding:10px 14px;text-align:right;">PSA 9</th>
      <th style="padding:10px 14px;text-align:right;">PSA 10</th>
      <th style="padding:10px 14px;text-align:right;">Gem Rate</th>
      <th style="padding:10px 14px;text-align:right;">Profit</th>
      <th style="padding:10px 14px;text-align:right;">ROI</th>
    </tr>
  </thead>
  <tbody>
{table_rows}
  </tbody>
</table>
<p style="font-size:12px;color:#94a3b8;margin-top:24px;">
  Raw = ungraded market price &nbsp;|&nbsp; Profit and ROI assume {_fmt_pct(os.environ.get('SELLING_FEE_RATE', 0.13))} selling fee + grading cost
</p>
</body></html>"""

    plain = f"{subject_line}\nAnalyzed {all_analyzed} cards | {len(opportunities)} passed filters\n\n"
    plain += "\n\n".join(rows_plain)
    return html, plain


def send_email(html_body: str, plain_body: str, subject: str) -> bool:
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
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))  # HTML part last — preferred by email clients

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
        html_body, plain_body = format_email_body(opportunities, len(all_df), today)
        subject = f"[Pokemon Flip] {len(opportunities)} opportunities — {today}"
        send_email(html_body, plain_body, subject)

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
