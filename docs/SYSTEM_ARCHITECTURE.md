# System Architecture

Historical note: this file predates the current image-first, HTML-fallback implementation and is kept as legacy reference. Use [docs/ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md) for the current architecture.

## Purpose

This project scrapes Amazon product detail pages (PDPs) and extracts only the fields requested or defaulted per ASIN. The architecture is retrieval-first:

1. Load one PDP with Selenium.
2. Extract visible product content as section-level plain text blocks.
3. Normalize those blocks into chunk records.
4. Rank chunks against each requested field with embeddings.
5. Ask the chat model to extract a structured value from the top-ranked evidence only.
6. Export one CSV row per ASIN with value, source, confidence, and evidence.

The current implementation is optimized for batch extraction of a small set of product attributes such as `serving_size`, `ingredients`, `packaging_type`, and `servings_per_container`.

## High-Level Flow

```mermaid
flowchart TD
    A[Input CSV\nasin + required_fields or product_type] --> B[run_product_batch.py]
    B --> C[read_jobs -> ScrapeJob]
    B --> D[OpenAIConfig.from_env]
    B --> E[AmazonScraper.run_batch]
    E --> F[Shared Chrome WebDriver]
    F --> G[Prepare Amazon session\nlanguage + delivery ZIP]
    G --> H[Load PDP /dp/{asin}]
    H --> I[BeautifulSoup parse]
    I --> J[HTML extractors\nsection-level plain text only\ntitle, facts block, bullets,\ndescription block, variation block]
    J --> K[build_chunk_records]
    K --> L[ChunkRecord[]]
    L --> M[DenseRetriever.rank_chunks\nper requested field]
    M --> N[OpenAIFieldExtractor.extract_field]
    N --> O[FieldResult map]
    O --> P[ScrapeResult]
    P --> Q[write_results_csv]
    Q --> R[Output CSV]
```

## Runtime Components

### 1. Batch entrypoint

`run_product_batch.py` owns CLI parsing, input loading, OpenAI configuration, scraper construction, and CSV export.

Responsibilities:
- Parse `--input-csv`, `--output-csv`, `--headless`, logging flags, and request pacing.
- Read the input CSV into `ScrapeJob` records.
- Load product-type field defaults from `schemas/product_type_fields.json` and use them when `required_fields` is omitted.
- Fail fast if OpenAI configuration is missing.
- Run a batch with one shared browser session.
- Compute the union of requested fields to define CSV output columns.

### 2. Scraper orchestration

`amazon_scraper.scraper.AmazonScraper` is the main application service.

Responsibilities:
- Create and manage Chrome WebDriver instances.
- Prepare a reusable Amazon session.
- Apply a delivery ZIP context (`10001` by default) so PDP content is more stable.
- Detect blocked, not-found, partial, and healthy pages.
- Coordinate HTML extraction, chunking, retrieval, and LLM field extraction.
- Return normalized `ScrapeResult` objects.

Important implementation details:
- `run_batch()` reuses one browser session across jobs to reduce startup cost.
- `_prepare_session()` is session-aware and runs only once per WebDriver session.
- `_wait_for_non_blocked_page()` allows manual CAPTCHA resolution in headed mode.
- `_fetch_page_source()` performs a light retry when expected PDP markers are missing.

### 3. HTML extraction layer

`amazon_scraper.html_extractors` converts Amazon DOM sections into plain text blocks. This layer does not parse structure ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â no key/value row extraction, no dimension label normalization, no ARIA attribute walking. Its only job is to locate known page sections and return their visible text.

Public functions:
- `detect_page_state()` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â classifies the rendered page into operational states
- `extract_title()` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â returns the product title as a single string
- `extract_bullets()` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â returns a list of plain text bullet strings
- `extract_description_text()` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â returns merged plain text from description and A+ sections
- `extract_facts_table()` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â returns plain text from the facts/details/overview containers as a single string
- `extract_variations()` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â returns plain text of currently selected variation labels and values as a single string

All semantic interpretation of what a label means, what dimension a variation belongs to, and how a field maps to page content is handled downstream by the retrieval and extraction layers. If Amazon changes a container's internal structure, only the selector in the relevant extractor function needs updating.

### 4. Chunking layer

`amazon_scraper.chunking.build_chunk_records()` converts plain text sections into `ChunkRecord` instances.

Each chunk includes:
- `section`: logical origin such as `facts_table` or `bullets`
- `text`: normalized content used for retrieval
- `dom_hint`: a lightweight provenance hint
- `order`: stable position within the page-derived sequence
- `source_priority`: tie-breaker favoring more trustworthy sections

Section priority currently favors:
1. `variations`
2. `facts_table`
3. `bullets`
4. `description`
5. `title`

Description text is split into sentence-like chunks up to about 500 characters to keep retrieval granular. Facts and variation text are passed as single blocks since their internal structure is preserved as plain text and the retrieval layer handles relevance.

### 5. Retrieval layer

`amazon_scraper.retrieval.DenseRetriever` ranks chunks for a specific field query.

Responsibilities:
- Embed the field query text.
- Embed chunk text.
- Cache embeddings in `.cache/embeddings.json`.
- Score chunk similarity with cosine similarity.
- Return the top `k` chunks, with priority/order tie-breakers.

Ranking tuple:
- cosine similarity
- `source_priority`
- inverse `order` so earlier page evidence wins ties

This layer is what keeps the chat extraction prompt narrow instead of sending the entire page.

### 6. Field extraction layer

`amazon_scraper.extraction.OpenAIFieldExtractor` converts retrieved evidence into a `FieldResult`.

Responsibilities:
- Build a prompt tailored to the requested field.
- Include label aliases from `field_hints.py`.
- Force JSON output from the chat model.
- Parse and normalize the returned value.
- Drop low-confidence or unsupported values.

Important guardrails:
- The system prompt explicitly instructs the model to use only the supplied evidence chunks.
- Empty values are returned when evidence is insufficient.
- `confidence_threshold` defaults to `0.45`.
- If the model omits source/evidence, the extractor falls back to the top-ranked chunk.

### 7. Parsing and normalization

Two modules shape data quality:

- `amazon_scraper.field_hints`
  - Maps field names to parser types and label aliases.
- `amazon_scraper.value_parsers`
  - Normalizes extracted values into stable output forms.

Examples:
- `serving_size` exports numeric values like `30`
- `servings_per_container` exports numeric counts like `30`
- `ingredients` exports short lists joined with ` | `
- `packaging_type` exports cleaned text values such as `pouch`

### 8. Output layer

`amazon_scraper.csv_export.write_results_csv()` writes the final batch output.

Per requested field, the CSV contains:
- `<field>`
- `<field>_source`
- `<field>_confidence`
- `<field>_evidence`

The fixed columns are:
- `asin`
- `product_type`
- `status`
- `url`
- `scraped_at`
- `title`
- `error`

### 9. Cross-cutting concerns

Logging:
- `amazon_scraper.logging_utils.configure_logging()` sets live terminal logging and rotating-file logging.
- Console logging is enabled by default and can be disabled with `--no-console-log`.
- Selenium wire logs are reduced to keep logs readable.
- Uncaught exceptions and warnings are routed into logging.

OpenAI API usage logging:
- Every embedding and chat/completions call emits a structured DEBUG-level log entry immediately after the response is received.
- Embedding call entries include: `call_type`, `model`, `input_count`, `total_tokens`, `cache_hits`, `estimated_cost_usd`, `batch_estimated_cost_usd`, `max_cost_usd`.
- Chat call entries include: `call_type`, `model`, `asin`, `field`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `retry_attempt`, `prompt_cost_usd`, `completion_cost_usd`, `estimated_cost_usd`, `batch_estimated_cost_usd`, `max_cost_usd`.
- At the end of each ASIN job, one INFO-level summary is emitted with token totals, estimated cost for that job, cumulative batch estimated cost, and the configured max cost.
- At the end of the full batch run, one INFO-level batch summary is emitted with total token usage, total estimated cost, job counts, and whether the batch stopped early.
- All usage entries use the key prefix `api_usage` so they can be filtered from log files without parsing prose.
- Cost estimation is optional and first tries a built-in price catalog for recognized OpenAI models, with manual overrides available when needed.
- If `max_cost_usd` is configured, the run stops immediately after the API call that reaches or exceeds the limit.

Configuration:
- `amazon_scraper.openai_client.OpenAIConfig.from_env()` requires:
  - `OPENAI_API_KEY`
  - `OPENAI_EMBEDDING_MODEL`
  - `OPENAI_CHAT_MODEL`
- Optional cost controls:
  - `OPENAI_MAX_COST_USD`
  - `OPENAI_EMBEDDING_COST_PER_1M_TOKENS`
  - `OPENAI_CHAT_INPUT_COST_PER_1M_TOKENS`
  - `OPENAI_CHAT_OUTPUT_COST_PER_1M_TOKENS`

External dependencies:
- Selenium + Chrome / ChromeDriver
- BeautifulSoup
- OpenAI HTTP API

## Data Contracts

### Input CSV

Required columns:
- `asin`
- one of `required_fields` / `fields`, or `product_type` with a matching entry in `schemas/product_type_fields.json`

Optional columns:
- `product_type` when explicit fields are already supplied

`required_fields` accepts comma, pipe, or semicolon separators.
If both `product_type` and `required_fields` are present, `required_fields` wins for that row.
`product_type` values are normalized, so spaces and hyphens resolve to the same key as underscores.

Examples:

```csv
asin,product_type,required_fields
B0FGX4T6D8,supplement,"serving_size,ingredients,packaging_type"
```

```csv
asin,product_type
B0FGX4T6D8,supplement
```

### Core runtime objects

`ScrapeJob`
- input contract for one ASIN request

`ChunkRecord`
- normalized retrieval unit derived from one PDP section

`FieldResult`
- normalized field output with provenance and confidence

`ScrapeResult`
- full per-ASIN result returned by the scraper

## Operational States

The scraper currently uses these page/result states:

- `OK` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â title found and page looks healthy
- `PARTIAL` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â page loaded but expected PDP signals are incomplete
- `BLOCKED` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â Amazon CAPTCHA or anti-bot page detected
- `NOT_FOUND` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â PDP does not exist or is unavailable
- `NAVIGATION_ERROR` ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â exception raised during scraping or extraction

## Design Decisions

### Section-level plain text extraction instead of structured DOM parsing

Why:
- Amazon's DOM structure for facts tables and variation pickers changes frequently
- Key/value row parsing and dimension label normalization are fragile and require ongoing maintenance
- All semantic interpretation is handled more reliably by the retrieval and extraction layers
- Reduces `html_extractors.py` to a thin, stable text-extraction layer that only needs selector updates when containers move

Tradeoff:
- Section-level text blocks are less granular than individual row chunks for facts/details
- The extraction model must infer structure from plain text evidence rather than pre-parsed key/value pairs
- Retrieval quality for tightly structured fields (e.g. a specific nutrition row) depends on embedding similarity being strong enough to surface the right block

### Retrieval-first extraction instead of whole-page prompting

Why:
- lowers token usage
- narrows the prompt to relevant evidence
- makes provenance easier to explain
- reduces hallucination risk relative to sending the full page

Tradeoff:
- if the correct evidence is never surfaced into the top-ranked chunks, extraction misses the field

### Shared browser session for batch runs

Why:
- avoids repeated WebDriver startup cost
- keeps one Amazon delivery/location context across jobs

Tradeoff:
- batch runs are mostly sequential and stateful by design

### Local embedding cache

Why:
- repeated fields and repeated PDP text are common
- caching materially reduces OpenAI calls across reruns

Tradeoff:
- cached embeddings persist until manually cleared, so model changes should use a new embedding model name or a cleared cache

## Extension Points

### Add a new extractable field

1. Add aliases and parser selection in `amazon_scraper/field_hints.py`.
2. Add or extend a parser in `amazon_scraper/value_parsers.py` if needed.
3. Include the new field in `required_fields` in the input CSV.

No scraper orchestration or HTML extractor changes are required if the evidence already appears in one of the extracted section text blocks.

### Add or update a product type field set

1. Edit `schemas/product_type_fields.json`.
2. Add or update the category key, for example `mct_oil` or `liposomal_vitamin_c`.
3. Add the default field list for that category.
4. Only edit `amazon_scraper/product_type_schemas.py` if you want different normalization behavior.

### Improve robustness against Amazon DOM changes

Edit only `amazon_scraper/html_extractors.py`:
- add or update CSS selectors for the affected section
- no row parsing logic or label normalization to maintain

### Change retrieval behavior

Edit `amazon_scraper/retrieval.py`:
- adjust `top_k`
- alter ranking tie-breakers
- swap caching strategy

### Change export shape

Edit `amazon_scraper/csv_export.py`.

## Known Constraints

- The scraper is tuned for Amazon US PDPs.
- It assumes Chrome is installed or Selenium Manager can provision a compatible driver.
- Amazon CAPTCHA can block fully automated execution.
- The project currently runs jobs sequentially.
- Default field sets come from `schemas/product_type_fields.json`, so unknown product types without explicit `required_fields` are skipped.
- Facts and variation text are passed as single plain text blocks; extraction quality for highly specific structured attributes depends on retrieval surfacing the right block.

## Recommended Reading Order

For a new developer, read files in this order:

1. `run_product_batch.py`
2. `amazon_scraper/scraper.py`
3. `amazon_scraper/html_extractors.py`
4. `amazon_scraper/chunking.py`
5. `amazon_scraper/retrieval.py`
6. `amazon_scraper/extraction.py`
7. `amazon_scraper/field_hints.py`
8. `amazon_scraper/value_parsers.py`
9. `amazon_scraper/csv_export.py`


