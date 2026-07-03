# Developer Guide

This guide is for reading the repository without getting buried in details too early.

## First: The Repo Is Smaller Than It Looks

The repo has a lot of supporting files, but the real runtime centers on a small set of modules:

- CLI or UI entrypoint
- scraper orchestrator
- image-first resolver
- HTML fallback extractor
- retrieval
- export

Everything else either feeds those modules or tests them.

## Repository Layout

```text
.
|-- amazon_scraper/
|   |-- chunking.py
|   |-- csv_export.py
|   |-- extraction.py
|   |-- field_hints.py
|   |-- gallery_images.py
|   |-- html_extractors.py
|   |-- input_normalization.py
|   |-- logging_utils.py
|   |-- models.py
|   |-- openai_client.py
|   |-- product_type_schemas.py
|   |-- retrieval.py
|   |-- scraper.py
|   |-- value_parsers.py
|   `-- vision_extraction.py
|-- docs/
|   |-- ARCHITECTURE_OVERVIEW.md
|   |-- CODEBASE_GUIDE.md
|   |-- DEVELOPER_GUIDE.md
|   |-- START_HERE.md
|   |-- SYSTEM_ARCHITECTURE.md
|   |-- UI_BATCH_SPEC.md
|   `-- decisions/
|       `-- ADR-001-image-first-html-fallback-extraction.md
|-- inputs/
|-- logs/
|-- outputs/
|-- schemas/
|   `-- product_type_fields.json
|-- tests/
|-- run_product_batch.py
|-- ui_app.py
`-- ui_run_manager.py
```

## Best Reading Order

### Layer 1: Learn The Runtime Shape

1. [README.md](../README.md)
2. [docs/START_HERE.md](START_HERE.md)
3. [docs/ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)

### Layer 2: Learn The Main Control Flow

1. [run_product_batch.py](../run_product_batch.py:1)
2. [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:1)

### Layer 3: Learn The Two Extraction Paths

1. [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
2. [amazon_scraper/gallery_images.py](../amazon_scraper/gallery_images.py:1)
3. [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)
4. [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)

### Layer 4: Learn Supporting Pieces Only When Needed

- [amazon_scraper/input_normalization.py](../amazon_scraper/input_normalization.py:1)
- [amazon_scraper/value_parsers.py](../amazon_scraper/value_parsers.py:1)
- [amazon_scraper/field_hints.py](../amazon_scraper/field_hints.py:1)
- [amazon_scraper/openai_client.py](../amazon_scraper/openai_client.py:1)
- [amazon_scraper/csv_export.py](../amazon_scraper/csv_export.py:1)

## The Main Modules

### `run_product_batch.py`

This is the CLI implementation.

Read this when you want to understand:

- how CSV rows become `ScrapeJob` objects
- how product-type defaults are applied
- how the scraper is constructed
- how output columns are chosen

### `amazon_scraper/scraper.py`

This is the heart of the system.

If you understand this file, you understand the repo.

Its responsibilities are:

- browser and session management
- page loading
- state detection
- HTML extraction
- gallery extraction
- image-first field resolution
- HTML fallback field resolution
- result assembly
- cancellation handling

### `amazon_scraper/vision_extraction.py`

This is the image-first resolver.

Read this when you want to understand:

- how packaging images are chosen
- how facts-panel images are chosen
- how generic image-backed fields are attempted
- what validation rules exist before image output is accepted

### `amazon_scraper/gallery_images.py`

This file decides what gallery images exist and which URLs are worth using.

If the wrong image gets picked, or a useful image is never considered, this is one of the first files to inspect.

### `amazon_scraper/extraction.py`

This is the HTML fallback extractor.

Read this when:

- retrieval found the right HTML evidence but output is still wrong
- field prompting needs to be stricter or looser
- confidence handling needs adjustment

### `amazon_scraper/retrieval.py`

This is the ranking layer for HTML chunks.

Read this when:

- HTML evidence exists but the model is not seeing the right chunk
- embeddings cost looks too high
- cache behavior looks wrong

### `amazon_scraper/openai_client.py`

This is the shared OpenAI transport layer.

Read this when:

- cost tracking looks wrong
- requests are not cancelling correctly
- image transport behavior needs to change
- inline image handling vs remote URL handling matters

### `ui_app.py` and `ui_run_manager.py`

Treat these as one UI subsystem.

Read them when:

- the UI builds incorrect jobs
- the stop button does not behave correctly
- you need to understand progress updates or download output

## What To Ignore Until You Need It

- most tests on your first pass
- legacy planning docs
- parser edge cases in `value_parsers.py`
- field alias details in `field_hints.py`
- low-level logging setup in `logging_utils.py`

Those files are useful, but they are not the right place to start if the repo already feels too big.

## Typical Debugging Paths

### A field is missing but visible in the listing image

Check in this order:

1. [amazon_scraper/gallery_images.py](../amazon_scraper/gallery_images.py:1)
2. [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
3. [amazon_scraper/openai_client.py](../amazon_scraper/openai_client.py:1)

### A field is missing but visible in HTML text

Check in this order:

1. [amazon_scraper/html_extractors.py](../amazon_scraper/html_extractors.py:1)
2. [amazon_scraper/chunking.py](../amazon_scraper/chunking.py:1)
3. [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)
4. [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)

### The UI run will not stop

Check in this order:

1. [ui_run_manager.py](../ui_run_manager.py:1)
2. [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:419)
3. [amazon_scraper/openai_client.py](../amazon_scraper/openai_client.py:1)

### Costs look high

Check in this order:

1. log lines with `api_usage`
2. [amazon_scraper/openai_client.py](../amazon_scraper/openai_client.py:1)
3. [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)
4. [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)

## Common Change Scenarios

### Add a new field alias or user-friendly field name

Start with:

- [amazon_scraper/input_normalization.py](../amazon_scraper/input_normalization.py:1)
- [amazon_scraper/field_hints.py](../amazon_scraper/field_hints.py:1)

### Add a new default field set for a product type

Start with:

- [schemas/product_type_fields.json](../schemas/product_type_fields.json:1)
- [amazon_scraper/product_type_schemas.py](../amazon_scraper/product_type_schemas.py:1) only if normalization rules need to change

### Improve supplement facts extraction

Start with:

- [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
- [amazon_scraper/gallery_images.py](../amazon_scraper/gallery_images.py:1)

### Improve plain HTML field extraction

Start with:

- [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)
- [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)

## Tests

There is an automated test suite in the repository.

Useful test files include:

- [tests/test_vision_extraction.py](../tests/test_vision_extraction.py:1)
- [tests/test_scraper_image_first.py](../tests/test_scraper_image_first.py:1)
- [tests/test_openai_client.py](../tests/test_openai_client.py:1)
- [tests/test_input_normalization.py](../tests/test_input_normalization.py:1)
- [tests/test_html_extractors.py](../tests/test_html_extractors.py:1)

## Minimal Mental Model

Keep this model in mind:

- Selenium gets the rendered page.
- HTML extractors turn page sections into text.
- Gallery parsing turns image metadata into candidate image records.
- Vision tries image-first extraction where useful.
- Retrieval plus chat extraction handles the HTML fallback path.
- CSV export serializes the final result.

If you can explain those six bullets back to yourself, you have enough context to work productively in the repo.


