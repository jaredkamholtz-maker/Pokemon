"""
Full pipeline: discover cards → AI filter → fetch prices → scrape PSA population → email

Steps:
  1. discover_cards: query PokeData.io for all cards in target sets (~3,000 cards)
  2. filter_cards_ai: Claude Haiku pre-filter → holos, full arts, chase rares (~400-600)
  3. fetch_tcgplayer_prices: get raw + PSA 9 + PSA 10 prices from PriceCharting
     Filter: PSA 9 or PSA 10 > $60 (configurable MIN_GRADED_PRICE)
  4. scrape_pokedata_population: get PSA submission counts and gem rate from 130point.com
  5. calculate_flip_ev: merge prices + population, calculate ROI
  6. Filter: gem rate >= 50% AND ROI >= 10% after $25 grading fee
  7. Email results: card name, raw price, PSA 9, PSA 10, profit %, gem rate, link

Usage:
    python execution/run_analysis.py
    python execution/run_analysis.py --era scarlet-violet
    python execution/run_analysis.py --sets "151,Evolving Skies"
    python execution/run_analysis.py --skip-discovery  # reuse last discovered_cards.csv
    python execution/run_analysis.py --skip-prices     # reuse last tcgplayer_prices.csv
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
import filter_cards_ai as ai_filter_mod
import fetch_tcgplayer_prices as prices_mod
import scrape_pokedata_population as pop_mod
import calculate_flip_ev as ev_mod

DISCOVERED_PATH = ".tmp/discovered_cards.csv"
FILTERED_PATH = ".tmp/filtered_cards.csv"
PRICES_PATH = ".tmp/tcgplayer_prices.csv"
POP_PATH = ".tmp/pokedata_population.csv"
OUTPUT_PATH = ".tmp/flip_opportunities.csv"


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
                return None

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(spreadsheet_id)
        try:
            ws = sh.worksheet(tab_name)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows=500, cols=20)

        df_out = df.copy().fillna("")
        ws.update([df_out.columns.tolist()] + df_out.values.tolist())
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        print(f"  Pushed {len(df)} rows to Google Sheets: {url}")
        return url

    except ImportError:
        print("  gspread not installed — skipping.")
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


def format_email_body(opportunities: pd.DataFrame, today: str) -> tuple[str, str]:
    """Return (html_body, plain_body)."""
    subject_line = f"Pokemon Card Flip Opportunities — {today}"
    count_summary = f"<strong>{len(opportunities)}</strong> cards with PSA gem rate ≥ 50% and ROI ≥ 10%"

    if opportunities.empty:
        html = f"""<html><body style="font-family:sans-serif;color:#222;">
<h2>{subject_line}</h2>
<p>No cards met the criteria today (PSA 9/10 &gt; $60, gem rate ≥ 50%, ROI ≥ 10%).</p>
</body></html>"""
        plain = f"{subject_line}\n\nNo cards met the criteria today."
        return html, plain

    rows_html = []
    rows_plain = []
    for rank, (_, row) in enumerate(opportunities.iterrows(), 1):
        name = row.get("card_name", "?")
        set_name = row.get("set_name", "?")
        raw = _fmt_price(row.get("raw_price"))
        psa9 = _fmt_price(row.get("psa9_price"))
        psa10 = _fmt_price(row.get("psa10_price"))
        gem = _fmt_pct(row.get("gem_rate"))
        roi = _fmt_pct(row.get("roi"))
        total = row.get("total_graded")
        total_str = f"{int(total):,}" if pd.notna(total) else "—"
        url = row.get("source_url") or ""

        name_cell = f'<a href="{url}" style="color:#1a73e8;text-decoration:none;">{name}</a>' if url else name

        rows_html.append(f"""<tr style="border-bottom:1px solid #e5e7eb;">
  <td style="padding:10px 14px;font-weight:500;">{rank}. {name_cell}<br>
    <span style="font-size:12px;color:#6b7280;">{set_name}</span></td>
  <td style="padding:10px 14px;text-align:right;">{raw}</td>
  <td style="padding:10px 14px;text-align:right;">{psa9}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:600;color:#15803d;">{psa10}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:700;color:#1d4ed8;">{roi}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:700;color:#15803d;">{gem}</td>
  <td style="padding:10px 14px;text-align:right;color:#6b7280;">{total_str}</td>
</tr>""")

        link_text = f"\n  {url}" if url else ""
        rows_plain.append(
            f"#{rank} {name} | {set_name}\n"
            f"  Raw: {raw}  PSA9: {psa9}  PSA10: {psa10}  Profit: {roi}  Gem Rate: {gem}  "
            f"Total Graded: {total_str}{link_text}"
        )

    table_rows = "\n".join(rows_html)
    html = f"""<html><body style="font-family:sans-serif;color:#222;max-width:860px;margin:0 auto;">
<h2 style="color:#1e293b;">{subject_line}</h2>
<p style="color:#64748b;">{count_summary}</p>
<table style="width:100%;border-collapse:collapse;font-size:14px;">
  <thead>
    <tr style="background:#f1f5f9;text-align:left;">
      <th style="padding:10px 14px;">Card</th>
      <th style="padding:10px 14px;text-align:right;">Raw (Ungraded)</th>
      <th style="padding:10px 14px;text-align:right;">PSA 9</th>
      <th style="padding:10px 14px;text-align:right;">PSA 10</th>
      <th style="padding:10px 14px;text-align:right;">Profit %</th>
      <th style="padding:10px 14px;text-align:right;">Gem Rate</th>
      <th style="padding:10px 14px;text-align:right;">Total Graded</th>
    </tr>
  </thead>
  <tbody>
{table_rows}
  </tbody>
</table>
<p style="font-size:12px;color:#94a3b8;margin-top:24px;">
  Profit % = ROI after $25 grading fee vs best graded price. Gem Rate = % of all PSA submissions grading 9 or 10. Only cards ≥ 50% gem rate shown.
</p>
</body></html>"""

    plain = f"{subject_line}\n{len(opportunities)} cards with gem rate ≥ 50% and ROI ≥ 10%\n\n"
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
        print("  Email not configured — skipping.")
        return False

    recipients = [a.strip() for a in email_to.split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

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
    target_sets: str = "data/target_sets.csv",
    era: str | None = None,
    sets: list[str] | None = None,
    skip_discovery: bool = False,
    skip_ai_filter: bool = False,
    skip_prices: bool = False,
    skip_sheets: bool = False,
    skip_email: bool = False,
    min_graded_price: float = 60.0,
    min_roi: float = 0.10,
    min_gem_rate: float = 0.50,
):
    load_dotenv()
    today = date.today().isoformat()
    grading_fee = float(os.environ.get("GRADING_FEE", 25.0))

    print(f"\n{'='*60}")
    print(f"Pokemon Flip Analysis  —  {today}")
    print(f"{'='*60}\n")

    # Step 1: Discover cards from PokeData.io
    if skip_discovery and Path(DISCOVERED_PATH).exists():
        n = len(pd.read_csv(DISCOVERED_PATH))
        print(f"[1/5] Reusing {n} discovered cards from {DISCOVERED_PATH}")
    else:
        scope = f"era={era}" if era else (f"sets={sets}" if sets else "all sets")
        print(f"[1/5] Discovering cards from PokeData.io ({scope})...")
        discover_mod.run(target_sets_path=target_sets, era=era, sets=sets)

    discovered_df = pd.read_csv(DISCOVERED_PATH)
    print(f"  → {len(discovered_df)} cards discovered\n")

    # Step 2: AI pre-filter (Claude Haiku)
    if skip_ai_filter and Path(FILTERED_PATH).exists():
        n = len(pd.read_csv(FILTERED_PATH))
        print(f"[2/5] Reusing {n} AI-filtered cards from {FILTERED_PATH}")
    else:
        print(f"[2/5] AI pre-filtering {len(discovered_df)} cards (Claude Haiku)...")
        filtered_df = ai_filter_mod.filter_cards(discovered_df)
        Path(FILTERED_PATH).parent.mkdir(parents=True, exist_ok=True)
        filtered_df.to_csv(FILTERED_PATH, index=False)

    filtered_df = pd.read_csv(FILTERED_PATH)
    print(f"  → {len(filtered_df)} cards after AI filter\n")

    if filtered_df.empty:
        print("No cards survived AI filter — nothing to analyze.")
        return pd.DataFrame()

    # Step 3: Fetch prices from PriceCharting
    if skip_prices and Path(PRICES_PATH).exists():
        n = len(pd.read_csv(PRICES_PATH))
        print(f"[3/5] Reusing {n} price records from {PRICES_PATH}")
    else:
        print(f"[3/5] Fetching prices from PriceCharting ({len(filtered_df)} cards, "
              f"PSA 9/10 > ${min_graded_price:.0f})...")
        prices_mod.run(watchlist_path=FILTERED_PATH)

    prices_df = pd.read_csv(PRICES_PATH)
    # Apply the PSA > $60 price filter
    has_graded = (
        (prices_df["psa10_price"].notna() & (prices_df["psa10_price"] > min_graded_price)) |
        (prices_df["psa9_price"].notna() & (prices_df["psa9_price"] > min_graded_price))
    )
    prices_df = prices_df[has_graded & prices_df["raw_price"].notna()]
    prices_df.to_csv(PRICES_PATH, index=False)
    print(f"  → {len(prices_df)} cards with PSA 9/10 > ${min_graded_price:.0f}\n")

    if prices_df.empty:
        print("No cards passed the price filter — nothing to analyze.")
        return pd.DataFrame()

    # Step 4: Scrape PSA population data from 130point.com
    print(f"[4/5] Scraping PSA population from 130point.com ({len(prices_df)} cards)...")
    pop_mod.run(PRICES_PATH)
    print()

    # Step 5: Calculate EV
    print(f"[5/5] Calculating flip EV...")
    df = ev_mod.run(grading_fee=grading_fee, min_roi=min_roi)

    # Apply final filters: gem rate >= 50% AND ROI >= 10%
    has_pop = df["gem_rate"].notna() & df["roi"].notna()
    opportunities = df[
        has_pop &
        (df["gem_rate"] >= min_gem_rate) &
        (df["roi"] >= min_roi)
    ].copy()
    opportunities = opportunities.sort_values("roi", ascending=False)

    # Save filtered results
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    opportunities.to_csv(OUTPUT_PATH, index=False)

    print(f"\n{'='*60}")
    print(f"{len(opportunities)} opportunities (gem rate ≥ {min_gem_rate*100:.0f}%, "
          f"ROI ≥ {min_roi*100:.0f}%) out of {len(prices_df)} price candidates")
    print(f"{'='*60}\n")

    # Google Sheets
    if not skip_sheets:
        spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
        if spreadsheet_id:
            push_to_google_sheets(opportunities, spreadsheet_id, f"Flip Analysis {today}")
        else:
            print("  GOOGLE_SPREADSHEET_ID not set — skipping Sheets output.")

    # Email
    if not skip_email:
        html_body, plain_body = format_email_body(opportunities, today)
        subject = f"[Pokemon Flip] {len(opportunities)} opportunities — {today}"
        send_email(html_body, plain_body, subject)

    # Print summary
    if opportunities.empty:
        print("No cards met all criteria today.")
    else:
        cols = ["card_name", "set_name", "raw_price", "psa9_price", "psa10_price",
                "gem_rate", "roi", "total_graded"]
        print(opportunities[[c for c in cols if c in opportunities.columns]].to_string(index=False))

    print(f"\nFull results saved to: {OUTPUT_PATH}")
    return opportunities


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pokemon card flip analysis")
    parser.add_argument("--target-sets", default="data/target_sets.csv")
    parser.add_argument("--era", default=None,
                        help="Filter to one era: mega-evolution, sword-shield, scarlet-violet")
    parser.add_argument("--sets", default=None,
                        help="Comma-separated set names, e.g. '151,Evolving Skies'")
    parser.add_argument("--skip-discovery", action="store_true",
                        help="Reuse last discovered_cards.csv (skip PokeData.io API call)")
    parser.add_argument("--skip-ai-filter", action="store_true",
                        help="Reuse last filtered_cards.csv (skip Claude Haiku step)")
    parser.add_argument("--skip-prices", action="store_true",
                        help="Reuse last tcgplayer_prices.csv (skip PriceCharting fetch)")
    parser.add_argument("--skip-sheets", action="store_true")
    parser.add_argument("--skip-email", action="store_true")
    parser.add_argument("--min-graded-price", type=float, default=60.0,
                        help="Min PSA 9 or PSA 10 price to include a card (default: $60)")
    parser.add_argument("--min-roi", type=float, default=0.10,
                        help="Min ROI after grading fee (default: 0.10 = 10%%)")
    parser.add_argument("--min-gem-rate", type=float, default=0.50,
                        help="Min gem rate to surface a card (default: 0.50 = 50%%)")
    args = parser.parse_args()

    sets_list = [s.strip() for s in args.sets.split(",")] if args.sets else None
    run(
        target_sets=args.target_sets,
        era=args.era,
        sets=sets_list,
        skip_discovery=args.skip_discovery,
        skip_ai_filter=args.skip_ai_filter,
        skip_prices=args.skip_prices,
        skip_sheets=args.skip_sheets,
        skip_email=args.skip_email,
        min_graded_price=args.min_graded_price,
        min_roi=args.min_roi,
        min_gem_rate=args.min_gem_rate,
    )
