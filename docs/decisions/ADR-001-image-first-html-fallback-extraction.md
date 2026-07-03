# ADR-001: Use Image-First, HTML-Fallback Field Resolution

## Status

Accepted

## Date

2026-04-16

## Context

The scraper needs to extract fields from Amazon PDPs where the most reliable source is not always HTML text.

In practice:

- packaging details may only be obvious from gallery images
- supplement facts are often visible in images even when HTML text is incomplete
- users should not need to know whether a field lives in HTML or in an image before requesting it

The earlier architecture was mostly retrieval-first over HTML text. That was simpler, but it missed fields that were clearly visible in product gallery images.

We also needed a design that:

- preserved provenance
- allowed HTML fallback when image evidence was weak
- stayed OpenAI-only for v1
- avoided manual image downloads per ASIN

## Decision

Adopt an image-first, HTML-fallback resolution strategy.

The system will:

1. load the rendered Amazon PDP
2. extract both HTML text evidence and gallery image metadata
3. classify and evaluate gallery images first for image-friendly fields
4. accept image results only when evidence is usable enough
5. fall back to HTML retrieval and chat extraction for unresolved fields

For the current implementation:

- packaging and supplement-facts-related fields are the strongest image-backed paths
- selected images are downloaded by this scraper process and sent inline to OpenAI by default
- HTML retrieval remains the second source of truth rather than being removed

## Alternatives Considered

### HTML-only retrieval-first extraction

Pros:

- simpler runtime
- lower cost
- easier to reason about

Cons:

- misses fields that only appear in images
- performs poorly on packaging type and supplement-facts-only evidence

Rejected because it did not meet the product goal.

### Image-only extraction

Pros:

- simple source precedence
- aligns with the idea that images are often more trustworthy

Cons:

- not all useful details are present or readable in images
- throws away strong HTML evidence
- increases cost unnecessarily

Rejected because HTML remains valuable as fallback.

### Dedicated OCR plus separate image pipeline in v1

Pros:

- potentially better on tiny text
- clearer separation between image retrieval and transcription

Cons:

- more moving parts
- more vendor complexity
- higher maintenance burden for v1

Rejected for v1 in favor of an OpenAI-only implementation.

## Consequences

Positive:

- users request fields without having to know the source format
- packaging and facts-panel extraction become much more reliable
- HTML retrieval remains useful and cost-effective for fallback
- provenance becomes clearer because image URLs can be recorded directly in field sources

Negative:

- the system is more complex than a plain HTML retrieval pipeline
- image classification and validation add extra cost
- image quality can still limit extraction accuracy
- the resolver needs ongoing validation logic to avoid false positives

## Follow-On Notes

- This ADR explains the high-level source precedence decision.
- Detailed image selection rules and validation logic live in code and supporting docs.
- Future ADRs may split out vision transport, escalation strategy, or broader multimodal retrieval if those become stable architectural choices.
