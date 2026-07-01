# Automatic Gallery Image Selection for Packaging and Supplement Facts

## Summary
- Do not rely on image order at all.
- For each ASIN, automatically collect all static product-gallery images from Amazon’s embedded gallery metadata, then classify every image by content.
- Independently select the best `packaging` image and the best `supplement_facts` image, then extract:
  - `packaging_type` as `enum + raw evidence`
  - `ingredients`, `serving_size`, `servings_per_container`
- Persist audit data through the existing result shape: selected image URL as `source`, model confidence as `*_confidence`, and raw evidence/full panel text as `*_evidence`.

## Key Changes
- Add a gallery-image discovery layer in the scraper that reads Amazon’s embedded image metadata first and falls back to visible main-image DOM metadata if needed.
- Restrict candidates to static product images only.
  - Ignore videos and video thumbnails.
  - Dedupe by Amazon physical image id / hi-res URL.
  - Use the currently loaded PDP state only, so the gallery reflects the selected variant already on the page.
- Add an image-classification stage that runs on every candidate image at moderate resolution and returns structured JSON:
  - `image_role`: `packaging`, `supplement_facts`, `both`, `other`
  - `packaging_type_enum`
  - `packaging_type_raw`
  - readability / visibility signals
  - confidence
- Selection rules:
  - Packaging: highest-confidence clear pack shot wins; tie-break by package visibility, then earlier slot.
  - Supplement facts: highest-confidence readable panel wins; tie-break by text/table readability, then earlier slot.
  - Low-confidence behavior: keep the best guess and propagate low confidence rather than blanking it.
- Add a supplement-panel transcription stage on the selected supplement image using the hi-res image.
- Return structured `ingredients`, `serving_size`, and `servings_per_container`.
  - Normalize the extracted values deterministically after transcription.
- Field precedence:
  - `packaging_type`: image-first only.
  - `ingredients`, `serving_size`, `servings_per_container`: image-first, then fall back to the current HTML/retrieval pipeline if no usable supplement image is found.
- Add local caching for image analysis keyed by `physical_id + task + model` so reruns do not re-analyze the same Amazon image.

## Public Interfaces / Types
- Extend config with optional `OPENAI_VISION_MODEL`.
  - Default to `OPENAI_CHAT_MODEL` if that model is vision-capable.
- Add internal image metadata type with:
  - `slot_index`, `variant`, `thumb_url`, `large_url`, `hi_res_url`, `physical_id`, `media_type`
- Add image-backed fields:
- `packaging_type`
- `ingredients`
- `servings_per_container`
- Keep `serving_size` as the existing canonical field.
- Normalize packaging to this enum:
  - `pouch`, `jar`, `bottle`, `canister`, `box`, `stick_pack`, `sachet`, `tub`, `other`

## Test Plan
- HTML fixture test: unordered gallery metadata still yields the full static image set.
- Selection test: packaging and supplement images are chosen correctly even when they appear in different slots or beyond the initially visible thumbnails.
- Exclusion test: videos and duplicate image records are ignored.
- Packaging normalization test: raw outputs like “stick packs” and “resealable pouch” map to the right enum while preserving raw evidence.
- Supplement transcription test: a facts-panel image produces `ingredients`, `serving_size`, and `servings_per_container`.
- Low-confidence test: best candidate is still returned with low confidence and evidence.
- Fallback test: when no supplement image is usable, overlapping fields still come from the existing HTML extraction path.
- Export test: image-backed fields write URL/confidence/evidence correctly without removing current CSV columns.

## Assumptions / Defaults
- The feature runs only when at least one image-backed field is requested.
- Existing product-type defaults stay unchanged in v1; schema opt-in can be added later.
- The analyzed gallery is the selected-variant gallery already loaded on the PDP.
- Static product images are in scope; user videos are out of scope.
