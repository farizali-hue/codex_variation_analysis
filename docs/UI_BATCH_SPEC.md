# Spec: Local Batch UI For Amazon PDP Extraction

This file is the original UI planning spec.

Use [README.md](../README.md), [docs/START_HERE.md](START_HERE.md), and [docs/ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md) for the current implemented behavior. Some sections below are historical and still contain open-question language from before the UI was fully built.

## Objective

Build a local operator-facing UI that replaces fragile hand-edited CSV input for Amazon PDP extraction jobs.

The UI should let a user:
- paste a list of ASINs
- choose a default product type for new rows and then edit product type per row
- add required fields one by one using a guided picker
- review the normalized job list before scraping starts
- run the existing scraper pipeline and download the results as a spreadsheet

The main goal is to improve input reliability without weakening extraction reliability.

## Tech Stack

Current stack:
- Python 3.13 virtualenv in `.variation_analysis`
- Selenium
- BeautifulSoup
- python-dotenv

Proposed UI stack:
- Python backend
- Streamlit for the local operator UI

Default implementation proposal:
- `Streamlit` for the local UI shell
- shared normalization and scraper modules reused from the CLI path

Why this proposal:
- easier to use than raw CSV
- faster to build than a custom HTML app
- good fit for copy-paste workflows
- can reuse the existing scraper modules directly

## Commands

Current setup:
- `d:\codex_variation_analysis\.variation_analysis\Scripts\python.exe -m pip install -r requirements.txt`
- `d:\codex_variation_analysis\.variation_analysis\Scripts\python.exe -m unittest discover -s tests`

Proposed development commands after the UI is added:
- `d:\codex_variation_analysis\.variation_analysis\Scripts\python.exe -m pip install -r requirements.txt`
- `d:\codex_variation_analysis\.variation_analysis\Scripts\python.exe -m unittest discover -s tests`
- `d:\codex_variation_analysis\.variation_analysis\Scripts\python.exe -m streamlit run ui_app.py`

## Project Structure

Existing core code stays in place:
- `amazon_scraper/` -> scraping, extraction, retrieval, export
- `run_product_batch.py` -> existing CLI entrypoint
- `tests/` -> unit tests
- `docs/` -> design and operational docs

Proposed additions:
- `ui_app.py` -> local Streamlit entrypoint
- `amazon_scraper/input_normalization.py` -> normalize ASIN and field input from the UI and CLI
- `tests/test_input_normalization.py` -> alias, typo, and ambiguity coverage
- `tests/test_ui_job_builder.py` -> UI-side job assembly behavior

## Code Style

Principles:
- keep the existing scraper pipeline reusable from both CLI and UI
- be forgiving at the user-input boundary, strict after normalization
- do not let UI free text leak directly into scraping logic
- keep all canonical field names internal and stable

Example normalization contract:

```python
NormalizedFieldSelection(
    raw_input="Ingredient list",
    canonical_name="ingredients",
    matched_by="alias",
)
```

## Testing Strategy

Test levels:
- unit tests for ASIN parsing and field normalization
- unit tests for ambiguity handling and typo correction thresholds
- unit tests for product-type default field loading
- unit tests for job preview generation
- regression tests for blocked and not-found pages returning early without LLM calls

Manual checks:
- paste multiple ASINs separated by newlines, commas, and spaces
- add fields via searchable input and verify canonical preview
- verify unknown or ambiguous fields are rejected before a run starts
- verify batch result export preserves requested field order
- verify UI never changes selected product variations during safe section expansion

## Boundaries

- Always:
  - validate ASIN format before building jobs
  - normalize all user-entered fields into canonical internal field names
  - reject ambiguous field matches instead of guessing
  - stop early on `BLOCKED` and `NOT_FOUND` pages
  - preserve requested field order in the final spreadsheet
- Ask first:
  - adding a new third-party UI dependency
  - changing the current scraper output schema
  - changing the scraper to run jobs in parallel
- Never:
  - click Amazon variation selectors as part of section expansion
  - silently invent a new field name because of user spelling
  - continue LLM extraction on blocked or missing PDPs

## Success Criteria

- A user can paste a mixed-format ASIN list and build a valid batch without editing CSV files by hand.
- A user can run mixed-product batches where each ASIN row has its own `product_type` and `required_fields`.
- A user can add required fields through a guided UI one by one.
- Free-text field entry is treated as search input, not as authoritative field IDs.
- The UI resolves known aliases such as `Serving Size`, `serving-size`, and `ingredient list` to canonical internal names.
- The UI rejects ambiguous inputs such as `size` unless the user picks a specific field.
- Product-type defaults can prefill the field list for a row or batch.
- The backend receives only canonical field names.
- The spreadsheet output preserves the exact reviewed field order.
- The scraper expands safe content sections only and does not change selected product variations.
- `BLOCKED` and `NOT_FOUND` PDPs do not trigger retrieval or chat extraction.

## Open Questions

- Should product type be selected once for the full batch, or optionally overridden per ASIN row?
- Do you want the first version to support only local single-user runs, or eventually multiple users?
- Do you want the first version to support only one batch-level field list, or row-level field overrides too?


