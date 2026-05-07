# Find Pokemon Card Flip Opportunities

## Goal
Identify Pokemon cards that can be profitably bought raw (ungraded), submitted to PSA for grading, and resold as PSA 9 or PSA 10 copies. Broad market scan across configurable eras/sets — not a static watchlist. Surface only cards where the math works: spread covers grading cost, gem rate is reliable, and expected ROI exceeds the threshold.

## Inputs
- `data/target_sets.csv` — sets to scan: `set_name, era, notes`
- `.env` — all credentials and tunable parameters (see `.env.example`)
- `ANTHROPIC_API_KEY` — required for AI pre-filter step (Claude Haiku)

## Pipeline (6 Steps)

```
target_sets.csv
      │
      ▼
[1] discover_cards.py      → .tmp/discovered_cards.csv    (~3,000 cards)
      │
      ▼
[2] filter_cards_ai.py     → .tmp/filtered_cards.csv      (~400–600 cards)
      │
      ▼
[3] fetch_tcgplayer_prices.py → .tmp/tcgplayer_prices.csv  (parallel, 3 workers, curl_cffi)
     → Filter applied: keep only cards with PSA 9 or PSA 10 > $60
      │
      ▼
[4] scrape_pokedata_population.py → .tmp/pokedata_population.csv (parallel, 5 workers)
      │
      ▼
[5] calculate_flip_ev.py   → .tmp/flip_opportunities.csv
     → Filter applied: gem_rate >= 50% AND ROI >= 10% after $25 grading fee
      │
      ▼
[6] analyze_card_images.py → .tmp/final_shortlist.csv      (top 20 cards only)
     → Finds cheapest eBay raw listing per card (curl_cffi scraping)
     → Downloads up to 5 listing photos
     → Sends to Claude Vision for PSA grade assessment
     → Keeps only SUBMIT cards (predicted PSA 9/10 candidate)
      │
      ▼
[OUT] Google Sheet + HTML Email (card, raw, PSA 9, PSA 10, profit %, gem rate,
                                  predicted grade, PSA 9+ probability, eBay link)
```

## Tools / Scripts

| Script | What it does |
|---|---|
| `execution/discover_cards.py` | Queries PokeData.io API for all cards in each target set; writes `discovered_cards.csv` |
| `execution/filter_cards_ai.py` | Sends cards to Claude Haiku in batches of 300; drops commons/trainers/bulk; returns PSA-worthy candidates (~80% reduction) |
| `execution/fetch_tcgplayer_prices.py` | Gets raw + PSA 9 + PSA 10 market prices from PriceCharting; parallel (3 workers); uses `curl_cffi` with `impersonate="chrome124"` to bypass Cloudflare bot detection |
| `execution/scrape_pokedata_population.py` | Scrapes grade population data from 130point.com; parallel (5 workers) |
| `execution/calculate_flip_ev.py` | Merges price + pop data; two-track EV analysis (see below); outputs full analysis sorted by ROI |
| `execution/analyze_card_images.py` | Takes top N flip candidates; finds cheapest eBay raw listing per card; downloads photos; sends to Claude Vision for PSA grade prediction; outputs SUBMIT/SKIP recommendation |
| `execution/run_analysis.py` | Orchestrates all 6 steps end-to-end; supports era/set filtering and skip flags |

## Running It

```bash
# Full market scan (all sets in target_sets.csv)
python execution/run_analysis.py

# Filter by era
python execution/run_analysis.py --era mega-evolution
python execution/run_analysis.py --era sword-shield
python execution/run_analysis.py --era scarlet-violet

# Filter to specific sets
python execution/run_analysis.py --sets "151,Evolving Skies,Obsidian Flames"

# Skip expensive steps on re-runs (reuse cached intermediate files)
python execution/run_analysis.py --skip-discovery   # reuse .tmp/discovered_cards.csv
python execution/run_analysis.py --skip-ai-filter   # reuse .tmp/filtered_cards.csv
python execution/run_analysis.py --skip-prices      # reuse .tmp/tcgplayer_prices.csv
python execution/run_analysis.py --skip-images      # skip eBay image analysis (no Claude Vision credits)

# Analyze more or fewer cards with Claude Vision (default: top 20)
python execution/run_analysis.py --image-top-n 10

# Skip outputs
python execution/run_analysis.py --skip-sheets --skip-email
```

## GitHub Actions
Trigger manually via **Actions → Pokemon Flip Analysis → Run workflow**:
- **era**: dropdown (blank = full scan, or mega-evolution / sword-shield / scarlet-violet)
- **sets**: comma-separated set names (overrides era)

Scheduled runs: Monday and Friday at 8am ET.

## Two-Track EV Analysis

### Track 1 — Full EV (population data available)
```
gem_rate         = (psa9_count + psa10_count) / total_graded

psa10_rate       = psa10_count / total_graded
psa9_rate        = psa9_count / total_graded
below_gem_rate   = 1 - gem_rate

expected_revenue = (psa10_rate * psa10_price)
                 + (psa9_rate  * psa9_price)
                 + (below_gem_rate * raw_price)   # raw price used as proxy for below-gem

cost             = raw_price + grading_fee
selling_fee      = expected_revenue * SELLING_FEE_RATE  # default 0.13 (TCGPlayer/eBay)

profit           = expected_revenue - cost - selling_fee
roi              = profit / cost
```

### Track 2 — Breakeven gem rate (no population data)
When PSA submission counts are unavailable, compute the minimum gem rate needed to break even. Cards requiring < `MAX_BREAKEVEN_GEM_RATE` (default 15%) are surfaced as low-bar opportunities.
```
breakeven_gem_rate = (cost / (1 - selling_fee_rate) - raw_price) / (psa_weighted - raw_price)
# where psa_weighted = 0.6 * psa10_price + 0.4 * psa9_price (assumed 60/40 split)
```

## Decision Criteria — All must pass to surface a card

### Track 1 (population data available)
| Filter | Default | Env var | Rationale |
|---|---|---|---|
| `roi >= MIN_ROI` | 0.20 (20%) | `MIN_ROI` | Minimum acceptable return |
| `gem_rate >= MIN_GEM_RATE` | 0.30 | `MIN_GEM_RATE` | Need reasonable odds of hitting 9/10 |
| `total_graded >= MIN_POP_COUNT` | 50 | `MIN_POP_COUNT` | Small samples make gem rate unreliable |
| `psa10_price > raw_price + grading_fee * 2` | — | — | Meaningful spread must exist |
| `raw_price <= MAX_RAW_PRICE` | 500.00 | `MAX_RAW_PRICE` | Cap exposure per card |

### Track 2 (no population data)
| Filter | Default | Env var |
|---|---|---|
| `breakeven_gem_rate <= MAX_BREAKEVEN_GEM_RATE` | 0.15 | `MAX_BREAKEVEN_GEM_RATE` |
| `psa10_price > raw_price + grading_fee * 2` | — | — |
| `raw_price <= MAX_RAW_PRICE` | 500.00 | `MAX_RAW_PRICE` |

## Eras and Sets
`data/target_sets.csv` defines all sets with an `era` column for scoped runs:
- **mega-evolution**: 13 XY-era sets (2014–2016) — Mega EX cards, BREAKs
- **sword-shield**: 10 sets (2020–2023) — VMAX, VSTAR, GX reprints
- **scarlet-violet**: 7 sets (2023–2024) — ex, special illustration rares (SIR)

To add a set: append a row to `target_sets.csv`. If PokeData.io uses a different name, add an alias to the `ALIASES` dict in `discover_cards.py`.

## AI Pre-Filter (Claude Haiku)
Cuts scraping from ~3,000 cards to ~400–600 by dropping:
- Commons and uncommons
- Basic Energy cards
- Trainers/Supporters/Stadiums (except full art / secret rare)
- Standard rares without meaningful grading premium

Keeps: holo rares, full arts, alt arts, VMAX/VSTAR/ex/GX, secret rares, high-demand Pokemon (Charizard, Pikachu, Mewtwo, Umbreon, Eevee, Rayquaza, Lugia, etc.)

- Batches of 300 cards per API call
- Prompt caching on system prompt (saves tokens across batches)
- On any API error: keeps entire batch (never silently drops cards)
- Falls back to full card list if `ANTHROPIC_API_KEY` is missing

## Edge Cases
- **Set not found on PokeData.io**: logs `[SKIP]` with closest-match hints; add alias to `ALIASES` dict in `discover_cards.py`
- **Card not found on PriceCharting**: logs warning, `raw_price` = null, skipped in EV calc
- **No graded listings**: `psa9_price`/`psa10_price` null → falls through to Track 2 (breakeven)
- **130point.com rate limit / 403**: logs error per card, population data absent → Track 2 for that card
- **Total graded < MIN_POP_COUNT**: still calculates but flags `low_data = True` in output
- **No `.env` file**: scripts fail loudly listing missing vars

## Tuning Notes
- PSA regular grading fee (~$25–50) changes — update `GRADING_FEE` in `.env`
- `SELLING_FEE_RATE` of 0.13 covers ~10% platform fee + ~3% payment processing
- A **high gem rate + low PSA 10 premium** = bad flip. Gem rate alone is not enough; spread matters.
- A **low gem rate + huge PSA 10 premium** can still be good if you cherry-pick near-mint raw copies. Population gem rate reflects *all* submissions, not just selective buyers.
- Run on Monday/Friday; prices move fast on hype cycles

## Self-Annealing Log
| Date | Issue | Fix Applied |
|---|---|---|
| 2026-05 | Static watchlist couldn't scale to broad market scan | Replaced with `discover_cards.py` querying PokeData.io API + `data/target_sets.csv` |
| 2026-05 | ~3,000 discovered cards made scraping too slow (~2–3 hrs) | Added Claude Haiku AI pre-filter to cut to ~400–600 cards before expensive scraping |
| 2026-05 | Sequential HTTP requests were bottleneck | Added `ThreadPoolExecutor` (8 workers for prices, 5 for population) |
| 2026-05 | `pd.concat` of EV dict with merged df created duplicate `gem_rate`/`total_graded` columns | Removed those keys from EV return dicts; they come from the merged population data directly |
| 2026-05 | `.tmp/` artifact not uploaded by GitHub Actions | Added `include-hidden-files: true` to upload-artifact step |
| 2026-05 | PokeData.io calls set "151" → "Pokemon Card 151" | Added `"Pokemon Card 151"` to `ALIASES["151"]`; added closest-match debug hint to `[SKIP]` log |
| 2026-05 | `run_analysis.py` on main lacked AI filter after partial push overwrote it | Always verify key files on main after any push; never push partial file sets |
| 2026-05 | 290/299 PriceCharting cards returned 403 (Cloudflare bot detection) | Switched `fetch_tcgplayer_prices.py` to `curl_cffi` with `impersonate="chrome124"`; reduced workers 8→3; added 3s backoff+retry on 403 |
| 2026-05 | Redesigned pipeline to use PokeData.io for prices — returned 0 cards | PokeData.io `/api/cards` endpoint returns only card metadata (no prices). Reverted to PriceCharting for prices; PokeData.io used only for card discovery. |
| 2026-05 | AI pre-filter step missing after pipeline redesign | Restored `filter_cards_ai.py` in `run_analysis.py`; it is step 2 and required for performance (cuts scraping 3,000 → 400-600 cards) |
| 2026-05 | Added eBay image analysis as step 6 | `analyze_card_images.py` scrapes eBay for cheapest raw listing per top-N card, downloads photos, sends to Claude Vision for PSA grade prediction; `--skip-images` bypasses step for faster runs |
