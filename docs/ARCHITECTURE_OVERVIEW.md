# Architecture Overview

## Purpose

This project scrapes Amazon product detail pages and extracts only the fields requested for each ASIN.

The current architecture is:

1. Load one PDP with Selenium.
2. Extract both HTML text evidence and gallery image metadata.
3. Try image-first resolution for image-friendly fields.
4. Fall back to HTML retrieval and chat extraction for anything still unresolved.
5. Export one CSV row per ASIN with value, source, confidence, and evidence.

The system is not a generic crawler. It is a field-extraction pipeline built around `asin + requested_fields` or `asin + product_type`.

## High-Level Flow

```mermaid
flowchart TD
    A[CLI CSV or Streamlit UI] --> B[Normalized ScrapeJob list]
    B --> C[AmazonScraper.run_batch]
    C --> D[Shared Chrome WebDriver]
    D --> E[Prepare Amazon session]
    E --> F[Load PDP /dp/{asin}]
    F --> G[Extract rendered HTML]
    G --> H[HTML extractors]
    G --> I[Gallery image parser]
    I --> J[VisionFieldResolver]
    H --> K[Chunk builder]
    K --> L[DenseRetriever]
    L --> M[OpenAIFieldExtractor]
    J --> N[Image-first FieldResult map]
    M --> O[HTML fallback FieldResult map]
    N --> P[Merge resolved fields]
    O --> P
    P --> Q[ScrapeResult]
    Q --> R[CSV export]
```

## Runtime Entry Points

### CLI

- [run_product_batch.py](../run_product_batch.py:1) is the user-facing CLI command.

Responsibilities:

- parse CLI arguments
- load product-type defaults
- normalize CSV input into `ScrapeJob` records
- build `AmazonScraper`
- run the batch
- write the output CSV

### Streamlit UI

- [ui_app.py](../ui_app.py:1) builds the operator UI
- [ui_run_manager.py](../ui_run_manager.py:1) owns background execution, progress, cancellation, and downloadable output

Responsibilities:

- collect ASINs, fields, and product types interactively
- normalize input using the same backend rules as the CLI
- start background batch runs
- expose progress and partial results
- allow stop requests while preserving completed rows

## Core Runtime Components

### 1. Orchestrator

[amazon_scraper/scraper.py](../amazon_scraper/scraper.py:1) contains the main application service, `AmazonScraper`.

Responsibilities:

- build and manage Chrome WebDriver instances
- prepare a reusable Amazon session
- detect blocked, partial, missing, and healthy PDPs
- capture debug HTML snapshots
- extract HTML text sections
- extract gallery image metadata
- run image-first resolution
- run HTML fallback extraction
- merge results into `ScrapeResult`

Important behavior:

- `run_batch()` reuses one browser session across jobs to reduce startup cost
- cancellation is supported during batch runs
- if the visible browser session is lost, the batch aborts instead of silently continuing

### 2. HTML extraction layer

[amazon_scraper/html_extractors.py](../amazon_scraper/html_extractors.py:1) converts rendered Amazon DOM sections into plain text blocks.

This layer is intentionally simple:

- no deep semantic parsing
- no row-object modeling
- no field-specific reasoning

It just returns visible text for:

- title
- bullets
- description and A+ text
- facts/details text
- variations text

### 3. Gallery image layer

[amazon_scraper/gallery_images.py](../amazon_scraper/gallery_images.py:1) parses Amazon gallery metadata from the rendered PDP HTML.

Responsibilities:

- collect static gallery image records
- ignore videos and video slates
- deduplicate repeated image records
- choose sensible image tiers for classification and extraction

Current tier policy:

- classification: `large`, else best `main`, else other usable fallback
- extraction: `hiRes`, else `large`, else best `main`

### 4. Image-first resolution layer

[amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1) contains `VisionFieldResolver`.

Responsibilities:

- classify gallery images into roles such as `packaging`, `facts_panel`, `marketing_text`, `other`
- select the best packaging candidate
- select up to a few facts-panel candidates
- validate that a supposed facts image is a real Supplement Facts or Nutrition Facts panel
- extract image-backed fields first
- emit `FieldResult` objects with image URL provenance

Current high-value image-backed fields:

- `packaging_type`
- `ingredients`
- `serving_size`
- `servings_per_container`

Other fields can also attempt image extraction, but the strongest and most validated path is for the targeted image-backed fields above.

### 5. Chunking layer

[amazon_scraper/chunking.py](../amazon_scraper/chunking.py:1) converts HTML-derived text sections into `ChunkRecord` instances.

Each chunk includes:

- section
- normalized text
- lightweight source hint
- order
- source priority

This is the bridge between scraping and retrieval.

### 6. Retrieval layer

[amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1) contains `DenseRetriever`.

Responsibilities:

- embed field queries
- embed chunk text
- cache embeddings locally
- score chunk similarity
- rank the best evidence for each requested field

This layer is used for the HTML fallback path, not the image-first path.

### 7. HTML fallback extraction layer

[amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1) contains `OpenAIFieldExtractor`.

Responsibilities:

- build field-specific prompts
- constrain the model to retrieved evidence
- parse JSON responses
- normalize and filter model output
- return `FieldResult`

This layer only runs after image-first resolution has had a chance to satisfy fields from gallery evidence.

### 8. OpenAI client and cost tracking

[amazon_scraper/openai_client.py](../amazon_scraper/openai_client.py:1) is the shared HTTP client for embeddings, chat completions, cost accounting, and vision transport.

Important behavior:

- inline image transport is the default
- selected gallery images are downloaded in this scraper process and sent to OpenAI as base64 image data unless remote URL fetching is explicitly enabled
- budget limits can stop a batch after the call that crosses the configured threshold
- API usage is logged in a structured way for debugging and cost analysis

### 9. Output layer

[amazon_scraper/csv_export.py](../amazon_scraper/csv_export.py:1) writes final results.

Per field, the CSV includes:

- `<field>`
- `<field>_source`
- `<field>_confidence`
- `<field>_evidence`

Fixed columns include:

- `asin`
- `product_type`
- `status`
- `url`
- `scraped_at`
- `title`
- `error`

## Data Contracts

### Input CSV

Required:

- `asin`
- one of `required_fields` or `product_type`

Optional:

- `product_type`
- both `product_type` and `required_fields`, where explicit `required_fields` wins

### Core runtime objects

[amazon_scraper/models.py](../amazon_scraper/models.py:1) defines:

- `ScrapeJob`
- `ChunkRecord`
- `FieldResult`
- `ScrapeResult`

## Source Precedence

The current default source precedence is:

1. image evidence first
2. HTML retrieval and chat extraction second

This does not mean image always wins blindly. Image output still has to be usable enough to accept. If the image path fails or returns weak evidence, the scraper falls back to the HTML path.

## Operational States

The main page and run states are:

- `OK`
- `PARTIAL`
- `BLOCKED`
- `NOT_FOUND`
- `NAVIGATION_ERROR`

At the batch level, the UI run manager also exposes states such as:

- `queued`
- `running`
- `stopping`
- `completed`
- `stopped`
- `error`

## Cross-Cutting Concerns

### Logging

- CLI logs default to terminal plus rotating file output
- UI runs write live scraper logs to `logs/streamlit_scraper.log`
- structured API usage lines use the `api_usage` prefix

### Cancellation

- UI stop requests set a shared cancel event
- the scraper closes the active browser session
- in-flight work may need the current blocking step to return before the run fully exits
- completed rows are preserved

### Caching

- embeddings cache: `.cache/embeddings.json`
- vision cache: `.cache/vision.json`

## Known Constraints

- tuned for Amazon US PDPs
- depends on Selenium plus Chrome availability
- Amazon can still block or challenge automated browsing
- batch execution is sequential
- image extraction quality depends on gallery quality
- HTML fallback quality depends on retrieval surfacing the right evidence

## Recommended Reading Order

If you want the smallest useful reading order:

1. [docs/START_HERE.md](START_HERE.md)
2. [run_product_batch.py](../run_product_batch.py:1)
3. [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:1)
4. [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
5. [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)
6. [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)


