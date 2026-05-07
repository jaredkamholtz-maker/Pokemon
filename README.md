# Pokemon Card Flip Analyzer

Automated system for identifying profitable raw → PSA grading → resell opportunities across Pokemon card sets.

## What It Does

1. **Discovers** all cards across configured sets via PokeData.io
2. **Filters** ~3,000 cards to ~400 PSA-worthy candidates using Claude Haiku (AI pre-filter)
3. **Fetches prices** (raw, PSA 9, PSA 10) from PriceCharting — parallel HTTP, 8 workers
4. **Scrapes population** data from 130point.com — parallel HTTP, 5 workers
5. **Calculates EV**: expected profit and ROI using actual gem rates, or breakeven gem rate when population data is unavailable
6. **Delivers results** to Google Sheets and email

## Quick Start

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, SMTP credentials, and optionally GOOGLE_SPREADSHEET_ID

pip install -r requirements.txt

# Full scan across all configured sets
python execution/run_analysis.py

# Limit to one era
python execution/run_analysis.py --era scarlet-violet

# Specific sets only
python execution/run_analysis.py --sets "151,Evolving Skies"
```

## Architecture

This project uses a 3-layer agent architecture (see `CLAUDE.md`):

- **Layer 1 — Directives** (`directives/`): SOPs that define goals, inputs, tools, and edge cases
- **Layer 2 — Orchestration** (`execution/run_analysis.py`): reads directives, sequences execution scripts, handles errors
- **Layer 3 — Execution** (`execution/*.py`): deterministic Python scripts for each step

## Configuration

All parameters live in `.env` (copy from `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Claude Haiku AI pre-filter |
| `GRADING_FEE` | 25.00 | PSA regular tier fee (USD) |
| `SELLING_FEE_RATE` | 0.13 | Platform fee (TCGPlayer/eBay) |
| `MIN_ROI` | 0.20 | Minimum 20% ROI to surface a card |
| `MIN_GEM_RATE` | 0.30 | Minimum PSA 9+10 rate |
| `MIN_POP_COUNT` | 50 | Minimum submissions for reliable gem rate |
| `MAX_RAW_PRICE` | 500.00 | Max raw card price |
| `MAX_BREAKEVEN_GEM_RATE` | 0.15 | Max breakeven gem rate for no-pop-data cards |
| `GOOGLE_SPREADSHEET_ID` | — | Optional Sheets output |
| `SMTP_*` / `EMAIL_TO` | — | Optional email delivery |

## Eras and Sets

`data/target_sets.csv` defines all sets. Three eras currently configured:

- **mega-evolution**: 13 XY-era sets (2014–2016)
- **sword-shield**: 10 sets (2020–2023)
- **scarlet-violet**: 7 sets (2023–2024)

To add sets: append to `target_sets.csv`. If PokeData.io uses a different set name, add an entry to `ALIASES` in `execution/discover_cards.py`.

## GitHub Actions

Scheduled runs every Monday and Friday at 8am ET. Manual trigger available with era or set filters.

Results are uploaded as workflow artifacts (`.tmp/` directory, retained 14 days).

## Detailed Documentation

See `directives/find_flip_opportunities.md` for the full pipeline spec, EV formulas, filter criteria, and self-annealing log.
