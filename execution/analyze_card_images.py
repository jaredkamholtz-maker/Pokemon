"""
Final filter: find cheapest eBay raw listing for each top flip candidate,
analyze listing photos with Claude Vision, and keep only SUBMIT cards.

Steps per card:
  1. Search eBay for cheapest active ungraded listing
  2. Download up to MAX_IMAGES listing photos
  3. Send images to Claude Vision with a PSA grading assessment prompt
  4. Keep cards where recommendation == SUBMIT

Output:
  .tmp/image_analysis.csv   — full results with grade predictions
  .tmp/final_shortlist.csv  — SUBMIT cards only, ready for email

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
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome124")
    _USE_CFFI = True
except ImportError:
    import requests as _req_fallback
    _SESSION = _req_fallback.Session()
    _USE_CFFI = False

INPUT_FILE = Path(".tmp/flip_opportunities.csv")
OUTPUT_ANALYSIS = Path(".tmp/image_analysis.csv")
OUTPUT_SHORTLIST = Path(".tmp/final_shortlist.csv")
DEBUG_DIR = Path(".tmp/debug_ebay")

EBAY_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
MAX_IMAGES = 5          # images to send to Claude per card
MAX_LISTINGS = 5        # eBay listings to evaluate per card
RATE_DELAY = 1.5        # seconds between eBay requests
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── eBay search ────────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> object | None:
    try:
        if _USE_CFFI:
            return _SESSION.get(url, params=params, headers=HEADERS, timeout=20, allow_redirects=True)
        return _SESSION.get(url, params=params, headers=HEADERS, timeout=20, allow_redirects=True)
    except Exception:
        return None


def _save_debug(filename: str, content: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / filename
    if not path.exists():
        path.write_text(content, encoding="utf-8", errors="replace")


def _is_graded_title(title: str) -> bool:
    """Return True if the listing title suggests a graded copy (PSA/BGS/CGC)."""
    t = title.lower()
    return any(kw in t for kw in ["psa ", "psa-", "bgs ", "cgc ", "sgc ", "graded", "gem mint"])


def search_ebay_listings(card_name: str, set_name: str) -> list[dict]:
    """
    Use eBay Finding API to get cheapest raw/ungraded listings.
    Returns up to MAX_LISTINGS candidates: title, price, url, api_image_url.
    Requires EBAY_APP_ID in environment.
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
        "outputSelector(0)": "PictureURLLargeSize",
        "outputSelector(1)": "PictureURLSuperSize",
    }

    try:
        resp = _get(EBAY_FINDING_URL, params=params)
        if not resp or resp.status_code != 200:
            print(f"  eBay API returned {resp and resp.status_code}")
            return []

        data = resp.json()
        items = (data
                 .get("findItemsByKeywordsResponse", [{}])[0]
                 .get("searchResult", [{}])[0]
                 .get("item", []))

        candidates = []
        for item in items:
            title = item.get("title", [""])[0]
            if _is_graded_title(title):
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

            # Best image the API provides (used as fallback if listing page scrape fails)
            api_image_url = (
                (item.get("pictureURLSuperSize") or [None])[0] or
                (item.get("pictureURLLargeSize") or [None])[0] or
                (item.get("galleryURL") or [None])[0]
            )

            candidates.append({
                "title": title,
                "price": price,
                "url": view_url,
                "api_image_url": api_image_url,
            })

            if len(candidates) >= MAX_LISTINGS:
                break

        candidates.sort(key=lambda c: c["price"])
        return candidates[:MAX_LISTINGS]

    except Exception as e:
        print(f"  eBay API error: {e}")
        return []


def pick_best_listing(card_name: str, set_name: str, listings: list[dict]) -> tuple[dict | None, dict]:
    """
    Analyze photos from each listing with Claude Vision and return the best one.
    Best = highest psa9_or_better_probability among SUBMIT candidates.
    Falls back to highest-scoring listing if none are SUBMIT.

    Returns (winning_listing, analysis_dict).
    """
    best_listing = None
    best_analysis: dict = {"recommendation": "SKIP"}
    best_score = -1

    for i, listing in enumerate(listings, 1):
        print(f"  Listing {i}/{len(listings)} (${listing['price']:.2f})...", end=" ", flush=True)
        time.sleep(RATE_DELAY)
        image_urls = get_listing_images(listing["url"], listing.get("api_image_url"))
        if not image_urls:
            print("no images")
            continue
        print(f"{len(image_urls)} images → analyzing...", end=" ", flush=True)
        analysis = analyze_images(card_name, set_name, image_urls)
        rec = analysis.get("recommendation", "SKIP")
        prob = analysis.get("psa9_or_better_probability") or 0
        print(f"{rec} (PSA 9+ prob: {prob}%)")

        # SUBMIT beats SKIP; within same recommendation, higher probability wins
        submit_bonus = 1000 if rec == "SUBMIT" else 0
        score = submit_bonus + prob
        if score > best_score:
            best_score = score
            best_listing = listing
            best_analysis = analysis

    return best_listing, best_analysis


def get_listing_images(listing_url: str, api_image_url: str | None = None) -> list[str]:
    """
    Fetch listing photos. Tries the item page first; falls back to the API-provided
    image if the page is blocked or returns no images.
    """
    time.sleep(RATE_DELAY)
    resp = _get(listing_url)
    if not resp or resp.status_code != 200:
        return [api_image_url] if api_image_url else []

    html = resp.text
    _save_debug("listing_page.html", html)

    image_urls: list[str] = []

    # Strategy 1: eBay embeds image data as JSON in a script tag
    for script in BeautifulSoup(html, "html.parser").find_all("script"):
        text = script.string or ""
        # Look for image URL arrays in the page JSON
        matches = re.findall(r'"(?:originalImg|maxImageUrl|imageUrl|PictureURL)":\s*"(https://i\.ebayimg\.com[^"]+)"', text)
        for url in matches:
            # Prefer s-l1600 (highest res); fall back to what we find
            clean = re.sub(r"s-l\d+", "s-l1600", url)
            if clean not in image_urls:
                image_urls.append(clean)

    # Strategy 2: img tags pointing to ebayimg.com
    if not image_urls:
        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-zoom-src") or ""
            if "ebayimg.com" in src and "s-l" in src:
                clean = re.sub(r"s-l\d+", "s-l1600", src)
                if clean not in image_urls:
                    image_urls.append(clean)

    # Strategy 3: fall back to API-provided image if page scraping got nothing
    if not image_urls and api_image_url:
        image_urls.append(api_image_url)

    return image_urls[:MAX_IMAGES]


def download_image_b64(url: str) -> str | None:
    """Download an image and return base64-encoded content, or None on failure."""
    try:
        resp = _get(url)
        if resp and resp.status_code == 200:
            return base64.standard_b64encode(resp.content).decode("utf-8")
    except Exception:
        pass
    return None


# ── Claude Vision analysis ─────────────────────────────────────────────────────

def analyze_images(card_name: str, set_name: str, image_urls: list[str]) -> dict:
    """
    Send listing images to Claude Vision and return the grade assessment dict.
    Returns a dict with assessment keys, plus an 'error' key on failure.
    """
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set", "recommendation": "SKIP"}

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "anthropic not installed", "recommendation": "SKIP"}

    # Download images
    images_b64 = []
    for url in image_urls:
        b64 = download_image_b64(url)
        if b64:
            images_b64.append(b64)
        time.sleep(0.3)

    if not images_b64:
        return {"error": "No images could be downloaded", "recommendation": "SKIP",
                "photo_quality": "INSUFFICIENT"}

    # Build content blocks: one image block per photo
    content = []
    for b64 in images_b64:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    content.append({
        "type": "text",
        "text": GRADING_PROMPT.format(card_name=card_name, set_name=set_name),
    })

    client = Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        raw_clean = re.sub(r"```(?:json)?\s*", "", raw).strip()

        # Try to extract the JSON object — find outermost { ... }
        start = raw_clean.find("{")
        end = raw_clean.rfind("}") + 1
        if start == -1 or end == 0:
            return {"error": f"No JSON object found in response: {raw[:200]}", "recommendation": "SKIP"}

        try:
            result = json.loads(raw_clean[start:end])
        except json.JSONDecodeError as e:
            return {"error": f"JSON parse error ({e}): {raw_clean[start:end][:200]}", "recommendation": "SKIP"}

        if not isinstance(result, dict):
            return {"error": f"Expected JSON object, got {type(result).__name__}", "recommendation": "SKIP"}

        result["images_analyzed"] = len(images_b64)
        return result
    except Exception as e:
        return {"error": str(e), "recommendation": "SKIP"}


# ── Main run ───────────────────────────────────────────────────────────────────

def run(input_path: str = str(INPUT_FILE), top_n: int = 20) -> pd.DataFrame:
    load_dotenv()

    df = pd.read_csv(input_path)
    if df.empty:
        print("No flip opportunities to analyze.")
        return pd.DataFrame()

    # Take top N by ROI (already sorted, but be safe)
    sort_col = "roi" if "roi" in df.columns else df.columns[0]
    candidates = df.sort_values(sort_col, ascending=False).head(top_n).copy()
    print(f"Analyzing top {len(candidates)} cards from {input_path}...\n")

    rows = []
    for rank, (_, card) in enumerate(candidates.iterrows(), 1):
        card_name = card.get("card_name", "")
        set_name = card.get("set_name", "")
        print(f"[{rank}/{len(candidates)}] {card_name} | {set_name}")

        result = {
            "card_name": card_name,
            "set_name": card.get("set_name", ""),
            "card_number": card.get("card_number", ""),
            "raw_price": card.get("raw_price"),
            "psa9_price": card.get("psa9_price"),
            "psa10_price": card.get("psa10_price"),
            "gem_rate": card.get("gem_rate"),
            "roi": card.get("roi"),
            "ebay_listing_url": None,
            "ebay_price": None,
            "images_analyzed": 0,
            "centering": None,
            "corners": None,
            "edges": None,
            "surface": None,
            "predicted_grade": None,
            "psa10_probability": None,
            "psa9_or_better_probability": None,
            "recommendation": "SKIP",
            "photo_quality": None,
            "notes": None,
            "error": None,
        }

        # Step 1: Find eBay listings (up to MAX_LISTINGS cheapest raw copies)
        print(f"  Searching eBay...", end=" ", flush=True)
        time.sleep(RATE_DELAY)
        listings = search_ebay_listings(card_name, set_name)
        if not listings:
            result["error"] = "No eBay listings found"
            print("none found")
            rows.append(result)
            continue
        print(f"{len(listings)} listings found (${listings[0]['price']:.2f}–${listings[-1]['price']:.2f})")

        # Step 2 & 3: Fetch images for each listing and pick best via Claude Vision
        best_listing, analysis = pick_best_listing(card_name, set_name, listings)

        if not best_listing:
            result["error"] = "No images found across any listing"
            rows.append(result)
            continue

        result["ebay_listing_url"] = best_listing["url"]
        result["ebay_price"] = best_listing["price"]
        result.update({
            "images_analyzed": analysis.get("images_analyzed", 0),
            "centering": analysis.get("centering"),
            "corners": analysis.get("corners"),
            "edges": analysis.get("edges"),
            "surface": analysis.get("surface"),
            "predicted_grade": analysis.get("predicted_grade"),
            "psa10_probability": analysis.get("psa10_probability"),
            "psa9_or_better_probability": analysis.get("psa9_or_better_probability"),
            "recommendation": analysis.get("recommendation", "SKIP"),
            "photo_quality": analysis.get("photo_quality"),
            "notes": analysis.get("notes"),
            "error": analysis.get("error"),
        })

        rec = result["recommendation"]
        grade = result.get("predicted_grade", "?")
        psa9p = result.get("psa9_or_better_probability", "?")
        print(f"  → Best pick: ${best_listing['price']:.2f} | {rec} (predicted grade: {grade}, PSA 9+: {psa9p}%)")

        rows.append(result)

    analysis_df = pd.DataFrame(rows)
    OUTPUT_ANALYSIS.parent.mkdir(parents=True, exist_ok=True)
    analysis_df.to_csv(OUTPUT_ANALYSIS, index=False)
    print(f"\nSaved full analysis → {OUTPUT_ANALYSIS}")

    shortlist = analysis_df[analysis_df["recommendation"] == "SUBMIT"].copy()
    shortlist.to_csv(OUTPUT_SHORTLIST, index=False)

    print(f"\n{'='*60}")
    print(f"{len(shortlist)} SUBMIT cards out of {len(candidates)} analyzed")
    print(f"{'='*60}")
    if not shortlist.empty:
        cols = ["card_name", "set_name", "ebay_price", "predicted_grade",
                "psa9_or_better_probability", "psa10_probability", "notes"]
        print(shortlist[[c for c in cols if c in shortlist.columns]].to_string(index=False))

    print(f"\nFull analysis saved to: {OUTPUT_ANALYSIS}")
    print(f"Final shortlist saved to: {OUTPUT_SHORTLIST}")
    return shortlist


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze eBay card images with Claude Vision")
    parser.add_argument("--input", default=str(INPUT_FILE),
                        help="Flip opportunities CSV to pull candidates from")
    parser.add_argument("--top", type=int, default=20,
                        help="Number of top candidates to analyze (default: 20)")
    args = parser.parse_args()
    run(input_path=args.input, top_n=args.top)
