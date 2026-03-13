# System Architecture

## Purpose

This project scrapes Amazon product detail pages (PDPs) and extracts only the fields requested per ASIN. The architecture is retrieval-first:

1. Load one PDP with Selenium.
2. Normalize the visible product content into chunk records.
3. Rank chunks against each requested field with embeddings.
4. Ask the chat model to extract a structured value from the top-ranked evidence only.
5. Export one CSV row per ASIN with value, source, confidence, and evidence.

The current implementation is optimized for batch extraction of a small set of product attributes such as `serving_size`, `caffeinated`, `caffeine_per_serving`, and `roast_level`.

## High-Level Flow

```mermaid
flowchart TD
    A[Input CSV\nasin + required_fields] --> B[run_mushroom_batch.py]
    B --> C[read_jobs -> ScrapeJob]
    B --> D[OpenAIConfig.from_env]
    B --> E[AmazonScraper.run_batch]
    E --> F[Shared Chrome WebDriver]
    F --> G[Prepare Amazon session\nlanguage + delivery ZIP]
    G --> H[Load PDP /dp/{asin}]
    H --> I[BeautifulSoup parse]
    I --> J[HTML extractors\n title, facts, bullets,\n description, variations]
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

`run_mushroom_batch.py` owns CLI parsing, input loading, OpenAI configuration, scraper construction, and CSV export.

Responsibilities:
- Parse `--input-csv`, `--output-csv`, `--headless`, logging flags, and request pacing.
- Read the input CSV into `ScrapeJob` records.
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

`amazon_scraper.html_extractors` converts Amazon DOM structures into normalized Python data.

Extractors:
- `extract_title()`
- `extract_facts_table()`
- `extract_bullets()`
- `extract_description_text()`
- `extract_variations()`
- `detect_page_state()`

This layer is intentionally selector-heavy and isolated so DOM changes can be handled without touching retrieval or export logic.

### 4. Chunking layer

`amazon_scraper.chunking.build_chunk_records()` converts extracted HTML content into `ChunkRecord` instances.

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

Description text is split into sentence-like chunks up to about 500 characters to keep retrieval granular.

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
- The system prompt explicitly says to use only supplied evidence.
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
- `yes_no_or_status` maps caffeine phrases to `Yes` / `No`
- `amount_with_unit` pulls values like `50 mg`
- `serving_size` extracts values like `30 servings`
- `roast_level` normalizes phrases like `Medium Roast`

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
- `amazon_scraper.logging_utils.configure_logging()` sets console and rotating-file logging.
- Selenium wire logs are reduced to keep logs readable.
- uncaught exceptions and warnings are routed into logging.

Configuration:
- `amazon_scraper.openai_client.OpenAIConfig.from_env()` requires:
  - `OPENAI_API_KEY`
  - `OPENAI_EMBEDDING_MODEL`
  - `OPENAI_CHAT_MODEL`

External dependencies:
- Selenium + Chrome / ChromeDriver
- BeautifulSoup
- OpenAI HTTP API

## Data Contracts

### Input CSV

Required columns:
- `asin`
- `required_fields` or `fields`

Optional columns:
- `product_type`

`required_fields` accepts comma, pipe, or semicolon separators.

Example:

```csv
asin,product_type,required_fields
B0B53Y7Z7D,mushroom_coffee,"serving_size,caffeinated,caffeine_per_serving,roast_level"
```

### Core runtime objects

`ScrapeJob`
- input contract for one ASIN request

`ChunkRecord`
- normalized retrieval unit derived from one PDP

`FieldResult`
- normalized field output with provenance and confidence

`ScrapeResult`
- full per-ASIN result returned by the scraper

## Operational States

The scraper currently uses these page/result states:

- `OK`
  - title found and page looks healthy
- `PARTIAL`
  - page loaded but expected PDP signals are incomplete
- `BLOCKED`
  - Amazon CAPTCHA or anti-bot page detected
- `NOT_FOUND`
  - PDP does not exist or is unavailable
- `NAVIGATION_ERROR`
  - exception raised during scraping or extraction

## Design Decisions

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

No scraper orchestration changes are required if existing page content already contains the evidence.

### Improve robustness against Amazon DOM changes

Edit only `amazon_scraper/html_extractors.py` first:
- add selectors
- update row parsing logic
- add new variation detection rules

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
- `schemas/` exists in the repo but is not used by the current code path.

## Recommended Reading Order

For a new developer, read files in this order:

1. `run_mushroom_batch.py`
2. `amazon_scraper/scraper.py`
3. `amazon_scraper/html_extractors.py`
4. `amazon_scraper/chunking.py`
5. `amazon_scraper/retrieval.py`
6. `amazon_scraper/extraction.py`
7. `amazon_scraper/field_hints.py`
8. `amazon_scraper/value_parsers.py`
9. `amazon_scraper/csv_export.py`
