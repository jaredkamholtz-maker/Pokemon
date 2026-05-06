# Execution Scripts

This directory contains deterministic Python scripts — the "doing" layer of the architecture.

## Principles

- Each script does one thing well and is independently runnable
- All secrets/tokens come from `.env` (never hardcoded)
- Scripts are commented to explain non-obvious logic
- Output goes to `.tmp/` unless it's a final cloud deliverable

## Usage

Scripts are invoked by the orchestration layer after reading the relevant directive.
Check here before writing a new script — reuse what exists.

## Naming convention

`<verb>_<subject>.py` — mirrors the directive that describes it,
e.g. `scrape_single_site.py`, `enrich_leads.py`, `push_to_sheets.py`
