# Find Pokemon Card Flip Opportunities

## Goal
Identify Pokemon cards that can be profitably bought raw (ungraded), submitted to PSA for grading, and resold as PSA 9 or PSA 10 copies. Surface only cards where the math works: spread covers grading cost, gem rate is reliable, and expected ROI exceeds the threshold.

## Inputs
- `data/watchlist.csv` — cards to analyze: `card_name, set_name, card_number, tcgplayer_set_name`
- `.env` — all credentials and tunable parameters (see `.env.example`)

## Tools / Scripts
| Script | What it does |
|---|---|
| `execution/auth_tcgplayer.py` | Fetches and caches TCGPlayer OAuth bearer token |
| `execution/fetch_tcgplayer_prices.py` | Gets raw + PSA 9 + PSA 10 market prices from TCGPlayer API |
| `execution/scrape_pokedata_population.py` | Scrapes grade population data from pokedata.io |
| `execution/calculate_flip_ev.py` | Merges price + pop data, calculates EV, profit, ROI per card |
| `execution/run_analysis.py` | Full pipeline: loads watchlist → calls above scripts → outputs results |

## Outputs
- **Google Sheet** (primary): all cards analyzed, full EV breakdown, sorted by ROI descending
- **Email summary** (optional): top N cards above ROI threshold, with key numbers

## Expected Value Formula

```
gem_rate         = (psa9_count + psa10_count) / total_graded

# Revenue weighted by probability of each outcome
psa10_rate       = psa10_count / total_graded
psa9_rate        = psa9_count / total_graded
below_gem_rate   = 1 - gem_rate

expected_revenue = (psa10_rate * psa10_price)
                 + (psa9_rate  * psa9_price)
                 + (below_gem_rate * below_gem_price)  # raw price used as proxy

cost             = raw_price + grading_fee
selling_fee      = expected_revenue * SELLING_FEE_RATE  # default 0.13 (TCGPlayer/eBay)

profit           = expected_revenue - cost - selling_fee
roi              = profit / cost
```

## Decision Criteria — All must pass to surface a card
| Filter | Default | Env var | Rationale |
|---|---|---|---|
| `roi >= MIN_ROI` | 0.20 (20%) | `MIN_ROI` | Minimum acceptable return |
| `gem_rate >= MIN_GEM_RATE` | 0.30 | `MIN_GEM_RATE` | Need reasonable odds of hitting 9/10 |
| `total_graded >= MIN_POP` | 50 | `MIN_POP_COUNT` | Small samples make gem rate unreliable |
| `psa10_price > raw_price + grading_fee * 2` | — | — | Meaningful spread must exist |
| `raw_price <= MAX_RAW_PRICE` | 500.00 | `MAX_RAW_PRICE` | Cap exposure per card |

## Edge Cases
- **Card not found on TCGPlayer**: log warning, skip card, continue
- **No graded listing on TCGPlayer**: mark `psa9_price` / `psa10_price` as `null`, skip EV calc
- **pokedata.io rate limit / 429**: sleep 5s, retry up to 3× with exponential backoff; log failure after 3rd
- **Total graded < MIN_POP_COUNT**: still calculate but flag as `low_data = True` in output
- **TCGPlayer token expires**: `auth_tcgplayer.py` caches expiry timestamp and auto-refreshes before the call fails
- **No `.env` file**: script should fail loudly with a clear message listing missing vars

## Tuning Notes
- PSA regular grading fee (~$25–50) changes — update `GRADING_FEE` in `.env` accordingly
- `SELLING_FEE_RATE` of 0.13 covers ~10% platform fee + ~3% payment processing; adjust for venue
- A **high gem rate + low PSA 10 premium** = bad flip. Gem rate alone is not enough; spread matters.
- A **low gem rate + huge PSA 10 premium** can still be good if you are selective about the raw condition you buy (near-mint only). The population gem rate reflects *all* submissions, not just careful buyers.
- Run daily or on-demand; prices move fast on hype cycles
- Add new cards to watchlist as sets release; remove cards with stale/thin markets

## Self-Annealing Log
| Date | Issue | Fix Applied |
|---|---|---|
| — | — | — |
