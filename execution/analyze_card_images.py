"""
Final filter: find cheapest eBay raw listings for each top flip candidate,
analyze listing photos with Claude Vision, and pick the best quality copy.

Design principles:
  - Only real seller photos are accepted. Stock photos, fakes, and altered
    cards are auto-disqualified and NEVER used as the listing URL.
  - If ALL listings for a card are disqualified, no specific URL is set and
    the email shows a generic eBay search link instead.
  - Images come exclusively from the eBay Browse API response.
  - Image analysis is additive: Claude's grade prediction and PSA 9+
    probability are bonus info on top of the EV math.

Steps per card:
  1. Search eBay Browse API for cheapest ungraded listings (up to MAX_LISTINGS)
  2. For each listing: download the API-provided image, send to Claude Vision
  3. Skip listings with stock photos, fakes, or critical red flags
  4. Pick the listing with highest PSA 9+ probability among valid photos
  5. Only set ebay_listing_url if at least one listing passed analysis

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

MAX_LISTINGS = 5        # analyze the top 5 most expensive ungraded listings
BROWSE_LIMIT = 50       # how many results to fetch from Browse API before filtering
MAX_PRICE    = 500.0    # ignore listings above this price (outliers / special editions)
RATE_DELAY = 0.5
MODEL = "claude-sonnet-4-6"

# eBay listing used as a visual reference for PSA 10 card quality and photo quality.
# Set REFERENCE_LISTING_URL in .env to override. Image is cached in .tmp/reference_card.jpg.
REFERENCE_ITEM_ID = "397923543088"
REFERENCE_CACHE   = Path(".tmp/reference_card.jpg")

GRADING_PROMPT = """You are a strict PSA card grader analyzing seller photos from an eBay listing. Apply the OFFICIAL PSA grading standards exactly as written below. Be conservative — most cards do not reach PSA 9 or 10. Do not give the benefit of the doubt when a defect is visible.

CARD CONTEXT:
- Card: {card_name}
- Set: {set_name}
- Number: {card_number}

OFFICIAL PSA GRADING STANDARDS (apply these exactly):

PSA 10 GEM MINT — Virtually perfect. ALL of the following must be true:
  • Four perfectly sharp corners, no wear whatsoever
  • Sharp focus, full original gloss, no surface scratches
  • Free of staining of any kind
  • Centering: ≤55/45 left-right AND top-bottom on front; ≤75/25 on back
  • A minor printing imperfection is allowed ONLY if it does not impair overall appeal

PSA 9 MINT — Outstanding condition. At most ONE of these minor defects:
  • Slight wear on one or two corners (barely visible)
  • Slightly off-white borders
  • Minor printing imperfection
  • Slight loss of original gloss
  • Centering: ≤60/40 front; ≤75/25 back
  • No creases, no staining, no surface scratches

PSA 8 NEAR MINT–MINT — Slightly worn. May have two of the PSA 9 defects, OR:
  • Slightly fuzzy corners on up to two corners
  • Centering: ≤65/35 front

PSA 7 NEAR MINT — Three or four corners show slight wear. Centering ≤70/30.

PSA 6 and below — Visible wear, scuffs, creases, or heavy whitening.

GRADING RULES:
- Any visible crease, bend, or water damage = PSA 5 or below (use grade_high = 5)
- Any two defects that individually would allow PSA 9 = PSA 8, not PSA 9
- Centering visibly off to the naked eye likely means ≤PSA 8
- Holo scratches visible at any angle = at most PSA 8 for holo cards
- Be skeptical of stock-looking photos — many eBay sellers use database images

ASSESS THE FOLLOWING. For each dimension, provide a rating, a confidence score 0.0–1.0 based on photo quality for THAT dimension, and specific observations. If photo quality prevents assessment, set rating to "unknown" and confidence to 0.0. Do not guess.

DIMENSIONS:
1. centering_front: measure border widths and estimate ratio like "55/45 L/R, 50/50 T/B"
2. centering_back: same format (or "unknown" if back not shown)
3. corners: sharp | minor_wear | moderate_wear | heavy_wear | unknown
   (sharp = PSA 10 eligible; minor_wear = PSA 9 at best; moderate+ = PSA 8 or below)
4. edges: clean | minor_whitening | moderate_whitening | heavy_whitening | unknown
5. surface_front: clean | print_lines | minor_scratches | scratches | scuffs | unknown
6. surface_back: same scale
7. holo_condition: pristine | minor_scratches | scratched | heavy_scratching | n/a | unknown
8. whitening_overall: none | minor | moderate | heavy | unknown

PREDICTED GRADE RANGE:
Based strictly on the PSA standards above, give the range this card would likely receive at PSA. Low = pessimistic outcome, High = optimistic outcome. Use 0 for both if ungradeable (creases, bends, water damage).

RED FLAGS — check each and report true/false with reasoning:
- stock_photo_suspected: photos look like generic/database images, not this specific card being sold by this seller
- back_not_shown: no clear photo of card back
- glare_obscures_detail: critical areas hidden by reflection
- altered_appearance: signs of trimming, recoloring, or surface treatment
- fake_indicators: wrong font, color, texture, holo pattern, or back design for this card/set
- damage_hidden: photos angled to obscure likely damage
- inconsistent_card: features visible don't match the identified card

Return ONLY valid JSON, no other text:
{
  "centering_front": {"rating": "...", "confidence": 0.0, "notes": "..."},
  "centering_back": {"rating": "...", "confidence": 0.0, "notes": "..."},
  "corners": {"rating": "...", "confidence": 0.0, "notes": "..."},
  "edges": {"rating": "...", "confidence": 0.0, "notes": "..."},
  "surface_front": {"rating": "...", "confidence": 0.0, "notes": "..."},
  "surface_back": {"rating": "...", "confidence": 0.0, "notes": "..."},
  "holo_condition": {"rating": "...", "confidence": 0.0, "notes": "..."},
  "whitening_overall": {"rating": "...", "confidence": 0.0, "notes": "..."},
  "predicted_grade_range": {"low": 0, "high": 0, "confidence": 0.0},
  "red_flags": {
    "stock_photo_suspected": {"flag": false, "reason": ""},
    "back_not_shown": {"flag": false, "reason": ""},
    "glare_obscures_detail": {"flag": false, "reason": ""},
    "altered_appearance": {"flag": false, "reason": ""},
    "fake_indicators": {"flag": false, "reason": ""},
    "damage_hidden": {"flag": false, "reason": ""},
    "inconsistent_card": {"flag": false, "reason": ""}
  },
  "overall_photo_quality": {"rating": "poor|fair|good|excellent", "notes": "..."},
  "summary": "2-3 sentence overall assessment applying PSA standards strictly"
}"""

REFERENCE_INTRO = (
    "The first image below is a REFERENCE EXAMPLE provided by the buyer. "
    "It represents a card they consider a strong PSA 9/10 candidate — "
    "use it to calibrate your expectations for corner sharpness, surface cleanliness, "
    "centering, and photo quality. Do NOT grade the reference card itself.\n\n"
    "The second image is the actual listing you must grade."
)

# Red flags that auto-disqualify a listing
_CRITICAL_FLAGS = {"stock_photo_suspected", "fake_indicators", "altered_appearance"}


def _fetch_reference_image(token: str) -> str | None:
    """
    Fetch the reference listing image via Browse API getItem, cache it locally.
    Returns base64-encoded JPEG, or None if unavailable.
    """
    if REFERENCE_CACHE.exists():
        try:
            return base64.standard_b64encode(REFERENCE_CACHE.read_bytes()).decode()
        except Exception:
            pass

    item_id = os.environ.get("REFERENCE_LISTING_ITEM_ID", REFERENCE_ITEM_ID)
    try:
        resp = _SESSION.get(
            f"https://api.ebay.com/buy/browse/v1/item/v1|{item_id}|0",
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            timeout=20,
        )
        if not resp or resp.status_code != 200:
            return None
        data      = resp.json()
        image_url = data.get("image", {}).get("imageUrl")
        if not image_url:
            return None
        img_resp = _SESSION.get(_upgrade_ebay_image_url(image_url), timeout=20)
        if img_resp and img_resp.status_code == 200 and img_resp.content:
            REFERENCE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            REFERENCE_CACHE.write_bytes(img_resp.content)
            return base64.standard_b64encode(img_resp.content).decode()
    except Exception as e:
        print(f"  (reference image fetch failed: {e})")
    return None


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
    return any(kw in t for kw in [
        "psa ", "psa-", "psa9", "psa10", "psa8", "psa7", "psa6",
        "bgs ", "cgc ", "sgc ", "graded", "gem mint",
    ])


def _is_multi_card_listing(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in [
        "pick your", "you pick", "your pick", "pick a card",
        "choose your", "choose a card", "your choice",
        "lot of", " lot ", "bundle", "set of", "complete set", "full set", "sealed set",
        "x2 ", "x3 ", "x4 ", "x5 ", " x2", " x3", " x4", " x5",
        "2x ", "3x ", "4x ", "5x ",
        "wholesale",
    ])


# ── eBay search ──────────────────────────────────────────────────────────────────────────────

def _get_browse_token(client_id: str, client_secret: str) -> str | None:
    try:
        creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = _SESSION.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
            timeout=20,
        )
        if resp and resp.status_code == 200:
            return resp.json().get("access_token")
    except Exception as e:
        print(f"  OAuth token error: {e}")
    return None


def search_ebay_listings(card_name: str, set_name: str) -> list[dict]:
    load_dotenv()
    app_id  = os.environ.get("EBAY_APP_ID")
    cert_id = os.environ.get("EBAY_CERT_ID")

    if not app_id or not cert_id:
        print("  EBAY_APP_ID/CERT_ID not set — skipping eBay search")
        return []

    token = _get_browse_token(app_id, cert_id)
    if not token:
        print("  OAuth token failed")
        return []

    def _parse_items(items: list, strict: bool) -> list:
        """
        Parse Browse API itemSummaries into candidates.
        strict=True  → also filter multi-card/lot listings
        strict=False → only filter graded listings (last-resort fallback)
        """
        out = []
        graded_n = multi_n = no_url_n = 0
        for item in items:
            title = item.get("title", "")
            url   = item.get("itemWebUrl", "")
            if not url:
                no_url_n += 1
                continue
            if _is_graded_title(title):
                graded_n += 1
                continue
            if strict and _is_multi_card_listing(title):
                multi_n += 1
                continue
            # Accept any price — auctions may show 0 until first bid
            try:
                price = float(item.get("price", {}).get("value") or 0)
            except (ValueError, TypeError):
                price = 0.0
            image_url = item.get("image", {}).get("imageUrl")
            # Prefer listings that have additional photos (real seller photos)
            extra_images = len(item.get("additionalImages", []))
            out.append({"title": title, "price": price, "url": url,
                        "image_url": image_url, "extra_images": extra_images})
            if len(out) >= MAX_LISTINGS * 4:   # gather extras for sorting
                break
        if strict:
            print(f"(graded={graded_n} lot={multi_n} no_url={no_url_n} raw={len(out)})", end=" ", flush=True)
        return out

    try:
        resp = _SESSION.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
            params={"q": f"{card_name} {set_name} pokemon",
                    "sort": "-price",
                    "limit": str(BROWSE_LIMIT)},
            timeout=20,
        )
        if not resp or resp.status_code != 200:
            print(f"(Browse API HTTP {resp and resp.status_code})", end=" ", flush=True)
            return []

        items = resp.json().get("itemSummaries", [])
        print(f"(API={len(items)})", end=" ", flush=True)

        candidates = _parse_items(items, strict=True)

        # Fallback: relax lot filter if strict pass found nothing
        if not candidates:
            print("(fallback: relaxing lot filter)", end=" ", flush=True)
            candidates = _parse_items(items, strict=False)

        if not candidates:
            print("(0 candidates after all filters)", end=" ", flush=True)
            return []

        # Cap at MAX_PRICE, then sort by price descending
        candidates = [c for c in candidates if c["price"] <= MAX_PRICE]
        if not candidates:
            print(f"(0 candidates under ${MAX_PRICE:.0f})", end=" ", flush=True)
            return []
        candidates.sort(key=lambda c: -c["price"])
        return candidates[:MAX_LISTINGS]
    except Exception as e:
        print(f"  Browse API error: {e}")
        return []


# ── Claude Vision analysis ─────────────────────────────────────────────────────────────────────────────

def _upgrade_ebay_image_url(url: str) -> str:
    """
    eBay CDN URLs end in s-l<size>.jpg (e.g. s-l500.jpg, s-l140.jpg).
    Swap to s-l1600 so Claude Vision gets a high-res photo instead of
    the small API thumbnail, which can look like a stock image.
    """
    if not url:
        return url
    import re as _re
    upgraded = _re.sub(r's-l\d+\.jpg', 's-l1600.jpg', url)
    return upgraded if upgraded != url else url


def download_image_b64(url: str) -> str | None:
    try:
        resp = _get(url)
        if resp and resp.status_code == 200 and resp.content:
            return base64.standard_b64encode(resp.content).decode("utf-8")
    except Exception:
        pass
    return None


def _derive_from_analysis(parsed: dict) -> dict:
    """
    Convert the detailed nested analysis JSON into flat pipeline fields.
    Returns recommendation, predicted_grade, psa9_or_better_probability,
    photo_quality, notes, red_flags_active.
    """
    red_flags = parsed.get("red_flags", {})

    # Critical flags → auto-disqualify
    active_critical = [f for f in _CRITICAL_FLAGS
                       if isinstance(red_flags.get(f), dict) and red_flags[f].get("flag")]
    if active_critical:
        return {
            "recommendation": "SKIP",
            "predicted_grade": None,
            "psa9_or_better_probability": 0,
            "photo_quality": "disqualified",
            "notes": f"Disqualified: {', '.join(active_critical)}",
            "red_flags_active": ", ".join(active_critical),
        }

    photo_q_info  = parsed.get("overall_photo_quality", {})
    photo_quality = photo_q_info.get("rating", "fair") if isinstance(photo_q_info, dict) else "fair"
    if photo_quality == "poor":
        return {
            "recommendation": "SKIP",
            "predicted_grade": None,
            "psa9_or_better_probability": 0,
            "photo_quality": "poor",
            "notes": photo_q_info.get("notes", "Photo quality too poor to assess") if isinstance(photo_q_info, dict) else "poor quality",
            "red_flags_active": None,
        }

    grade_info = parsed.get("predicted_grade_range", {})
    grade_low  = grade_info.get("low",  0)   if isinstance(grade_info, dict) else 0
    grade_high = grade_info.get("high", 0)   if isinstance(grade_info, dict) else 0
    grade_conf = grade_info.get("confidence", 0.5) if isinstance(grade_info, dict) else 0.5

    if grade_high == 0:
        return {
            "recommendation": "SKIP",
            "predicted_grade": None,
            "psa9_or_better_probability": 0,
            "photo_quality": photo_quality,
            "notes": parsed.get("summary", "Ungradeable condition"),
            "red_flags_active": None,
        }

    predicted_grade = round((grade_low + grade_high) / 2)

    if grade_high >= 10:
        prob = int(min(95, 70 + 25 * grade_conf))
    elif grade_high >= 9 and grade_low >= 9:
        prob = int(min(85, 60 + 25 * grade_conf))
    elif grade_high >= 9:
        range_size = max(grade_high - grade_low, 1)
        overlap    = (grade_high - 9 + 1) / (range_size + 1)
        prob       = int(overlap * 70 * grade_conf)
    else:
        prob = 0

    # Soft flag: back not shown → halve probability
    if isinstance(red_flags.get("back_not_shown"), dict) and red_flags["back_not_shown"].get("flag"):
        prob = prob // 2

    soft_flags = [f for f, info in red_flags.items()
                  if f not in _CRITICAL_FLAGS
                  and isinstance(info, dict) and info.get("flag")]

    summary = parsed.get("summary", "")
    notes   = (f"⚠️ {', '.join(soft_flags)}. " if soft_flags else "") + summary

    return {
        "recommendation":           "SUBMIT" if grade_high >= 9 and prob >= 30 else "SKIP",
        "predicted_grade":          predicted_grade,
        "psa9_or_better_probability": prob,
        "photo_quality":            photo_quality,
        "notes":                    notes,
        "red_flags_active":         ", ".join(soft_flags) if soft_flags else None,
    }


def _get_dim_rating(parsed: dict, key: str) -> str | None:
    val = parsed.get(key)
    return val.get("rating") if isinstance(val, dict) else None


def analyze_image(card_name: str, set_name: str, card_number: str, image_b64: str,
                  reference_b64: str | None = None) -> dict:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not set", "recommendation": "SKIP"}

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "anthropic not installed", "recommendation": "SKIP"}

    raw = ""
    try:
        prompt_text = (GRADING_PROMPT
                       .replace("{card_name}", card_name)
                       .replace("{set_name}", set_name)
                       .replace("{card_number}", card_number or "unknown"))
        if reference_b64:
            content = [
                {"type": "text", "text": REFERENCE_INTRO},
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": reference_b64}},
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": prompt_text},
            ]
        else:
            content = [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": prompt_text},
            ]
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": content}],
        )
        raw       = response.content[0].text.strip()
        raw_clean = re.sub(r"```(?:json)?\s*", "", raw).strip()

        start = raw_clean.find("{")
        end   = raw_clean.rfind("}") + 1
        if start == -1 or end == 0:
            return {"error": f"No JSON in response: {raw[:100]}", "recommendation": "SKIP"}

        parsed = json.loads(raw_clean[start:end])
        if not isinstance(parsed, dict):
            return {"error": "Response was not a JSON object", "recommendation": "SKIP"}

        parsed  = {k.strip().strip('"').strip("'"): v for k, v in parsed.items()}
        derived = _derive_from_analysis(parsed)
        return {
            **derived,
            "centering_front": _get_dim_rating(parsed, "centering_front"),
            "corners":         _get_dim_rating(parsed, "corners"),
            "edges":           _get_dim_rating(parsed, "edges"),
            "surface_front":   _get_dim_rating(parsed, "surface_front"),
            "holo_condition":  _get_dim_rating(parsed, "holo_condition"),
            "grade_low":  parsed.get("predicted_grade_range", {}).get("low")  if isinstance(parsed.get("predicted_grade_range"), dict) else None,
            "grade_high": parsed.get("predicted_grade_range", {}).get("high") if isinstance(parsed.get("predicted_grade_range"), dict) else None,
        }

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e} | raw: {raw[:200]}", "recommendation": "SKIP"}
    except Exception as e:
        return {"error": str(e), "recommendation": "SKIP"}


def pick_best_listing(card_name: str, set_name: str, card_number: str,
                      listings: list[dict], reference_b64: str | None = None) -> tuple[dict, dict]:
    """
    Analyze each listing photo with Claude Vision.
    Returns (best_listing, analysis).

    Always returns listings[0] as a minimum fallback (cheapest single-card
    listing — already filtered for graded/lot/pick listings at search time).
    If a non-disqualified listing is found, that takes priority.
    If a SUBMIT listing is found, that wins.
    """
    # Cheapest listing is always the fallback — it's already filtered for
    # graded/lot/multi-card titles, so it's a real single-card listing
    best_listing  = listings[0]
    best_analysis: dict = {"recommendation": "SKIP", "notes": "photo unverified"}
    best_score = -1
    any_non_disqualified = False

    for i, listing in enumerate(listings, 1):
        image_url = listing.get("image_url")
        if not image_url:
            print(f"  [{i}/{len(listings)}] ${listing['price']:.2f} — no image from API")
            continue

        print(f"  [{i}/{len(listings)}] ${listing['price']:.2f} — downloading image...", end=" ", flush=True)
        time.sleep(RATE_DELAY)
        b64 = download_image_b64(_upgrade_ebay_image_url(image_url))
        if not b64:
            print("download failed")
            continue

        print("analyzing...", end=" ", flush=True)
        analysis = analyze_image(card_name, set_name, card_number, b64, reference_b64)
        rec      = analysis.get("recommendation", "SKIP")
        prob     = analysis.get("psa9_or_better_probability") or 0
        flags    = analysis.get("red_flags_active") or ""
        flag_str = f" [{flags}]" if flags else ""
        print(f"{rec} (PSA 9+: {prob}%){flag_str}")

        # Disqualified (stock photo / fake) listings never win, but cheapest
        # non-disqualified listing upgrades the fallback
        if analysis.get("photo_quality") == "disqualified":
            continue

        any_non_disqualified = True
        submit_bonus = 1000 if rec == "SUBMIT" else 0
        score = submit_bonus + prob
        if score > best_score:
            best_score    = score
            best_listing  = listing
            best_analysis = analysis

    if not any_non_disqualified:
        print("  All listings had stock/unverifiable photos — using cheapest listing URL as fallback")

    return best_listing, best_analysis


# ── Main run ───────────────────────────────────────────────────────────────────────────────────

def run(input_path: str = str(INPUT_FILE), top_n: int = 20) -> pd.DataFrame:
    load_dotenv()

    df = pd.read_csv(input_path)
    if df.empty:
        print("No flip opportunities to analyze.")
        return pd.DataFrame()

    sort_col   = "roi" if "roi" in df.columns else df.columns[0]
    candidates = df.sort_values(sort_col, ascending=False).head(top_n).copy()
    print(f"Analyzing top {len(candidates)} cards...\n")

    # Load reference image once — used as a visual quality anchor in every Claude Vision call
    app_id  = os.environ.get("EBAY_APP_ID")
    cert_id = os.environ.get("EBAY_CERT_ID")
    reference_b64: str | None = None
    if app_id and cert_id:
        ref_token = _get_browse_token(app_id, cert_id)
        if ref_token:
            reference_b64 = _fetch_reference_image(ref_token)
            status = "cached" if REFERENCE_CACHE.exists() else "fetched"
            print(f"Reference image {'loaded (' + status + ')' if reference_b64 else 'unavailable — grading without reference'}\n")

    rows = []
    for rank, (_, card) in enumerate(candidates.iterrows(), 1):
        card_name   = str(card.get("card_name",   "")).strip()
        set_name    = str(card.get("set_name",    "")).strip()
        card_number = str(card.get("card_number", "")).strip()
        print(f"[{rank}/{len(candidates)}] {card_name} | {set_name} #{card_number}")

        result = {
            "card_name": card_name, "set_name": set_name, "card_number": card_number,
            "raw_price":   card.get("raw_price"),   "psa9_price":  card.get("psa9_price"),
            "psa10_price": card.get("psa10_price"), "gem_rate":    card.get("gem_rate"),
            "total_graded": card.get("total_graded"), "psa9_count": card.get("psa9_count"),
            "psa10_count":  card.get("psa10_count"),  "roi":        card.get("roi"),
            "track": card.get("track"), "breakeven_gem_rate": card.get("breakeven_gem_rate"),
            # URL fields start as None — only set if a real photo listing passes analysis
            "ebay_listing_url": None, "ebay_price": None,
            "centering_front": None, "corners": None, "edges": None,
            "surface_front": None, "holo_condition": None,
            "grade_low": None, "grade_high": None,
            "predicted_grade": None, "psa9_or_better_probability": None,
            "recommendation": "NO_DATA", "photo_quality": None,
            "red_flags_active": None, "notes": None, "error": None,
        }

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

        try:
            best_listing, analysis = pick_best_listing(card_name, set_name, card_number, listings, reference_b64)

            # Always set URL — pick_best_listing guarantees a listing (cheapest fallback)
            result["ebay_listing_url"] = best_listing["url"]
            result["ebay_price"]       = best_listing["price"]

            if analysis and isinstance(analysis, dict):
                for field in ("recommendation", "predicted_grade", "psa9_or_better_probability",
                              "photo_quality", "notes", "error", "red_flags_active",
                              "centering_front", "corners", "edges", "surface_front",
                              "holo_condition", "grade_low", "grade_high"):
                    if field in analysis:
                        result[field] = analysis[field]
        except Exception as e:
            print(f"  [analysis error] {e}")
            result["error"] = str(e)

        rec   = result.get("recommendation", "NO_DATA")
        grade = result.get("predicted_grade", "?")
        prob  = result.get("psa9_or_better_probability", "?")
        url   = result.get("ebay_listing_url") or "[no valid listing — search link will be used]"
        print(f"  → {url}")
        print(f"  → ${result.get('ebay_price') or 0:.2f} | {rec} | predicted PSA {grade} | PSA 9+: {prob}%\n")

        rows.append(result)

    analysis_df = pd.DataFrame(rows)
    OUTPUT_ANALYSIS.parent.mkdir(parents=True, exist_ok=True)
    analysis_df.to_csv(OUTPUT_ANALYSIS, index=False)
    print(f"Full analysis saved → {OUTPUT_ANALYSIS}")

    shortlist = analysis_df[analysis_df["recommendation"] == "SUBMIT"].copy()
    shortlist.to_csv(OUTPUT_SHORTLIST, index=False)

    total  = len(candidates)
    found  = (analysis_df["ebay_listing_url"].notna()).sum()
    submit = len(shortlist)
    print(f"\n{'='*60}")
    print(f"Real photo listings found: {found}/{total}")
    print(f"SUBMIT (PSA 9/10 candidate): {submit}/{found}")
    print(f"{'='*60}\n")

    return shortlist


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(INPUT_FILE))
    parser.add_argument("--top",   type=int, default=20)
    args = parser.parse_args()
    run(input_path=args.input, top_n=args.top)
