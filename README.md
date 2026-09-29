# Amazon Product Scraper

Amazon PDP scraper for structured field extraction with an image-first, HTML-fallback pipeline.

## Start Here

If you are new to the repo or feeling lost in the code, read these in order:

1. [docs/START_HERE.md](docs/START_HERE.md)
2. [docs/ARCHITECTURE_OVERVIEW.md](docs/ARCHITECTURE_OVERVIEW.md)
3. [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md)
4. [docs/decisions/ADR-001-image-first-html-fallback-extraction.md](docs/decisions/ADR-001-image-first-html-fallback-extraction.md)
5. [AGENTS.md](AGENTS.md)

## What Is Implemented

- Loads each Amazon PDP once with Selenium using a reusable Amazon session and a New York delivery ZIP context.
- Extracts title, bullets, facts/details text, description text, variations, and gallery image metadata from the rendered PDP.
- Resolves requested fields with image-first behavior when gallery evidence is useful, then falls back to HTML retrieval and chat extraction when image evidence is weak or missing.
- Uses OpenAI embeddings to rank HTML chunks for text-based fallback extraction.
- Uses an OpenAI chat model for structured field extraction and an optional vision-capable model for gallery image classification and image-based extraction.
- Writes CSV output with `<field>`, `<field>_source`, `<field>_confidence`, and `<field>_evidence`.
- Supports both CLI batch runs and a local Streamlit UI with progress updates, download output, and a real stop button.
- Optionally estimates OpenAI cost from built-in pricing for recognized models and stops the batch when a configured USD limit is reached.
- Resolves default field sets by product type from `schemas/product_type_fields.json`.

## Documentation

- Onboarding: [docs/START_HERE.md](docs/START_HERE.md)
- Repo rules for agents and contributors: [AGENTS.md](AGENTS.md)
- Current architecture: [docs/ARCHITECTURE_OVERVIEW.md](docs/ARCHITECTURE_OVERVIEW.md)
- Current developer guide: [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md)
- Implementation readiness and rollout checklist: [docs/RUN_SCOPED_SCHEMA_IMPLEMENTATION_CHECKLIST.md](docs/RUN_SCOPED_SCHEMA_IMPLEMENTATION_CHECKLIST.md)
- Slice 1 implementation spec: [docs/RUN_SCOPED_SCHEMA_SLICE1_SPEC.md](docs/RUN_SCOPED_SCHEMA_SLICE1_SPEC.md)
- Legacy architecture reference: [docs/SYSTEM_ARCHITECTURE.md](docs/SYSTEM_ARCHITECTURE.md)
- Legacy codebase guide: [docs/CODEBASE_GUIDE.md](docs/CODEBASE_GUIDE.md)
- UI spec history: [docs/UI_BATCH_SPEC.md](docs/UI_BATCH_SPEC.md)
- Architecture decision record: [docs/decisions/ADR-001-image-first-html-fallback-extraction.md](docs/decisions/ADR-001-image-first-html-fallback-extraction.md)
- Engineering guardrails: [docs/decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md](docs/decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md)
- Proposed run-scoped schema and canonicalization direction: [docs/decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md](docs/decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md)

## Input Format
Explicit fields per row:
```csv
asin,required_fields
B0FGX4T6D8,"serving_size,ingredients,packaging_type"
```

Product-type defaults from the schema file:
```csv
asin,product_type
B0FGX4T6D8,supplement
```

Mixed mode with an explicit override:
```csv
asin,product_type,required_fields
B0FGX4T6D8,supplement,"serving_size,ingredients"
```

Notes:
- `required_fields` accepts comma, pipe, or semicolon separators.
- If `required_fields` is present, it overrides the product-type defaults for that row.
- `product_type` values are normalized, so `mct oil`, `mct-oil`, and `mct_oil` all resolve to the same schema key.
- Edit `schemas/product_type_fields.json` to add or change default field sets.

## Required Environment
Set these before running:
- `OPENAI_API_KEY`
- `OPENAI_EMBEDDING_MODEL`
- `OPENAI_CHAT_MODEL`

Optional:
- `OPENAI_VISION_MODEL` to use a different model for image-first extraction and classification. If omitted, the chat model is reused.
- `OPENAI_VISION_REMOTE_URLS` if you explicitly want OpenAI to fetch remote image URLs itself. The default is `false`, which keeps vision requests inline-only by downloading the selected image in this scraper process and sending base64 image data to OpenAI.
- `RUN_SCOPED_SCHEMA_ENABLED` to enable the new run-scoped field-contract runtime path. Keep this `false` by default in production until the migration slices are validated.

The process fails fast at startup if the required OpenAI variables are missing.

A local `.env` file in the repo root is loaded automatically. Use `.env.example` as the template.

## Optional Cost Controls
You can enable cost estimation and a hard batch budget with environment variables or CLI flags.

Environment variables:
- `OPENAI_MAX_COST_USD`
- `OPENAI_EMBEDDING_COST_PER_1M_TOKENS`
- `OPENAI_CHAT_INPUT_COST_PER_1M_TOKENS`
- `OPENAI_CHAT_OUTPUT_COST_PER_1M_TOKENS`

CLI flags:
- `--max-cost-usd`
- `--embedding-cost-per-1m-tokens`
- `--chat-input-cost-per-1m-tokens`
- `--chat-output-cost-per-1m-tokens`

Notes:
- Known chat and embedding models use a built-in pricing catalog based on the configured model names.
- CLI flags or env vars still override the built-in prices when you need to pin or correct them.
- If a configured model is not in the built-in pricing catalog, set its prices manually with env vars or CLI flags.
- The stop happens immediately after the API call that reaches or exceeds the configured limit, so the final call can take the total slightly over the cap.
- If the run stops mid-batch, completed rows are still written to the output CSV.

## Run
Install dependencies:

```bash
pip install -r requirements.txt
```

CLI batch run:

```bash
python run_product_batch.py
```

Headless CLI run:

```bash
python run_product_batch.py --headless
```

CLI with terminal logging and an automatic cost cap:
```bash
python run_product_batch.py --headless --max-cost-usd 1.50
```

Local Streamlit UI:
```bash
python -m streamlit run ui_app.py
```

The UI lets you build mixed-product batches. Each ASIN row can use its own `product_type` and `required_fields`, and human-friendly field names are normalized before the scraper runs.

Console logging is enabled by default and is written live to the terminal as well as the rotating log file. Use `--no-console-log` only if you want file-only logging.

UI notes:
- The UI defaults to `headless = false` so you can watch visible-browser runs during testing.
- The UI writes live scraper logs to `logs/streamlit_scraper.log`.
- The UI has a `Stop Batch` button that requests cancellation, closes the active browser session, and preserves partial completed rows.

Sample input:
- `inputs/sample_product_asins.csv`

Default output:
- `outputs/product_batch_results.csv`

## Repository Map
- `run_product_batch.py`: user-facing CLI entrypoint
- `ui_app.py`: Streamlit UI for guided batch creation and execution
- `ui_run_manager.py`: background run management, stop handling, and download state for the UI
- `amazon_scraper/scraper.py`: Selenium orchestration and end-to-end job execution
- `amazon_scraper/input_normalization.py`: shared ASIN and field normalization for CLI and UI input
- `amazon_scraper/gallery_images.py`: gallery metadata parsing and image-tier selection
- `amazon_scraper/vision_extraction.py`: image classification and image-first field resolution
- `amazon_scraper/html_extractors.py`: DOM parsing and page-state detection
- `amazon_scraper/chunking.py`: converts page content into retrieval chunks
- `amazon_scraper/retrieval.py`: embeddings, cache, and chunk ranking
- `amazon_scraper/extraction.py`: field prompts, JSON parsing, normalization, and HTML fallback extraction
- `amazon_scraper/product_type_schemas.py`: product-type normalization and default field-set loading
- `amazon_scraper/csv_export.py`: final CSV writing
- `tests/`: unit coverage for extraction, image selection, input normalization, scraper behavior, and OpenAI client behavior

## Notes
- Embeddings are cached locally in `.cache/embeddings.sqlite3`.
- Vision analysis is cached locally in `.cache/vision.sqlite3`.
- Both are SQLite stores written one row at a time. To carry over entries from the
  older `.json` caches, run `python -m amazon_scraper.run_cache_migration` once.
- `product_type` can drive default fields, but extraction still runs strictly against the resolved field list for that row.
- `run_product_batch.py` is the primary CLI entrypoint.

