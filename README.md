# Pokemon Card PSA Flip Analyzer

Finds raw Pokemon cards worth grading for PSA profit. Scrapes pokemonpricetracker.com for EV data, analyzes eBay listing photos with Claude Vision + Ximilar grading AI, and emails a shortlist of actionable opportunities.

## How it works

1. **Scrape** pokemonpricetracker.com for cards with high expected profit after PSA grading
2. **Analyze** eBay raw listings — Claude Vision + Ximilar grade each card's photos
3. **Email** cards where both AI systems confirm PSA 9+ condition

## Setup

### 1. Clone and install

```bash
git clone https://github.com/jaredkamholtz-maker/Pokemon.git
cd Pokemon
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

Copy the template and fill in your keys:

```bash
cp .env.example .env
nano .env
```

Required keys:

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Vision API — [console.anthropic.com](https://console.anthropic.com) |
| `XIMILAR_API_KEY` | Card grading API — [app.ximilar.com](https://app.ximilar.com) |
| `EBAY_APP_ID` | eBay Browse API app ID — [developer.ebay.com](https://developer.ebay.com) |
| `EBAY_CERT_ID` | eBay Browse API cert ID (needed for OAuth) |
| `SMTP_HOST` | Outbound email server (e.g. `smtp.gmail.com`) |
| `SMTP_PORT` | Usually `587` |
| `SMTP_USER` | Email username |
| `SMTP_PASS` | Email password or app password |
| `EMAIL_TO` | Recipient address for the results email |
| `GRADING_FEE` | Your PSA grading cost per card in USD (default: `25`) |

Optional:

| Variable | Description |
|---|---|
| `GOOGLE_SPREADSHEET_ID` | Push results to a Google Sheet (requires `credentials.json`) |

### 3. Run

```bash
python3 run.py
```

That's it. Results are emailed to `EMAIL_TO`.

#### Options

```bash
python3 run.py --all-pages        # scrape all 46 PPT pages instead of just page 1
python3 run.py --skip-images      # skip eBay photo analysis, email all filtered cards
python3 run.py --skip-email       # analyze but don't send email
```

## Architecture

```
run.py                          ← single entry point
execution/
  scrape_ppt.py                 ← scrapes pokemonpricetracker.com (Playwright)
  analyze_card_images.py        ← eBay listing search + Claude + Ximilar grading
  run_analysis.py               ← orchestrates pipeline, formats and sends email
directives/                     ← plain-English SOPs for each step
.env                            ← API keys (never committed)
.tmp/                           ← intermediate files, auto-regenerated
```

## Raspberry Pi

This is designed to run on a Raspberry Pi (arm64). The Pi's local IP bypasses the cloud-IP block that pokemonpricetracker.com applies. No Codespace or cloud runner needed.

Tested on Python 3.11+.
