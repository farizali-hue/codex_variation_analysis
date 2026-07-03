# CLAUDE.md

Guidance for Claude Code (and any AI agent) working in this repository. This
file is loaded automatically at the start of every session. Read it first, then
follow the pointers below before giving substantive guidance or writing code.

## What this project is

An Amazon PDP scraper for structured field extraction with an **image-first,
HTML-fallback** pipeline. It loads a product page once, tries image (gallery)
evidence first when that is the most reliable source, falls back to embedding-
ranked HTML retrieval + chat extraction, and writes a CSV row carrying
`value`, `source`, `confidence`, and `evidence` per requested field. There is
a CLI (`run_product_batch.py`) and a local Streamlit UI (`ui_app.py`).

## Read these before architectural work

The repo is deliberately documented for onboarding. Load in this order:

1. @AGENTS.md — the always-on working contract for how to reason, explain,
   escalate, and implement here. **Follow it.**
2. [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) — **current handoff
   state**: what is done, what works today, and the agreed follow-up backlog.
   Read this before proposing work; update it when you finish a phase.
3. [docs/START_HERE.md](docs/START_HERE.md) — 5-minute mental model + "if you
   want to change X, go here" map.
4. [docs/ARCHITECTURE_OVERVIEW.md](docs/ARCHITECTURE_OVERVIEW.md)
5. [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md)
6. ADRs — the accepted/proposed architecture:
   - [ADR-001 image-first / HTML-fallback](docs/decisions/ADR-001-image-first-html-fallback-extraction.md)
   - [ADR-002 engineering guardrails](docs/decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md)
   - [ADR-003 run-scoped typed field contracts + generic canonicalization](docs/decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md)

The four files to hold in your head: [scraper.py](amazon_scraper/scraper.py)
(orchestrator), [vision_extraction.py](amazon_scraper/vision_extraction.py)
(image-first), [extraction.py](amazon_scraper/extraction.py) +
[retrieval.py](amazon_scraper/retrieval.py) (HTML fallback).

## Setup & run

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows Git Bash; or .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                # then fill in your OWN OpenAI keys
```

Required env (process fails fast at startup if missing): `OPENAI_API_KEY`,
`OPENAI_EMBEDDING_MODEL`, `OPENAI_CHAT_MODEL`. A repo-root `.env` is auto-loaded.
See [README.md](README.md) for the full env list, cost controls, and input CSV
formats.

Run:
```bash
python run_product_batch.py                 # CLI batch (add --headless, --max-cost-usd 1.50)
python -m streamlit run ui_app.py           # local UI
```

Tests / dev extras (not in requirements.txt — install as needed):
```bash
pip install pytest openpyxl                 # openpyxl is needed by the consolidation path
python -m pytest -q
```

## Project knowledge you won't fully get from the code

### Consolidation pipeline (Bronze → Silver)

The scraper emits a wide **detail-mode "raw" CSV** — one column per *literal*
ingredient string (Bronze). The downstream deliverable is a hand-cleaned
**"Consolidated"** market-analysis sheet where each ingredient collapses to one
canonical column (e.g. `Pumpkin Seed Oil (Cucurbita pepo)`, `Organic Pumpkin
Seed Oil`, `Pumpkin Seed Oil` → one `Pumpkin Seed Oil`). The gap being closed
is **shape, not extraction** — values are already correct.

- [amazon_scraper/consolidation.py](amazon_scraper/consolidation.py) — folds
  variant columns into canonical ones, drops parse artifacts, blanks
  `"grammage not mentioned"` sentinels, derives `serving_size` /
  `number_of_capsules_per_container` / `item_form`. Unknown ingredients are
  **kept AND flagged** to `*_needs_review.csv` — never silently merged.
- [amazon_scraper/canonical_ingredients.json](amazon_scraper/canonical_ingredients.json)
  — the synonym→canonical dictionary + drop/rename/normalization rules.
- **TODO (external-data join):** `Brand`, `Reviews`, `Bought in Past Month`,
  `BSR`, `Pack Size` come from per-brand Helium10/search exports, not the PDP;
  the consolidator leaves them blank for a later left-join on ASIN.

### Canonicalization MUST generalize (hard product constraint)

Research spans the **entire supplement category** — new ingredients arrive
constantly. **Do not** grow `canonical_ingredients.json` by hand per product/
per ASIN; a static lookup is the wrong primary mechanism and is unmaintainable
at category scale. Instead:

- Generic, category-wide normalization *rules* are fine (strip trademarks,
  `(as X)`, form/plant-part qualifiers, word-order swaps).
- The matcher must be **semantic** (embeddings + LLM equivalence), because
  fuzzy string matching mis-merges (it once suggested `Vitamin K2` → `Vitamin
  B12`). Keep disambiguation guards (D2≠D3, K1≠K2) and a human review queue
  for low-confidence cases.
- Any persisted dictionary must be a **self-populating cache** the engine fills
  from confident decisions — never a config a human maintains by hand.
- Reuse the existing `RunScopedCanonicalizer` engine (LLM judge + embeddings +
  clustering + cache); see ADR-003. This aligns with AGENTS.md rules #3 and #5.

## Conventions (see AGENTS.md for the full contract)

- Prefer **false-negative matching** over false-positive canonical mapping — a
  wrong canonical match is worse than emitting a new custom field.
- Default to **run-scoped** behavior; persistence must be explicit and opt-in.
- Architecture-impacting changes (field resolution, canonicalization, output
  contracts, source precedence, persistence, AI routing) need design-first:
  root problem → options → tradeoffs → recommendation → approval, before code.
- Label temporary fixes as temporary. Don't ship symptom fixes as architecture.

## Do not commit

Generated/secret artifacts are gitignored — keep them that way:
`outputs/`, `logs/`, `.test_artifacts/`, `.cache/`, and `.env`. Each engineer
uses their own `.env` (copy from `.env.example`). Do not commit scraped result
CSVs or session data.
