# Project Status & Handoff

**Last updated: 2026-07-02** (end of the vision-extraction / field-contracts phase).

This file is the session-to-session state of the project: what is done, what
works today, and what is deliberately left open. It exists so an engineer — or
an AI agent — pulling `main` on a fresh machine knows exactly where to pick up
without reading the whole PR history. **If you finish a phase of work, update
this file in the same PR.**

## Where things stand

- **PR #1** (`feature/vision-extraction-field-contracts-ui`) is merged into
  `main` (2026-07-02). It delivered the current system end-to-end:
  - image-first / HTML-fallback extraction (ADR-001), vision gallery pipeline
    ([amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py))
  - run-scoped typed field contracts + generic canonicalization engine
    (ADR-003; `RunScopedCanonicalizer`, contract editor in the UI)
  - detail-mode supplement-facts export (wide raw CSV + per-ingredient
    `*_supplement_facts_rows.csv` ledger sidecar)
  - two Bronze→Silver consolidation paths (see "Working pipeline" below)
  - Streamlit UI ([ui_app.py](../ui_app.py)), CLI batch runner
    ([run_product_batch.py](../run_product_batch.py)), cost controls
  - onboarding/architecture docs (CLAUDE.md, AGENTS.md, docs/, three ADRs)
- **Review state:** five rounds of Codex PR review were fixed and replied to,
  through commit `d8a1d50` (reserved CSV column-name guard + streamlit ≥1.37
  floor). The final commit was **not** re-reviewed (Codex intentionally not
  tagged); there are no known open findings.
- **Tests:** 153 passing — `python -m pytest -q` (needs `pip install pytest`;
  the consolidation path also needs `openpyxl`).

## Picking up on a fresh machine

1. Clone, then `pip install -r requirements.txt` (+ `pytest`, `openpyxl`).
2. `cp .env.example .env` and fill in **your own** OpenAI keys
   (`OPENAI_API_KEY`, `OPENAI_EMBEDDING_MODEL`, `OPENAI_CHAT_MODEL` — the
   process fails fast at startup without them).
3. Note what does **not** transfer via git (all gitignored): `outputs/`
   (scraped results), `logs/`, `.env`, and `.cache/` — the embedding/vision
   caches and the self-populating canonical-ingredients store
   (`.cache/canonical_ingredients_store.json`) start empty on a new machine and
   refill from your own runs. Expect the first canonicalization runs to cost
   slightly more and send more items to review than on the original machine.
4. Sanity check: `python -m pytest -q` → 153 passed.

## Working pipeline today

Scrape (stage 1) — produces the raw wide CSV plus the ingredient ledger:

```bash
python run_product_batch.py --input-csv inputs/<file>.csv \
    --extract-supplement-facts-details --headless --max-cost-usd 2.00
```

UI alternative: `python -m streamlit run ui_app.py` (streamlit ≥ 1.37).

Consolidate (stage 2) — for **new products**, use the semantic path on the
ledger sidecar (LLM judge + embeddings + self-populating store; ADR-003):

```bash
python -m amazon_scraper.run_ingredient_consolidation "outputs/<name>_supplement_facts_rows.csv"
```

The static-dictionary path (`python -m amazon_scraper.consolidation
<raw.csv>`) is calibrated to the pumpkin-seed-oil product (98.3% cell match on
that product only) — do not extend its dictionary per product; see the
"canonicalization must generalize" constraint in CLAUDE.md.

Manual finish (stage 3): clear the `*_needs_review.csv` queue, join the
external market columns (see backlog), assemble the Consolidated xlsx.

## Recently closed

- **Cache storage moved from JSON to SQLite** (2026-09-29).
  `EmbeddingCache`/`VisionCache` rewrote the whole file on every save, which
  reached a `MemoryError` once `.cache/embeddings.json` passed 234 MB. Both now
  sit on `SqliteKeyValueStore` ([amazon_scraper/kv_store.py](../amazon_scraper/kv_store.py)):
  one row per write, nothing loaded at startup. Keys, vectors and every calling
  site are unchanged — embedding vectors pack to float32, which is lossless for
  OpenAI embeddings (verified bit-identical across 230,400 real values).
  Existing entries carry over via `python -m amazon_scraper.run_cache_migration`.

## Known gaps / follow-up backlog

None of these block usage; they are the agreed next-work candidates, roughly
grouped. Several came out of PR #1 review and were deliberately deferred.

**Reliability**
- No retry/backoff on OpenAI 429/5xx — one transient API blip still fails that
  field/job.
- `scraper.py`: attach `exc.partial_results` **before**
  `_finalize_run_scoped_results` in the except paths; guard the `json.loads`
  in `_write_run_scoped_merge_metadata`.

**Output contract**
- Detail-mode CSV drops `source`/`confidence`/`evidence` for extra requested
  fields — provenance gap vs AGENTS.md rule #6.
- External-data join not built: `Brand`, `Reviews`, `Bought in Past Month`,
  `BSR`, `Pack Size` stay blank; they come from per-brand Helium10/search
  exports and need an ASIN left-join after consolidation.

**Extraction coverage**
- `html_extractors`: selected-variation text lacks its dimension label (emits
  "Chocolate", not "Flavor: Chocolate"); dropdown-style twister variants are
  not captured.

**Efficiency**
- `_image_data_url_cache` (base64 page images) grows unbounded in memory.
- Canonical cluster rename doesn't index the new key, so exact repeats
  re-judge instead of hitting the cache.

**Architecture hygiene (AGENTS.md rules #1/#5)**
- Duplicated helpers to consolidate: `_parse_json_response` ×3,
  `_coerce_confidence` ×4, `_build_direct_support_instruction` ×2,
  `OUTPUT_TYPE_LABELS`/`OUTPUT_TYPE_HELP` ×2.
- Hardcoded `_FACTS_FIELDS` / `_METADATA_LABEL_BY_KEY` / `_TEXT_EQUIVALENTS`
  tables should derive from the field registry instead.
- Dead code: `save_custom_field_presets`, `peek_merge_events`,
  `build_query_text`.
- `schemas/custom_field_presets.json` is a hand-maintained persistent config
  loaded silently at scraper init — in tension with AGENTS.md rule #4
  (run-scoped by default); revisit under ADR-003.
- Unify the static-dictionary consolidator with the `RunScopedCanonicalizer`
  semantic path (ADR-003) so there is one canonicalization engine.

## Note for AI agents

CLAUDE.md and AGENTS.md are the working contract — load them first (CLAUDE.md
is auto-loaded). This file is the *state*; the ADRs in
[docs/decisions/](decisions/) are the *why*. Before starting backlog items
marked as architecture-impacting (output contract, canonicalization,
persistence), follow the design-first protocol in AGENTS.md rule #2.
