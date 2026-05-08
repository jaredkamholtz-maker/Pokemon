"""
Full pipeline: discover → AI filter → prices → PSA population → EV → image analysis → email

Steps:
  1. discover_cards: query Pokemon TCG API for all cards in target sets (~3,000 cards)
  2. filter_cards_ai: Claude Haiku pre-filter → holos, full arts, chase rares (~400-600)
  3. fetch_ebay_prices: get raw + PSA 9 + PSA 10 prices from eBay completed sales
     Filter: PSA 9 or PSA 10 > $60 (configurable MIN_GRADED_PRICE)
  4. scrape_pokedata_population: get PSA submission counts and gem rate from 130point.com
  5. calculate_flip_ev: merge prices + population, calculate ROI
     Filter: gem rate >= 35% AND ROI >= 10% after $25 grading fee
  6. analyze_card_images: find cheapest eBay raw listing per top-20 card, analyze photos
     with Claude Vision, keep only SUBMIT cards
  7. Email final shortlist: card, raw price, PSA 9/10, profit %, gem rate, predicted grade

Usage:
    python execution/run_analysis.py
    python execution/run_analysis.py --era scarlet-violet
    python execution/run_analysis.py --sets "151,Evolving Skies"
    python execution/run_analysis.py --skip-discovery  # reuse last discovered_cards.csv
    python execution/run_analysis.py --skip-prices     # reuse last ebay_prices.csv
    python execution/run_analysis.py --skip-images     # skip eBay image analysis step
    python execution/run_analysis.py --skip-sheets --skip-email
"""

import argparse
import os
import smtplib
import sys
import urllib.parse
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

import discover_cards as discover_mod
import filter_cards_ai as ai_filter_mod
import fetch_ebay_prices as prices_mod
import scrape_pokedata_population as pop_mod
import calculate_flip_ev as ev_mod
import analyze_card_images as images_mod

DISCOVERED_PATH = ".tmp/discovered_cards.csv"
FILTERED_PATH = ".tmp/filtered_cards.csv"
PRICES_PATH = ".tmp/ebay_prices.csv"
POP_PATH = ".tmp/pokedata_population.csv"
OUTPUT_PATH = ".tmp/flip_opportunities.csv"
SHORTLIST_PATH = ".tmp/final_shortlist.csv"


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
        f = float(val)
        return "—" if f != f else f"${f:.2f}"  # f != f is True only for NaN
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(val) -> str:
    try:
        f = float(val)
        return "—" if f != f else f"{f * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def format_email_body(opportunities: pd.DataFrame, today: str, has_image_analysis: bool = False) -> tuple[str, str]:
    """Return (html_body, plain_body)."""
    subject_line = f"Pokemon Card Flip Opportunities — {today}"
    if has_image_analysis:
        count_summary = (f"<strong>{len(opportunities)}</strong> cards passed all filters "
                         f"including eBay photo analysis (Claude Vision)")
    else:
        count_summary = f"<strong>{len(opportunities)}</strong> cards with PSA gem rate ≥ 35% and ROI ≥ 10%"

    if opportunities.empty:
        html = f"""<html><body style="font-family:sans-serif;color:#222;">
<h2>{subject_line}</h2>
<p>No cards met the criteria today (PSA 9/10 &gt; $60, gem rate ≥ 35%, ROI ≥ 10%).</p>
</body></html>"""
        plain = f"{subject_line}\n\nNo cards met the criteria today."
        return html, plain

    rows_html = []
    rows_plain = []
    for rank, (_, row) in enumerate(opportunities.iterrows(), 1):
        name = row.get("card_name", "?")
        set_name = row.get("set_name", "?")
        # Use the actual eBay listing price (what you'd pay) if captured; fall back to PriceCharting
        _ebay_price = row.get("ebay_price")
        _raw_price = row.get("raw_price")
        buy_price_val = _ebay_price if pd.notna(_ebay_price) else _raw_price
        raw = _fmt_price(buy_price_val)
        psa9 = _fmt_price(row.get("psa9_price"))
        psa10 = _fmt_price(row.get("psa10_price"))
        is_breakeven = row.get("track") == "breakeven"
        # Gem rate: show "52.5% (420 / 800)" for Track 1, "BE ≤ 8%" for Track 2
        gem_val = row.get("gem_rate")
        total = row.get("total_graded")
        psa9_count = row.get("psa9_count")
        psa10_count = row.get("psa10_count")
        if pd.notna(gem_val) and gem_val == gem_val:
            pct = f"{gem_val * 100:.1f}%"
            if pd.notna(psa9_count) and pd.notna(psa10_count) and pd.notna(total):
                gem = f"{pct} ({int(psa9_count + psa10_count):,} / {int(total):,})"
            else:
                gem = pct
        elif is_breakeven:
            be = row.get("breakeven_gem_rate")
            gem = f"BE ≤ {_fmt_pct(be)}" if pd.notna(be) and be == be else "No data"
        else:
            gem = "—"
        # Profit %: use ROI if available, else label as breakeven opportunity
        roi_val = row.get("roi")
        if pd.notna(roi_val) and roi_val == roi_val:
            roi = _fmt_pct(roi_val)
        elif is_breakeven:
            roi = "breakeven play"
        else:
            roi = "—"
        url = row.get("source_url") or ""
        _ebay_raw = row.get("ebay_listing_url")
        ebay_listing_url = str(_ebay_raw) if pd.notna(_ebay_raw) and _ebay_raw else ""

        # Broad eBay search — just card name + set, no BIN/condition filters so results always appear
        ebay_search_url = (
            "https://www.ebay.com/sch/i.html?"
            + urllib.parse.urlencode({
                "_nkw": f"{name} {set_name} pokemon",
                "_sop": "15",
            })
        )
        # Prefer a specific listing URL (from image analysis); fall back to search
        buy_url = ebay_listing_url or ebay_search_url

        # Image analysis columns (only present when step 6 ran)
        pred_grade = row.get("predicted_grade")
        psa9p = row.get("psa9_or_better_probability")
        notes = row.get("notes") or ""

        card_link = f'<a href="{url}" style="color:#1a73e8;text-decoration:none;">{name}</a>' if url else name
        if ebay_listing_url:
            # Specific listing Claude analyzed + search fallback in case it sold
            ebay_link = (
                f' <a href="{ebay_listing_url}" style="font-size:11px;color:#e67e00;font-weight:600;">[Buy this listing]</a>'
                f' <a href="{ebay_search_url}" style="font-size:11px;color:#6b7280;">[Search if sold]</a>'
            )
        else:
            ebay_link = f' <a href="{ebay_search_url}" style="font-size:11px;color:#e67e00;font-weight:600;">[Find on eBay]</a>'

        image_cells = ""
        image_plain = ""
        if has_image_analysis:
            pred_str = f"PSA {int(pred_grade)}" if pd.notna(pred_grade) else "—"
            prob_str = f"{int(psa9p)}%" if pd.notna(psa9p) else "—"
            image_cells = (
                f'<td style="padding:10px 14px;text-align:right;font-weight:700;color:#7c3aed;">{pred_str}</td>'
                f'<td style="padding:10px 14px;text-align:right;color:#7c3aed;">{prob_str}</td>'
            )
            image_plain = f"  Predicted: {pred_str}  PSA9+ Probability: {prob_str}"
            if notes:
                image_plain += f"\n  Note: {notes}"

        rows_html.append(f"""<tr style="border-bottom:1px solid #e5e7eb;">
  <td style="padding:10px 14px;font-weight:500;">{rank}. {card_link}{ebay_link}<br>
    <span style="font-size:12px;color:#6b7280;">{set_name}</span>
    {"<br><span style='font-size:11px;color:#9ca3af;font-style:italic;'>" + notes + "</span>" if has_image_analysis and notes else ""}
  </td>
  <td style="padding:10px 14px;text-align:right;">{raw}</td>
  <td style="padding:10px 14px;text-align:right;">{psa9}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:600;color:#15803d;">{psa10}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:700;color:#1d4ed8;">{roi}</td>
  <td style="padding:10px 14px;text-align:right;font-weight:700;color:#15803d;">{gem}</td>
  {image_cells}
</tr>""")

        link_text = f"\n  Buy on eBay: {buy_url}" + (f"\n  PriceCharting: {url}" if url else "")
        rows_plain.append(
            f"#{rank} {name} | {set_name}\n"
            f"  Raw: {raw}  PSA9: {psa9}  PSA10: {psa10}  Profit: {roi}  Gem Rate: {gem}"
            f"{image_plain}{link_text}"
        )

    image_headers = ""
    image_footer = ""
    if has_image_analysis:
        image_headers = (
            '<th style="padding:10px 14px;text-align:right;">Predicted Grade</th>'
            '<th style="padding:10px 14px;text-align:right;">PSA 9+ Probability</th>'
        )
        image_footer = " Cards shown passed eBay photo analysis (Claude Vision)."

    table_rows = "\n".join(rows_html)
    html = f"""<html><body style="font-family:sans-serif;color:#222;max-width:960px;margin:0 auto;">
<h2 style="color:#1e293b;">{subject_line}</h2>
<p style="color:#64748b;">{count_summary}</p>
<table style="width:100%;border-collapse:collapse;font-size:14px;">
  <thead>
    <tr style="background:#f1f5f9;text-align:left;">
      <th style="padding:10px 14px;">Card</th>
      <th style="padding:10px 14px;text-align:right;">{"Buy Price (eBay)" if has_image_analysis else "Raw (Ungraded)"}</th>
      <th style="padding:10px 14px;text-align:right;">PSA 9</th>
      <th style="padding:10px 14px;text-align:right;">PSA 10</th>
      <th style="padding:10px 14px;text-align:right;">Profit %</th>
      <th style="padding:10px 14px;text-align:right;">Gem Rate (gem / total)</th>
      {image_headers}
    </tr>
  </thead>
  <tbody>
{table_rows}
  </tbody>
</table>
<p style="font-size:12px;color:#94a3b8;margin-top:24px;">
  Profit % = ROI after $25 grading fee. Gem Rate = % of all PSA submissions that came back 9 or 10 (gem count / total graded). BE ≤ X% = breakeven if at least X% grade gem.{image_footer}
</p>
</body></html>"""

    plain = f"{subject_line}\n{len(opportunities)} cards\n\n"
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
    skip_images: bool = False,
    skip_sheets: bool = False,
    skip_email: bool = False,
    min_graded_price: float = 60.0,
    min_roi: float = 0.10,
    min_gem_rate: float = 0.35,
    image_top_n: int = 20,
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
        print(f"[1/6] Reusing {n} discovered cards from {DISCOVERED_PATH}")
    else:
        scope = f"era={era}" if era else (f"sets={sets}" if sets else "all sets")
        print(f"[1/6] Discovering cards via Pokemon TCG API ({scope})...")
        discover_mod.run(target_sets_path=target_sets, era=era, sets=sets)

    discovered_df = pd.read_csv(DISCOVERED_PATH)
    print(f"  → {len(discovered_df)} cards discovered\n")

    # Step 2: AI pre-filter (Claude Haiku)
    if skip_ai_filter and Path(FILTERED_PATH).exists():
        n = len(pd.read_csv(FILTERED_PATH))
        print(f"[2/6] Reusing {n} AI-filtered cards from {FILTERED_PATH}")
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

    # Step 3: Fetch prices from eBay completed sales
    if skip_prices and Path(PRICES_PATH).exists():
        n = len(pd.read_csv(PRICES_PATH))
        print(f"[3/6] Reusing {n} price records from {PRICES_PATH}")
    else:
        print(f"[3/6] Fetching eBay sold prices ({len(filtered_df)} cards, "
              f"PSA 9/10 > ${min_graded_price:.0f})...")
        prices_mod.run(input_path=FILTERED_PATH, output_path=PRICES_PATH)

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
    print(f"[4/6] Scraping PSA population from 130point.com ({len(prices_df)} cards)...")
    pop_mod.run(PRICES_PATH)
    print()

    # Step 5: Calculate EV
    print(f"[5/6] Calculating flip EV...")
    df = ev_mod.run(grading_fee=grading_fee, min_roi=min_roi)

    # Track 1: population data available — gem rate >= 50% AND roi >= 10%
    has_pop = df["gem_rate"].notna() & df["roi"].notna()
    track1 = df[
        has_pop &
        (df["gem_rate"] >= min_gem_rate) &
        (df["roi"] >= min_roi)
    ].copy()
    track1["track"] = "population"

    # Track 2: no population data, but spread is so good breakeven is very low
    # Surface cards where you'd break even needing < 15% gem rate — attractive even blind
    max_breakeven = float(os.environ.get("MAX_BREAKEVEN_GEM_RATE") or 0.15)
    has_breakeven = ("breakeven_gem_rate" in df.columns) and df["breakeven_gem_rate"].notna()
    track2 = df[
        has_breakeven &
        ~has_pop &
        (df["breakeven_gem_rate"] <= max_breakeven)
    ].copy()
    track2["track"] = "breakeven"

    opportunities = pd.concat([track1, track2], ignore_index=True)
    opportunities = opportunities.sort_values(
        ["roi", "breakeven_gem_rate"],
        ascending=[False, True],
        na_position="last",
    )

    # Save filtered results
    Path(OUTPUT_PATH).parent.mkdir(parents=True, exist_ok=True)
    opportunities.to_csv(OUTPUT_PATH, index=False)

    print(f"\n{'='*60}")
    print(f"{len(track1)} Track-1 cards (gem rate ≥ {min_gem_rate*100:.0f}%, ROI ≥ {min_roi*100:.0f}%)")
    print(f"{len(track2)} Track-2 cards (breakeven gem rate ≤ {max_breakeven*100:.0f}%, no pop data)")
    print(f"{len(opportunities)} total opportunities out of {len(prices_df)} price candidates")
    print(f"{'='*60}\n")

    # Step 6: eBay image analysis (final filter on top N)
    # analyze_card_images always saves a direct listing URL before analysis runs,
    # so every card in the result has a specific eBay link regardless of outcome.
    final = opportunities
    has_image_analysis = False
    if not skip_images and not opportunities.empty:
        print(f"[6/6] Analyzing eBay listing photos (top {image_top_n} cards, Claude Vision)...")
        shortlist = images_mod.run(input_path=OUTPUT_PATH, top_n=image_top_n)

        analysis_csv = Path(".tmp/image_analysis.csv")
        if shortlist is not None and not shortlist.empty:
            # SUBMIT cards: show only these with full image analysis details
            final = shortlist
            has_image_analysis = True
            Path(SHORTLIST_PATH).parent.mkdir(parents=True, exist_ok=True)
            shortlist.to_csv(SHORTLIST_PATH, index=False)
        elif analysis_csv.exists():
            # No SUBMIT cards — show all opportunities but attach whatever
            # eBay listing URLs and prices image analysis did find
            print("  No SUBMIT cards from image analysis — showing all opportunities with eBay links.")
            analysis_df = pd.read_csv(analysis_csv)
            lookup = {
                (str(r["card_name"]).strip(), str(r["set_name"]).strip()): r
                for _, r in analysis_df.iterrows()
            }
            final = opportunities.copy()
            final["ebay_listing_url"] = final.apply(
                lambda r: lookup.get(
                    (str(r["card_name"]).strip(), str(r["set_name"]).strip()), {}
                ).get("ebay_listing_url"), axis=1
            )
            final["ebay_price"] = final.apply(
                lambda r: lookup.get(
                    (str(r["card_name"]).strip(), str(r["set_name"]).strip()), {}
                ).get("ebay_price"), axis=1
            )
        print()
    elif skip_images:
        print("[6/6] Skipping image analysis (--skip-images)\n")

    # Google Sheets
    if not skip_sheets:
        spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
        if spreadsheet_id:
            tab = f"Flip Analysis {today}" + (" (Vision)" if has_image_analysis else "")
            push_to_google_sheets(final, spreadsheet_id, tab)
        else:
            print("  GOOGLE_SPREADSHEET_ID not set — skipping Sheets output.")

    # Email
    if not skip_email:
        html_body, plain_body = format_email_body(final, today, has_image_analysis=has_image_analysis)
        subject = f"Your {date.today().strftime('%m/%d')} Pokenalysis: {len(final)} Opportunit{'y' if len(final) == 1 else 'ies'} Found"
        send_email(html_body, plain_body, subject)

    # Print summary + eBay URL diagnostic
    if final.empty:
        print("No cards met all criteria today.")
    else:
        cols = ["card_name", "set_name", "raw_price", "psa9_price", "psa10_price",
                "gem_rate", "roi", "predicted_grade", "psa9_or_better_probability"]
        print(final[[c for c in cols if c in final.columns]].to_string(index=False))

    # Show eBay URL status for every card so we can diagnose in CI logs
    if "ebay_listing_url" in final.columns:
        print("\n── eBay listing URLs ──")
        for _, row in final.iterrows():
            url = row.get("ebay_listing_url")
            label = url if pd.notna(url) and url else "[MISSING — will show generic search link]"
            print(f"  {row.get('card_name', '?')} | {label}")
    else:
        print("\n[WARNING] ebay_listing_url column missing from final dataframe — all links will be generic")

    out = SHORTLIST_PATH if has_image_analysis else OUTPUT_PATH
    print(f"\nFinal results saved to: {out}")
    return final


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
    parser.add_argument("--skip-images", action="store_true",
                        help="Skip eBay image analysis step (faster, no Claude Vision credits)")
    parser.add_argument("--image-top-n", type=int, default=20,
                        help="Number of top cards to analyze with Claude Vision (default: 20)")
    parser.add_argument("--skip-sheets", action="store_true")
    parser.add_argument("--skip-email", action="store_true")
    parser.add_argument("--min-graded-price", type=float, default=60.0,
                        help="Min PSA 9 or PSA 10 price to include a card (default: $60)")
    parser.add_argument("--min-roi", type=float, default=0.10,
                        help="Min ROI after grading fee (default: 0.10 = 10%%)")
    parser.add_argument("--min-gem-rate", type=float, default=0.50,
                        help="Min gem rate to surface a card (default: 0.35 = 35%%)")
    args = parser.parse_args()

    sets_list = [s.strip() for s in args.sets.split(",")] if args.sets else None
    run(
        target_sets=args.target_sets,
        era=args.era,
        sets=sets_list,
        skip_discovery=args.skip_discovery,
        skip_ai_filter=args.skip_ai_filter,
        skip_prices=args.skip_prices,
        skip_images=args.skip_images,
        skip_sheets=args.skip_sheets,
        skip_email=args.skip_email,
        min_graded_price=args.min_graded_price,
        min_roi=args.min_roi,
        min_gem_rate=args.min_gem_rate,
        image_top_n=args.image_top_n,
    )
