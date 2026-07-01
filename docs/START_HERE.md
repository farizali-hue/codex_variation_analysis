# Start Here

This file exists for one reason: you should not have to understand the whole repo before you can work on it.

## What This Project Really Is

At a high level, the project does one job:

1. Load an Amazon product page.
2. Collect the useful evidence from the page.
3. Try image evidence first when it is likely to be the most reliable source.
4. Fall back to HTML retrieval when image evidence is weak or missing.
5. Export a CSV row with value, source, confidence, and evidence for each requested field.

That is the whole system. Most of the code is just support code around those five steps.

## Read This Before Architectural Work

If you are making or reviewing design-level changes, read these before touching code:

1. [AGENTS.md](../AGENTS.md)
2. [docs/decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md](decisions/ADR-002-engineering-guardrails-for-agent-and-architecture-decisions.md)
3. [docs/decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md](decisions/ADR-003-run-scoped-typed-field-contracts-and-generic-canonicalization.md)

`AGENTS.md` is the repo-level always-on contract for how agents should reason, explain, escalate, and implement in new sessions.

## The 5-Minute Mental Model

There are only four pieces you need to hold in your head at first:

- `run_product_batch.py` or `ui_app.py` starts a batch.
- `amazon_scraper/scraper.py` runs one ASIN end to end.
- `amazon_scraper/vision_extraction.py` handles image-first field resolution.
- `amazon_scraper/extraction.py` plus `amazon_scraper/retrieval.py` handle HTML fallback extraction.

If you understand those four places, the rest of the repo starts to make sense.

## What To Ignore At First

Do not start by reading everything.

You can safely ignore these on your first pass:

- `tests/`
- old plan docs in the repo root
- `value_parsers.py` details beyond knowing it normalizes outputs
- `field_hints.py` details beyond knowing it maps field names and aliases
- `run_product_batch.py` details once you understand that it is the CLI entrypoint

Those files matter later, but they are not the best entrypoint when you are trying to form a mental model.

## Read In This Order

### If you want the fastest orientation

1. [README.md](../README.md)
2. [docs/ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)
3. [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:1)
4. [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
5. [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)
6. [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)

### If you only care about running batches

1. [README.md](../README.md)
2. [ui_app.py](../ui_app.py:1)
3. [ui_run_manager.py](../ui_run_manager.py:1)

### If you only care about extraction accuracy

1. [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:212)
2. [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:100)
3. [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)
4. [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)
5. [amazon_scraper/value_parsers.py](../amazon_scraper/value_parsers.py:1)

## How One ASIN Flows Through The System

1. The CLI or UI builds a `ScrapeJob`.
2. `AmazonScraper.run_job()` opens `/dp/{asin}` and captures the rendered HTML.
3. The scraper extracts plain-text HTML sections and gallery image metadata.
4. If requested fields are image-friendly, `VisionFieldResolver` tries to resolve them from gallery images first.
5. For any unresolved fields, the scraper builds text chunks, ranks them with embeddings, and asks the chat model to extract from the best evidence only.
6. The result is written as one `ScrapeResult`.

## The Most Important Files By Responsibility

- [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:1)
  The orchestrator. If you are confused about overall control flow, start here.
- [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
  The image-first resolver. This is where packaging and facts-panel logic lives.
- [amazon_scraper/gallery_images.py](../amazon_scraper/gallery_images.py:1)
  Parses Amazon gallery metadata and chooses which image tier to use.
- [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)
  The HTML fallback extractor and field prompt builder.
- [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)
  Embedding-based ranking for HTML chunks.
- [amazon_scraper/openai_client.py](../amazon_scraper/openai_client.py:1)
  Shared OpenAI HTTP client, cost tracking, and inline image transport.
- [ui_run_manager.py](../ui_run_manager.py:1)
  UI background execution, progress, stop requests, and downloads.

## If You Want To Change X, Go Here

- Add or adjust a field alias: [amazon_scraper/field_hints.py](../amazon_scraper/field_hints.py:1)
- Normalize a model output differently: [amazon_scraper/value_parsers.py](../amazon_scraper/value_parsers.py:1)
- Improve HTML extraction: [amazon_scraper/html_extractors.py](../amazon_scraper/html_extractors.py:1)
- Improve image selection: [amazon_scraper/gallery_images.py](../amazon_scraper/gallery_images.py:1) and [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1)
- Change UI behavior: [ui_app.py](../ui_app.py:1) and [ui_run_manager.py](../ui_run_manager.py:1)
- Change CSV output: [amazon_scraper/csv_export.py](../amazon_scraper/csv_export.py:1)
- Change default fields by category: [schemas/product_type_fields.json](../schemas/product_type_fields.json:1)

## Debugging By Symptom

- A field is blank but visible in HTML:
  Start with [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1) and [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1).
- A field is blank but visible in an image:
  Start with [amazon_scraper/gallery_images.py](../amazon_scraper/gallery_images.py:1) and [amazon_scraper/vision_extraction.py](../amazon_scraper/vision_extraction.py:1).
- The browser run will not stop:
  Start with [ui_run_manager.py](../ui_run_manager.py:1) and [amazon_scraper/scraper.py](../amazon_scraper/scraper.py:419).
- The UI looks stuck but the batch is still running:
  Check `logs/streamlit_scraper.log` and [ui_app.py](../ui_app.py:312).
- Costs look too high:
  Start with [amazon_scraper/openai_client.py](../amazon_scraper/openai_client.py:1) and filter logs for `api_usage`.

## What I Would Personally Read First In The Code

If I had to onboard again from zero, I would read just these functions before anything else:

1. [AmazonScraper.run_batch](../amazon_scraper/scraper.py:128)
2. [AmazonScraper.run_job](../amazon_scraper/scraper.py:212)
3. [VisionFieldResolver.resolve_fields](../amazon_scraper/vision_extraction.py:117)
4. `DenseRetriever.rank_chunks()` in [amazon_scraper/retrieval.py](../amazon_scraper/retrieval.py:1)
5. `OpenAIFieldExtractor.extract_field()` in [amazon_scraper/extraction.py](../amazon_scraper/extraction.py:1)

That path gives you the system without drowning you in details too early.

## Final Advice

You do not need to understand every helper before you can work productively here.

Start with:

- one entrypoint
- one orchestrator
- one image path
- one HTML path

Then only expand outward when a real bug or feature forces you to.


