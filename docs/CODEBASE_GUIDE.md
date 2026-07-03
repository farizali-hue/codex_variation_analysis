# Codebase Guide

Historical note: this file reflects an older view of the repo before the current image-first and UI-run-manager workflow. Use [docs/DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) for the current developer-facing guide.

## What This Repository Does

The repository runs a batch Amazon scraper that extracts only the product fields requested for each ASIN. It is not a generic crawler. It is a focused pipeline:

- load one Amazon PDP
- extract visible page sections as plain text blocks
- convert those blocks into normalized evidence chunks
- retrieve the best chunks for each field
- ask the model for a structured answer using only that evidence
- export the results to CSV

## Repository Layout

```text
.
|-- amazon_scraper/
|   |-- chunking.py
|   |-- csv_export.py
|   |-- extraction.py
|   |-- field_hints.py
|   |-- html_extractors.py
|   |-- logging_utils.py
|   |-- models.py
|   |-- openai_client.py
|   |-- product_type_schemas.py
|   |-- retrieval.py
|   |-- scraper.py
|   `-- value_parsers.py
|-- docs/
|   |-- CODEBASE_GUIDE.md
|   `-- SYSTEM_ARCHITECTURE.md
|-- inputs/
|-- outputs/
|-- logs/
|-- schemas/
|   `-- product_type_fields.json
|-- run_product_batch.py
`-- README.md
```

## How To Read The Code

### Start at the batch entrypoint

Read `run_product_batch.py` first.

What to look for:
- how input rows resolve `requested_fields` from explicit CSV values or product-type defaults
- what environment variables are mandatory
- how output columns are chosen
- where `AmazonScraper` is constructed and executed

### Then read the orchestrator

Read `amazon_scraper/scraper.py` second.

This file contains the actual runtime path:
- build or reuse a Chrome session
- initialize Amazon state
- fetch PDP HTML
- extract page sections as plain text
- build chunks
- retrieve evidence per field
- call the LLM extractor
- return `ScrapeResult`

If you understand this file, you understand the system.

### Read the pipeline stages in order

After `scraper.py`, the most useful sequence is:

1. `amazon_scraper/html_extractors.py`
2. `amazon_scraper/chunking.py`
3. `amazon_scraper/retrieval.py`
4. `amazon_scraper/extraction.py`
5. `amazon_scraper/field_hints.py`
6. `amazon_scraper/value_parsers.py`
7. `amazon_scraper/csv_export.py`

## Module-by-Module Notes

### `amazon_scraper/models.py`

Defines the core dataclasses:
- `ScrapeJob`
- `ChunkRecord`
- `FieldResult`
- `ScrapeResult`

These types are small and worth reading early because they explain the data flow.

### `amazon_scraper/html_extractors.py`

Purpose:
- locate known page sections and return their visible text as plain strings

What this layer does:
- `detect_page_state()` classifies the page (blocked, not found, partial, ok)
- `extract_title()` returns the product title as a single string
- `extract_bullets()` returns a list of plain text bullet strings
- `extract_description_text()` returns merged plain text from description and A+ sections
- `extract_facts_table()` returns the facts/details/overview containers as a single plain text string
- `extract_variations()` returns currently selected variation labels and values as a single plain text string

What this layer does not do:
- no key/value row parsing
- no dimension label normalization
- no ARIA attribute walking
- no stopword filtering
- no structured objects from facts or variations

All semantic interpretation of what labels mean or how fields map to page content is handled downstream by the retrieval and extraction layers.

You will spend time here only when:
- Amazon changes a container selector and a section stops returning text
- page state detection needs a new signal

### `amazon_scraper/chunking.py`

Purpose:
- turn plain text section blocks into retrieval units

Key idea:
- extraction is not performed directly on raw HTML
- instead, the system creates semantically meaningful text chunks with provenance metadata
- facts and variation blocks arrive as single plain text strings and are passed through as single chunks
- description text is split into sentence-like chunks of approximately 500 characters

This is the bridge between scraping and LLM reasoning.

### `amazon_scraper/retrieval.py`

Purpose:
- pick the best evidence for each requested field

Key idea:
- fields are converted into query text using aliases
- chunks are embedded and ranked with cosine similarity
- local caching keeps reruns cheaper

If results are empty despite good page content, inspect this module.

### `amazon_scraper/extraction.py`

Purpose:
- ask the chat model for one field at a time

Important behavior:
- only top-ranked evidence chunks are sent
- output is expected in JSON
- low-confidence values are blanked out
- parser-based normalization happens after model output

If the model appears inconsistent, inspect the prompt construction and confidence threshold here.

### `amazon_scraper/field_hints.py`

Purpose:
- define field-specific aliases and parser names

This is the main configuration surface for new fields.

### `amazon_scraper/product_type_schemas.py`

Purpose:
- normalize product type names and load default field sets from `schemas/product_type_fields.json`

This is the main configuration surface when you want `mct_oil`, `liposomal_vitamin_c`, or any other category to inherit a standard field list without repeating `required_fields` on every row.

### `amazon_scraper/value_parsers.py`

Purpose:
- normalize model outputs into stable CSV values

This module is where loose language becomes machine-friendly output.

### `amazon_scraper/openai_client.py`

Purpose:
- thin HTTP client for embeddings and chat completions
- emit structured usage log entries after every API call
- optionally estimate spend and stop when a configured budget is reached

Notable detail:
- the code uses the raw HTTP API via `urllib`, not the OpenAI Python SDK
- every call logs a DEBUG entry with `api_usage` prefix containing model, token counts, and call metadata
- embedding calls additionally log `cache_hits`, `estimated_cost_usd`, and cumulative batch estimated cost
- chat calls additionally log `asin`, `field`, `retry_attempt`, and prompt/completion cost estimates so per-field spend is traceable
- pricing is auto-resolved for recognized model names from a built-in catalog, and can still be overridden via env vars or CLI arguments
- if a max cost is configured, the run stops immediately after the API call that reaches or exceeds the threshold

### `amazon_scraper/csv_export.py`

Purpose:
- flatten `ScrapeResult` into CSV rows

This is the only export path in the current implementation.

### `amazon_scraper/logging_utils.py`

Purpose:
- initialize useful logging defaults for CLI runs

Check this file when:
- logs are too noisy
- log rotation needs tuning
- exceptions are not appearing in logs

## End-to-End Sequence For One ASIN

1. A CSV row is parsed into `ScrapeJob(asin, product_type, requested_fields)`, with `requested_fields` coming either from the row itself or from `schemas/product_type_fields.json` via the normalized `product_type`.
2. `AmazonScraper.run_job()` opens `https://www.amazon.com/dp/{asin}`.
3. The HTML is parsed with BeautifulSoup.
4. Extractors return plain text blocks for title, facts, bullets, description, and variations.
5. `build_chunk_records()` creates a ranked set of `ChunkRecord` items.
6. For each requested field, `build_query_text()` creates a retrieval query.
7. `DenseRetriever.rank_chunks()` returns the top evidence chunks.
8. `OpenAIFieldExtractor.extract_field()` asks the chat model for JSON output.
9. `parse_value()` normalizes the returned value.
10. Results are stored in `ScrapeResult.fields`.
11. `write_results_csv()` writes the final row.

## Typical Debugging Paths

### API usage looks higher than expected

Check in this order:
1. Are cache hits being counted correctly? Inspect embedding cache keying in `retrieval.py`.
2. Are agentic fallback retries triggering unnecessarily? Check confidence threshold and retry conditions in `extraction.py`.
3. Filter log output for `api_usage` prefix to see per-call and per-ASIN token and estimated-cost breakdowns.
4. Check the batch summary log entry at the end of the run for total token usage, estimated cost, and whether the batch stopped early.

### A field is always blank

Check in this order:
1. Does the evidence exist on the page in the extracted section text?
2. Did chunking keep that evidence intact?
3. Did retrieval rank the right chunks into the top 5?
4. Did the parser reject the model output?
5. Was confidence below `0.45`?

### Status is `PARTIAL` or `BLOCKED`

Inspect:
- page state detection in `html_extractors.py`
- CAPTCHA waiting behavior in `scraper.py`
- headed vs headless execution

### Output columns look wrong

Inspect:
- `collect_export_columns()` in `run_product_batch.py`
- `write_results_csv()` in `csv_export.py`

### API token usage seems too high

Check in this order:
1. Filter logs for `api_usage` entries to see per-call token counts and estimated costs
2. Check `cache_hits` in embedding log entries ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â low cache hits on reruns means the cache is not being keyed correctly
3. Check `retry_attempt` in chat log entries ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â values above 0 mean agentic fallback is firing frequently, which multiplies chat calls per field
4. Compare per-ASIN INFO summaries against batch summary to identify which ASINs are expensive

## Common Change Scenarios

### Add a new field

Minimum likely changes:
- `amazon_scraper/field_hints.py`
- optionally `amazon_scraper/value_parsers.py`

No HTML extractor changes are needed unless the evidence lives in a section not currently extracted.

### Add or update a product type field set

Start with:
- `schemas/product_type_fields.json`
- `amazon_scraper/product_type_schemas.py` only if you want different normalization rules

This is the path for category-level defaults such as `mct_oil` or `liposomal_vitamin_c`.

### Support a changed Amazon layout

Start with:
- `amazon_scraper/html_extractors.py`

Only the CSS selector for the affected section needs updating. There is no row parsing logic or label normalization to maintain. Only touch retrieval or prompting after confirming the section text is being returned correctly.

### Make extraction stricter or looser

Start with:
- `amazon_scraper/extraction.py`
- optionally `amazon_scraper/value_parsers.py`

### Improve performance

Start with:
- shared-session behavior in `amazon_scraper/scraper.py`
- caching and ranking in `amazon_scraper/retrieval.py`

## Current Gaps

- No automated test suite is present in the repository.
- Product-type defaults depend on `schemas/product_type_fields.json` staying aligned with the categories used in the input CSV.
- No retry/backoff exists for OpenAI HTTP failures beyond surfacing the exception.
- No parallel job execution exists.

## Minimal Mental Model

Keep this model in mind:

- Selenium gets the rendered page.
- BeautifulSoup extracts plain text from known page sections.
- Chunking turns section text into evidence records.
- Embeddings choose which evidence matters for each field.
- The chat model produces a structured answer from that evidence.
- CSV export serializes the result.

That is the whole system.


