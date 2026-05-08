"""
Final filter: find cheapest eBay raw listings for each top flip candidate,
analyze listing photos with Claude Vision, and pick the best quality copy.

Design principles:
  - Cheapest listing URL is ALWAYS saved before image analysis runs.
    The email always has a direct eBay link, even if image analysis fails.
  - Images come exclusively from the eBay Finding API response.
    No listing-page scraping — nothing to block, nothing to break.
  - Image analysis is additive: Claude's grade prediction and PSA 9+
    probability are bonus info. They never block a card from getting a link.

Steps per card:
  1. Search eBay Finding API for cheapest ungraded listings (up to MAX_LISTINGS)
  2. Save cheapest listing URL immediately as the fallback buy link
  3. For each listing: download the API-provided image, send to Claude Vision
  4. Pick the listing with highest PSA 9+ probability (prefer SUBMIT over SKIP)
  5. Update result with winning listing URL + Claude's grade assessment

Output:
  .tmp/image_analysis.csv   — full results for all analyzed cards
  .tmp/final_shortlist.csv  — SUBMIT cards only

Usage:
    python execution/analyze_card_images.py
    python execution/analyze_card_images.py --top 20
    python execution/analyze_card_images.py --input .tmp/flip_opportunities.csv
"""

import argparse
import base64
import json
import os
import re
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome124")
except ImportError:
    import requests as _req_fallback
    _SESSION = _req_fallback.Session()

INPUT_FILE = Path(".tmp/flip_opportunities.csv")
OUTPUT_ANALYSIS = Path(".tmp/image_analysis.csv")
OUTPUT_SHORTLIST = Path(".tmp/final_shortlist.csv")

EBAY_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
MAX_LISTINGS = 5        # eBay listings to evaluate per card
RATE_DELAY = 0.5        # seconds between API calls
MODEL = "claude-sonnet-4-6"

GRADING_PROMPT = """You are an experienced PSA grader examining a Pokemon card listed for sale on eBay.

Card details: {card_name} from {set_name}

Analyze every photo provided and assess the card's condition. Focus on:
- **Centering**: Is the card centered front and back? Estimate the ratio (e.g. 55/45).
- **Corners**: Are all four corners sharp or do any show wear/rounding/fraying?
- **Edges**: Are all edges clean or do any show chips, nicks, or roughness?
- **Surface**: Any scratches, print lines, holo damage, whitening, or indentations?

Then give your overall prediction and recommendation.

IMPORTANT RULES:
- If photos are too blurry, too dark, or don't show enough of the card to assess, set photo_quality to "INSUFFICIENT" and recommendation to "SKIP".
- Be conservative — a card needs to look genuinely clean to earn SUBMIT.
- PSA 10 requires near-perfect centering AND pristine corners/edges/surface.
- PSA 9 allows slight centering variance but corners/edges/surface must be excellent.

Respond ONLY with valid JSON in exactly this format:
{
  "centering": <1-10>,
  "corners": <1-10>,
  "edges": <1-10>,
  "surface": <1-10>,
  "predicted_grade": <1-10>,
  "psa10_probability": <0-100>,
  "psa9_or_better_probability": <0-100>,
  "recommendation": "SUBMIT" or "SKIP",
  "photo_quality": "GOOD" or "PARTIAL" or "INSUFFICIENT",
  "notes": "<one or two sentences on what you observed>"
}"""


def _get(url: str, params: dict | None = None, max_retries: int = 3) -> object | None:
    for attempt in range(1, max_retries + 1):
        try:
            resp = _SESSION.get(url, params=params, timeout=20)
            if resp.status_code == 500 and attempt < max_retries:
                wait = attempt * 10
                print(f"(HTTP 500, retrying in {wait}s...)", end=" ", flush=True)
                time.sleep(wait)
                continue
            return resp
        except Exception:
            if attempt < max_retries:
                time.sleep(attempt * 5)
    return None


def _is_graded_title(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in ["psa ", "psa-", "bgs ", "cgc ", "sgc ", "graded", "gem mint"])


# ── eBay search ────────────────────────────────────────────────────────────────

def search_ebay_listings(card_name: str, set_name: str) -> list[dict]:
    """
    Use eBay Finding API to get cheapest raw/ungraded listings.
    Returns up to MAX_LISTINGS candidates sorted by price ascending.
    Each dict: {title, price, url, image_url}
    """
    load_dotenv()
    app_id = os.environ.get("EBAY_APP_ID")
    if not app_id:
        print("  EBAY_APP_ID not set — skipping eBay search")
        return []

    params = {
        "OPERATION-NAME": "findItemsByKeywords",
        "SERVICE-VERSION": "1.13.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "keywords": f"{card_name} {set_name} pokemon",
        "itemFilter(0).name": "ListingType",
        "itemFilter(0).value(0)": "FixedPrice",
        "itemFilter(0).value(1)": "Auction",
        "sortOrder": "PricePlusShippingLowest",
        "paginationInput.entriesPerPage": str(MAX_LISTINGS * 3),
        "outputSelector(0)": "PictureURLSuperSize",
        "outputSelector(1)": "PictureURLLargeSize",
    }

    try:
        resp = _get(EBAY_FINDING_URL, params=params)
        if not resp or resp.status_code != 200:
            print(f"  eBay API error: HTTP {resp and resp.status_code}")
            return []

        data = resp.json()
        # Log the raw API ack/error so we can see in CI if something is wrong
        ack = (data.get("findItemsByKeywordsResponse", [{}])[0]
                   .get("ack", [""])[0])
        total_entries = (data.get("findItemsByKeywordsResponse", [{}])[0]
                             .get("searchResult", [{}])[0]
                             .get("@count", "?"))
        print(f"(API ack={ack}, totalResults={total_entries})", end=" ", flush=True)

        items = (data
                 .get("findItemsByKeywordsResponse", [{}])[0]
                 .get("searchResult", [{}])[0]
                 .get("item", []))

        candidates = []
        graded_skipped = 0
        for item in items:
            title = item.get("title", [""])[0]
            if _is_graded_title(title):
                graded_skipped += 1
                continue

            view_url = item.get("viewItemURL", [""])[0]
            price_str = (item.get("sellingStatus", [{}])[0]
                            .get("currentPrice", [{}])[0]
                            .get("__value__", "0"))
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                price = 0.0

            if price <= 0 or not view_url:
                continue

            # Best image the API provides — prefer largest size available
            image_url = (
                (item.get("pictureURLSuperSize") or [None])[0]
                or (item.get("pictureURLLargeSize") or [None])[0]
                or (item.get("galleryURL") or [None])[0]
            )

            candidates.append({
                "title": title,
                "price": price,
                "url": view_url,
                "image_url": image_url,
            })

            if len(candidates) >= MAX_LISTINGS:
                break

        if graded_skipped:
            print(f"({graded_skipped} graded listings skipped)", end=" ", flush=True)
        return sorted(candidates, key=lambda c: c["price"])[:MAX_LISTINGS]

    except Exception as e:
        print(f"  eBay API exception: {e}")
        return []


# ── Claude Vision analysis ─────────────────────────────────────────────────────

def download_image_b64(url: str) -> str | None:
    """Download an image URL and return base64-encoded bytes, or None on failure."""
    try:
        resp = _get(url)
        if resp and resp.status_code == 200 and resp.content:
            return base64.standard_b64encode(resp.content).decode("utf-8")
    except Exception:
        pass
    return None


def analyze_image(card_name: str, set_name: str, image_b64: str) -> dict:
    """
    Send a single listing image to Claude Vision for PSA grade assessment.
    Returns the parsed assessment dict, or a SKIP dict on any failure.
    """
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set", "recommendation": "SKIP"}

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "anthropic not installed", "recommendation": "SKIP"}

    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
        },
        {
            "type": "text",
            "text": GRADING_PROMPT.format(card_name=card_name, set_name=set_name),
        },
    ]

    try:
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        raw_clean = re.sub(r"```(?:json)?\s*", "", raw).strip()

        start = raw_clean.find("{")
        end = raw_clean.rfind("}") + 1
        if start == -1 or end == 0:
            return {"error": f"No JSON in response: {raw[:100]}", "recommendation": "SKIP"}

        result = json.loads(raw_clean[start:end])
        if not isinstance(result, dict):
            return {"error": "Response was not a JSON object", "recommendation": "SKIP"}

        return result

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}", "recommendation": "SKIP"}
    except Exception as e:
        return {"error": str(e), "recommendation": "SKIP"}


def pick_best_listing(card_name: str, set_name: str, listings: list[dict]) -> tuple[dict, dict]:
    """
    Analyze the API-provided image for each listing with Claude Vision.
    Returns (best_listing, analysis_dict).

    best_listing always defaults to listings[0] (cheapest) so there is always
    a direct eBay URL regardless of whether image analysis succeeds.
    Best = highest psa9_or_better_probability, preferring SUBMIT over SKIP.
    """
    best_listing = listings[0]   # cheapest — guaranteed fallback URL
    best_analysis: dict = {}
    best_score = -1

    for i, listing in enumerate(listings, 1):
        image_url = listing.get("image_url")
        if not image_url:
            print(f"  [{i}/{len(listings)}] ${listing['price']:.2f} — no image from API")
            continue

        print(f"  [{i}/{len(listings)}] ${listing['price']:.2f} — downloading image...", end=" ", flush=True)
        time.sleep(RATE_DELAY)
        b64 = download_image_b64(image_url)
        if not b64:
            print("download failed")
            continue

        print("analyzing...", end=" ", flush=True)
        analysis = analyze_image(card_name, set_name, b64)
        rec = analysis.get("recommendation", "SKIP")
        prob = analysis.get("psa9_or_better_probability") or 0
        print(f"{rec} (PSA 9+: {prob}%)")

        submit_bonus = 1000 if rec == "SUBMIT" else 0
        score = submit_bonus + prob
        if score > best_score:
            best_score = score
            best_listing = listing
            best_analysis = analysis

    return best_listing, best_analysis


# ── Main run ───────────────────────────────────────────────────────────────────

def run(input_path: str = str(INPUT_FILE), top_n: int = 20) -> pd.DataFrame:
    load_dotenv()

    df = pd.read_csv(input_path)
    if df.empty:
        print("No flip opportunities to analyze.")
        return pd.DataFrame()

    sort_col = "roi" if "roi" in df.columns else df.columns[0]
    candidates = df.sort_values(sort_col, ascending=False).head(top_n).copy()
    print(f"Analyzing top {len(candidates)} cards...\n")

    rows = []
    for rank, (_, card) in enumerate(candidates.iterrows(), 1):
        card_name = str(card.get("card_name", "")).strip()
        set_name = str(card.get("set_name", "")).strip()
        print(f"[{rank}/{len(candidates)}] {card_name} | {set_name}")

        result = {
            "card_name": card_name,
            "set_name": set_name,
            "card_number": card.get("card_number"),
            "raw_price": card.get("raw_price"),
            "psa9_price": card.get("psa9_price"),
            "psa10_price": card.get("psa10_price"),
            "gem_rate": card.get("gem_rate"),
            "total_graded": card.get("total_graded"),
            "psa9_count": card.get("psa9_count"),
            "psa10_count": card.get("psa10_count"),
            "roi": card.get("roi"),
            "track": card.get("track"),
            "breakeven_gem_rate": card.get("breakeven_gem_rate"),
            "ebay_listing_url": None,
            "ebay_price": None,
            "centering": None,
            "corners": None,
            "edges": None,
            "surface": None,
            "predicted_grade": None,
            "psa10_probability": None,
            "psa9_or_better_probability": None,
            "recommendation": "NO_DATA",
            "photo_quality": None,
            "notes": None,
            "error": None,
        }

        # Step 1: Search eBay
        print(f"  Searching eBay...", end=" ", flush=True)
        time.sleep(RATE_DELAY)
        listings = search_ebay_listings(card_name, set_name)

        if not listings:
            result["error"] = "No eBay listings found"
            print("none found")
            rows.append(result)
            continue

        price_range = f"${listings[0]['price']:.2f}" + (
            f"–${listings[-1]['price']:.2f}" if len(listings) > 1 else ""
        )
        print(f"{len(listings)} listings ({price_range})")

        # Step 2: Always save cheapest listing URL as the guaranteed fallback
        result["ebay_listing_url"] = listings[0]["url"]
        result["ebay_price"] = listings[0]["price"]

        # Step 3: Analyze images, pick the best quality listing
        best_listing, analysis = pick_best_listing(card_name, set_name, listings)

        # Update to best listing (may be same as cheapest if analysis picked it or failed)
        result["ebay_listing_url"] = best_listing["url"]
        result["ebay_price"] = best_listing["price"]

        if analysis:
            result.update({
                "centering":                  analysis.get("centering"),
                "corners":                    analysis.get("corners"),
                "edges":                      analysis.get("edges"),
                "surface":                    analysis.get("surface"),
                "predicted_grade":            analysis.get("predicted_grade"),
                "psa10_probability":          analysis.get("psa10_probability"),
                "psa9_or_better_probability": analysis.get("psa9_or_better_probability"),
                "recommendation":             analysis.get("recommendation", "SKIP"),
                "photo_quality":              analysis.get("photo_quality"),
                "notes":                      analysis.get("notes"),
                "error":                      analysis.get("error"),
            })

        rec = result["recommendation"]
        grade = result.get("predicted_grade", "?")
        prob = result.get("psa9_or_better_probability", "?")
        print(f"  → {best_listing['url']}")
        print(f"  → ${best_listing['price']:.2f} | {rec} | predicted PSA {grade} | PSA 9+: {prob}%\n")

        rows.append(result)

    analysis_df = pd.DataFrame(rows)
    OUTPUT_ANALYSIS.parent.mkdir(parents=True, exist_ok=True)
    analysis_df.to_csv(OUTPUT_ANALYSIS, index=False)
    print(f"Full analysis saved → {OUTPUT_ANALYSIS}")

    shortlist = analysis_df[analysis_df["recommendation"] == "SUBMIT"].copy()
    shortlist.to_csv(OUTPUT_SHORTLIST, index=False)

    total = len(candidates)
    found = (analysis_df["ebay_listing_url"].notna()).sum()
    submit = len(shortlist)
    print(f"\n{'='*60}")
    print(f"eBay listings found: {found}/{total}")
    print(f"SUBMIT (PSA 9/10 candidate): {submit}/{found}")
    print(f"{'='*60}\n")

    return shortlist
