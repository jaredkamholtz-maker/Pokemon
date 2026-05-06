# Directives

This directory contains SOPs (Standard Operating Procedures) written in Markdown.

Each directive file describes:
- **Goal**: What the workflow accomplishes
- **Inputs**: What data or parameters are required
- **Tools/Scripts**: Which `execution/` scripts to use
- **Outputs**: What the deliverable looks like and where it lives
- **Edge Cases**: Known failure modes and how to handle them

## Usage

The orchestration layer (AI) reads these files to understand how to route a task.
Directives are **living documents** — update them when you discover API constraints,
better approaches, or common errors.

## Naming convention

`<verb>_<subject>.md` — e.g. `scrape_website.md`, `enrich_leads.md`, `generate_report.md`
