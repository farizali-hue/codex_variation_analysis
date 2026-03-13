# Codebase Guide

## What This Repository Does

The repository runs a batch Amazon scraper that extracts only the product fields requested for each ASIN. It is not a generic crawler. It is a focused pipeline:

- load one Amazon PDP
- convert page content into normalized evidence chunks
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
|-- run_mushroom_batch.py
`-- README.md
```

## How To Read The Code

### Start at the batch entrypoint

Read `run_mushroom_batch.py` first.

What to look for:
- how input rows become `ScrapeJob`
- what environment variables are mandatory
- how output columns are chosen
- where `AmazonScraper` is constructed and executed

### Then read the orchestrator

Read `amazon_scraper/scraper.py` second.

This file contains the actual runtime path:
- build or reuse a Chrome session
- initialize Amazon state
- fetch PDP HTML
- extract page sections
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
- convert raw Amazon HTML into structured content

You will spend time here when:
- Amazon changes its DOM
- values stop appearing in the facts table
- selected variation values are missing
- title/bullet/description parsing breaks

The most fragile logic in the repository is here by design.

### `amazon_scraper/chunking.py`

Purpose:
- turn extracted content into retrieval units

Key idea:
- extraction is not performed directly on raw HTML
- instead, the system creates semantically meaningful text chunks with provenance metadata

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
- only top-ranked evidence is sent
- output is expected in JSON
- low-confidence values are blanked out
- parser-based normalization happens after model output

If the model appears inconsistent, inspect the prompt construction and confidence threshold here.

### `amazon_scraper/field_hints.py`

Purpose:
- define field-specific aliases and parser names

This is the main configuration surface for new fields.

### `amazon_scraper/value_parsers.py`

Purpose:
- normalize model outputs into stable CSV values

This module is where loose language becomes machine-friendly output.

### `amazon_scraper/openai_client.py`

Purpose:
- thin HTTP client for embeddings and chat completions

Notable detail:
- the code uses the raw HTTP API via `urllib`, not the OpenAI Python SDK

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

1. A CSV row is parsed into `ScrapeJob(asin, product_type, requested_fields)`.
2. `AmazonScraper.run_job()` opens `https://www.amazon.com/dp/{asin}`.
3. The HTML is parsed with BeautifulSoup.
4. Extractors pull title, facts, bullets, description, and selected variations.
5. `build_chunk_records()` creates a ranked set of `ChunkRecord` items.
6. For each requested field, `build_query_text()` creates a retrieval query.
7. `DenseRetriever.rank_chunks()` returns the top evidence chunks.
8. `OpenAIFieldExtractor.extract_field()` asks the chat model for JSON output.
9. `parse_value()` normalizes the returned value.
10. Results are stored in `ScrapeResult.fields`.
11. `write_results_csv()` writes the final row.

## Typical Debugging Paths

### A field is always blank

Check in this order:
1. Does the evidence exist on the page in the extracted sections?
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
- `collect_export_columns()` in `run_mushroom_batch.py`
- `write_results_csv()` in `csv_export.py`

## Common Change Scenarios

### Add a new field

Minimum likely changes:
- `amazon_scraper/field_hints.py`
- optionally `amazon_scraper/value_parsers.py`

### Support a changed Amazon layout

Start with:
- `amazon_scraper/html_extractors.py`

Only touch retrieval or prompting after confirming extraction is still correct.

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
- No schema files are wired into the runtime despite the `schemas/` directory.
- No retry/backoff exists for OpenAI HTTP failures beyond surfacing the exception.
- No parallel job execution exists.

## Minimal Mental Model

Keep this model in mind:

- Selenium gets the rendered page.
- BeautifulSoup extracts stable text regions.
- Chunking turns text into evidence records.
- Embeddings choose which evidence matters for each field.
- The chat model produces a structured answer from that evidence.
- CSV export serializes the result.

That is the whole system.
