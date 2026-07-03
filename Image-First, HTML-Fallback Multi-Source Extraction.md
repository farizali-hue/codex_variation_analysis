# Plan: Image-First, HTML-Fallback Multi-Source Extraction

## Summary
- Make `source precedence` a core system rule: for every requested field, search image evidence first, then fall back to HTML/text only if image evidence is missing, unreadable, or low confidence.
- Keep the user interface source-agnostic. The user asks for a detail once; the program decides whether that detail comes from gallery images or HTML.
- Build this as a routed extraction system, not a one-off supplement feature. Each field request should flow through the same stages: discover sources, rank image evidence first, extract, validate, then optionally fall back to HTML.

## Best Approach
- Add a `field resolution policy` layer before extraction:
  - default policy: `image_first_then_html`
  - exceptions only when a field is clearly not image-friendly
- Add an image evidence pipeline:
  - discover all static gallery images from Amazon’s embedded gallery JSON
  - classify each image by role such as `packaging`, `facts_panel`, `marketing_claims`, `other`
  - use `large` or largest `main` for candidate classification
  - use `hiRes`, else `large`, else largest `main` for final extraction
- Add field-level routing:
  - for each requested field, determine which image role is most likely to contain it
  - try image extraction first from the best candidate
  - accept image result only if confidence and evidence quality pass validation
  - otherwise run the existing HTML retrieval/extraction path
- Add source-aware outputs:
  - always return value, source URL or section, confidence, and evidence
  - make it obvious whether the winning evidence came from image or HTML

## Why This Is Best
- Pros:
  - Matches your product goal exactly: the user does not need to know where the detail exists.
  - Uses packaging panels, facts panels, and on-pack text first, which are often more trustworthy than seller-written HTML bullets.
  - Keeps the existing HTML pipeline valuable as fallback instead of replacing it.
  - Extends naturally from a few targeted image-backed fields to a broader set later.
- Cons:
  - Higher cost than HTML-only extraction because every ASIN now pays for some image analysis.
  - More moving parts: image discovery, classification, routing, confidence gating, fallback.
  - Some details are still hard in images when text is tiny, blurry, cropped, or absent.
  - “Image is always most reliable” is directionally good, but not universally true; some marketing images can be less precise than a product facts table in HTML. The router must still validate rather than blindly trust image outputs.

## Version Distribution
- V1:
  - Introduce source precedence `image_first_then_html`
  - Support image-first extraction for high-value fields only:
    - `packaging_type`
    - `ingredients`
    - `serving_size`
    - `servings_per_container`
  - Use HTML fallback for overlapping fields when image evidence is weak
  - Keep arbitrary HTML field extraction as it works today
- V1.5:
  - Expand image roles beyond facts panels:
    - claim badges
    - count/quantity panels
    - front-label descriptors
  - Add field-to-image-role mapping so more requested fields can attempt image-first automatically
  - Add better validation rules so image results must contain expected labels, units, or row structure before acceptance
- V2:
  - Generalize to true `multi-source field resolution`
  - Every requested field follows the same routing policy:
    - image-first
    - HTML fallback
    - optional merged evidence when both agree
  - Add a confidence-based escalation path for hard images, such as upgrading only the final facts-panel read to a stronger model when mini is insufficient
  - Add a field capability registry so the system can say whether a field is image-friendly, HTML-friendly, or both

## Public Interfaces / Behavior
- Keep user input simple: users continue asking for fields by name only.
- Add an internal `FieldResolutionPolicy` with default `image_first_then_html`.
- Add an internal `FieldCapabilityRegistry`:
  - expected image role
  - validation rules
  - fallback eligibility
- Add provenance values that clearly distinguish:
  - image source: selected image URL
  - HTML source: section or DOM hint

## Test Plan
- For a requested field present in both image and HTML, image evidence wins when valid.
- For a requested field missing or unreadable in image but present in HTML, HTML fallback wins.
- For a requested field unsupported by current image routing, HTML extraction still works.
- For supplement listings with unordered galleries, the right facts image is found without using slot position.
- For low-quality facts panels, image result is rejected and fallback behavior is deterministic.

## Assumptions and Defaults
- Default system policy should be `image first, HTML fallback`.
- V1 should stay OpenAI-only and avoid separate OCR.
- The first implementation should be targeted rather than “extract any possible image detail,” because generic image-anything extraction is much riskier and harder to validate.
- Over time, the system should evolve toward source-agnostic field extraction, but only after field routing and validation are reliable.
