# OpenAI-Only RAG Amazon Extraction Plan

## Summary
- Replace the current schema-first design with a retrieval-first pipeline driven by `asin + required_fields`.
- Scrape the full Amazon PDP once, extract visible product content as section-level plain text blocks, embed those blocks as chunk records, retrieve relevant chunks using OpenAI embeddings, then extract final field values using an OpenAI chat model.
- Remove schema files from the main architecture. `product_type` becomes optional metadata only.
- Use only the OpenAI API for semantic retrieval and final extraction; no Ollama, no local inference runtime, and no BM25 fallback.
- Keep classic RAG as the default path, with a lightweight agentic fallback only for weak, conflicting, or low-confidence field retrieval.

## Key Changes

### HTML extraction approach
- Replace the previous key/value parsing and variation dimension detection logic with section-level plain text extraction.
- `html_extractors.py` is responsible only for:
  - detecting page state (blocked, not found, partial, ok)
  - extracting the product title as a single string
  - extracting bullets as a list of plain text strings
  - extracting description and A+ content as a single merged plain text string
  - extracting facts/details sections as a single plain text block per container (no row-level key/value parsing)
  - extracting selected variation text as a plain text block (no dimension label normalization or ARIA walking)
- All semantic interpretation of extracted text — what a label means, what dimension a variation belongs to — is delegated entirely to the retrieval and extraction layers.
- No stopword filtering, no dimension key normalization, no label alias mapping, and no ARIA attribute walking in the extraction layer.
- The public interface of `html_extractors.py` remains stable: `detect_page_state`, `extract_title`, `extract_bullets`, `extract_description_text`, `extract_facts_table`, `extract_variations`. Only their return types simplify: `extract_facts_table` returns a plain text string and `extract_variations` returns a plain text string.

### Input contract
- Required: `asin`, `required_fields`
- Optional: `product_type`
- `required_fields` accepts comma/pipe/semicolon-separated field names per row.

### Content acquisition
- Keep Selenium-based PDP loading on `amazon.com` with New York, USA delivery context.
- Capture title, bullets, facts/details tables, description/A+ text, and selected variation text.
- Normalize each section into plain text only. No structured parsing of rows, labels, or variation dimensions.

### Chunking layer
- `build_chunk_records()` receives plain text per section and converts them into `ChunkRecord` instances.
- Chunk boundaries remain semantic by section block: title, individual bullets, facts/details text block, variation text block, and coherent description/A+ paragraphs split at approximately 500 characters.
- Each chunk retains `section`, `text`, `dom_hint`, `order`, and `source_priority` metadata.
- Section priority order remains:
  1. `variations`
  2. `facts_table`
  3. `bullets`
  4. `description`
  5. `title`

### Retrieval pipeline
- Build dense retrieval over normalized chunks using OpenAI embeddings only.
- No BM25 or keyword retriever.
- Embed both chunk text and requested-field queries with the configured OpenAI embedding model.
- Log token usage after every embedding API call:
  - log the number of tokens consumed as returned by the API response usage field
  - log the embedding model name
  - log a running total of embedding tokens per ASIN and per batch
  - do not compute cost in code; log raw token counts only so cost can be calculated externally against any pricing version
- Build requested-field queries using a hybrid strategy:
  - first use a maintained field-query template map for known fields
  - if no template exists, fall back to dynamic query text derived from the raw field name
  - if dynamic fallback is still too weak, optionally call an OpenAI chat model to generate an expanded retrieval query using synonyms, expected PDP phrasing, and likely section terminology
- Rank chunks primarily by cosine similarity and return top-K evidence chunks per requested field.
- Add light section-aware prioritization: for structured or binary attributes, prefer facts/details and variation text over marketing copy.
- Add a lightweight agentic fallback only for failed, weak, conflicting, or low-confidence fields:
  1. retry retrieval with an expanded field query using synonyms and expected PDP phrasing
  2. if still weak, retry with section-aware targeting (facts/details, title, variations)
  3. if still weak, retry with a broader evidence set (top 8 chunks plus title and details)
  4. only then allow final blank output
- Add local caching for embeddings keyed by normalized chunk/query text to reduce repeated API cost.
- Log retrieval diagnostics for low-confidence or unsupported outputs, including query text, top retrieved chunks, retry path taken, and similarity scores.

### Extraction pipeline
- Log token usage after every chat completions API call:
  - log `prompt_tokens`, `completion_tokens`, and `total_tokens` from the API response usage object
  - log the chat model name
  - log a running total of chat tokens per ASIN and per batch
  - do not compute cost in code; log raw token counts only so cost can be calculated externally against any pricing version
- For each requested field, call an OpenAI chat/completions model with:
  - requested field name
  - product title
  - top retrieved chunks as plain text evidence
  - strict response contract requiring JSON with `value`, `evidence`, `source`, and `confidence`
- Run deterministic post-processing after model output for whitespace cleanup, numeric/unit normalization, yes/no normalization, and blank/null handling.
- Add deterministic post-retrieval resolvers for simple highly structured fields, especially binary yes/no attributes, before final blanking.
- Treat model confidence as a proposed score that must be validated using evidence explicitness, source quality, chunk agreement, and ambiguity or conflict checks.
- Reject low-confidence or unsupported outputs and write blank values only after the retrieval fallback, expanded-query retry, and deterministic resolver paths are exhausted.

### Output contract
- One CSV per run.
- Core columns: `asin`, `product_type`, `status`, `url`, `scraped_at`, `title`, `error`.
- For each requested field: `<field>`, `<field>_source`, `<field>_confidence`, `<field>_evidence`.

### OpenAI API usage logging
- Every call to the OpenAI API — both embedding and chat/completions — must emit a structured log entry at DEBUG level immediately after the response is received.
- For embedding calls, log:
  - `call_type: embedding`
  - `model`: the embedding model name from config
  - `input_count`: number of strings submitted in the batch
  - `total_tokens`: token count from the API response usage field
  - `cache_hits`: number of inputs that were served from local cache and did not reach the API
- For chat/completions calls, log:
  - `call_type: chat`
  - `model`: the chat model name from config
  - `asin`: the ASIN being processed
  - `field`: the requested field name
  - `prompt_tokens`: token count from the API response usage field
  - `completion_tokens`: token count from the API response usage field
  - `total_tokens`: sum of prompt and completion tokens
  - `retry_attempt`: 0 for first pass, 1+ for agentic fallback retries
- At the end of each ASIN job, emit a single INFO-level usage summary containing:
  - `asin`
  - `embedding_calls`: total embedding API calls made (excluding cache hits)
  - `embedding_tokens`: total tokens consumed across all embedding calls
  - `chat_calls`: total chat API calls made
  - `chat_prompt_tokens`: total prompt tokens across all chat calls
  - `chat_completion_tokens`: total completion tokens across all chat calls
  - `chat_total_tokens`: total tokens across all chat calls
- At the end of the full batch run, emit a single INFO-level batch usage summary containing the same fields summed across all ASINs, plus `asin_count` and `fields_requested`.
- All usage log entries must use a consistent key prefix such as `api_usage` so they can be filtered and aggregated from log files without parsing prose.
- Do not estimate cost in code. Log raw token counts only so cost can be computed externally against any pricing version.

### Deprecations
- Remove schema YAML files from the planned extraction path.
- Remove all key/value row parsing logic from `html_extractors.py`.
- Remove all variation dimension normalization, stopword filtering, ARIA attribute walking, and `_canonicalize_dimension_key` logic from `html_extractors.py`.
- Existing deterministic parsers in `value_parsers.py` remain only as post-processing helpers, not as the primary extraction strategy.

## Public Interfaces
- `ScrapeJob` carries `asin`, optional `product_type`, and `requested_fields`.
- `html_extractors` exposes section-level plain text only. No structured objects from facts or variations.
- Document-store layer returns normalized chunk records for one PDP.
- OpenAI embedding client handles chunk/query embedding and similarity scoring.
- OpenAI extraction client handles structured field extraction from retrieved evidence.
- Field-query strategy layer supports:
  - maintained query templates
  - dynamic fallback from raw field names
  - optional OpenAI-generated expanded retrieval queries when template coverage is missing or first-pass retrieval is weak
- Retrieval diagnostics output for debugging low-confidence or blank results, including whether lightweight agentic fallback was triggered.
- Configuration through environment variables:
  - `OPENAI_API_KEY`
  - `OPENAI_EMBEDDING_MODEL`
  - `OPENAI_CHAT_MODEL`
- Fail fast at startup if any required OpenAI configuration is missing.

## Test Plan

### Unit tests
- API usage log entries are emitted for every embedding and chat call with correct field names
- per-ASIN usage summary aggregates correctly across multiple fields and retry attempts
- batch usage summary aggregates correctly across multiple ASINs
- cache hits are excluded from embedding call counts
- plain text extraction from title, bullets, facts/details block, description paragraphs, and variation block
- dense retrieval ranking from mocked embeddings
- field-query template mapping with dynamic fallback
- OpenAI-generated expanded query fallback behavior when template coverage is missing or retrieval is weak
- embedding-cache keying and reuse
- extraction-response parsing and post-processing
- deterministic resolver behavior for simple yes/no fields
- low-confidence fallback handling before final blanking
- lightweight agentic retry ordering: expanded query → section-aware retry → broader evidence

### Fixture tests
- one PDP where the answer is in the facts/details text block
- one PDP where the answer is only in title/bullets
- one PDP where the requested field appears across multiple section text blocks
- one PDP where the requested field is absent
- one PDP where the raw field name is weak but expanded query retrieval succeeds

### Acceptance scenarios
- input with only `asin + required_fields` runs successfully without schema files
- `ingredients` resolves from retrieved facts/details evidence
- `packaging_type` resolves from retrieved facts/details or image evidence
- repeated field mentions across sections do not cause incorrect extraction when one source is more authoritative
- structured fields such as `servings_per_container` normalize into stable numeric outputs when the answer is present
- missing field-query templates do not block retrieval because dynamic query fallback is available
- weak first-pass retrieval can be improved by expanded field-query retry before broader evidence fallback
- repeated runs reuse cached embeddings for unchanged content
- unrelated high-similarity text does not get written as the field value
- low-confidence first-pass retrieval triggers lightweight agentic fallback before export
- low-confidence outputs are exported as blank with evidence/confidence metadata preserved only after fallback and deterministic resolution fail
- retrieval diagnostics make blank outputs explainable and debuggable

## Assumptions
- OpenAI API access is available and acceptable for both embeddings and extraction.
- No local LLM runtime, Ollama integration, BM25 retriever, or schema-driven fallback is in scope.
- Retrieval is dense-only and per-run, with local embedding cache persisted between runs.
- Lightweight retry logic for failed fields is in scope, including expanded-query retry and optional OpenAI-generated retrieval-query fallback, but full agentic RAG is not the default architecture.
- The system is designed for dynamic requested fields, but extraction quality still depends on the requested field being inferable from PDP evidence.
- All semantic interpretation of product attributes is handled by the retrieval and extraction layers, not by the HTML extraction layer.
