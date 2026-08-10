"""Image-first field resolution for Amazon gallery images.

This module classifies gallery images, selects the best packaging and
facts-panel candidates, and extracts image-backed fields before the scraper
falls back to HTML retrieval.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .contract_value_normalization import build_output_type_instruction
from .field_hints import get_field_hint
from .gallery_images import GalleryImageRecord, select_classification_image_url, select_extraction_image_url
from .models import (
    FieldResult,
    RunFieldContract,
    SupplementFactsDetail,
    SupplementFactsIngredient,
    SupplementFactsMetadataEntry,
)
from .supplement_facts_contract import canonical_supplement_metadata_label
from .value_parsers import normalize_amount_to_mg, normalize_whitespace, parse_value

LOGGER = logging.getLogger(__name__)

_CLASSIFICATION_SYSTEM_PROMPT = (
    "You classify Amazon PDP gallery images. "
    "Return JSON with key images containing one item per supplied image. "
    "For each item include slot_index, physical_id, roles, packaging_type_enum, "
    "packaging_type_raw, readability, visible_text_summary, confidence. "
    "roles must be a list chosen from packaging, facts_panel, marketing_text, other. "
    "packaging_type_enum must be one of pouch, jar, bottle, canister, box, stick_pack, sachet, tub, other. "
    "Use facts_panel only for a true Supplement Facts or Nutrition Facts style panel. "
    "Do not label directions, warnings, barcodes, manufacturer blocks, certifications, or free-of callouts as facts_panel."
)
_FACTS_SYSTEM_PROMPT = (
    "You extract supplement facts from one product image. "
    "Use only visible text in the image. Return JSON with keys ingredients, serving_size, "
    "servings_per_container, evidence, confidence. "
    "If the image does not show a true Supplement Facts or Nutrition Facts panel, return empty strings and confidence 0."
)
_FACTS_DETAIL_SYSTEM_PROMPT = (
    "You extract structured Supplement Facts or Nutrition Facts data from one product image. "
    "Use only visible text in the image. Return JSON with keys panel_detected, metadata, ingredients, "
    "other_ingredients, unparsed_fragments, evidence, confidence. "
    "metadata must contain only serving size and servings per container entries, as objects with keys label and raw_value_text. "
    "ingredients must be a list of objects with keys name, raw_amount_text, daily_value_text, footnote_markers, category, parent_blend_name. "
    "category must be one of nutrient, ingredient, proprietary_blend, blend_sub_ingredient. "
    "Preserve ingredient names as printed aside from trivial whitespace cleanup, but do not include amounts or daily values in the name. "
    "For text like 'Includes 2g Added Sugars', use name 'Added Sugars' and raw_amount_text '2 g'. "
    "If a blend sub-ingredient has no stated amount, leave raw_amount_text empty. "
    "If the image does not show a true Supplement Facts or Nutrition Facts panel, return panel_detected false, "
    "empty lists/strings, and confidence 0."
)
_GENERIC_SYSTEM_PROMPT = (
    "You extract one product field from one or more product images. "
    "Use only visible text or directly visible packaging details in the images. "
    "Return a value only when the images directly support the requested field itself. "
    "Do not infer a field from brand names, product names, ingredient names, dosage text, or general marketing copy. "
    "A front-label product identity term or hero ingredient phrase is not enough unless the image explicitly presents it as the requested field. "
    "If the field is not directly supported, return an empty value. "
    "Return JSON with keys value, evidence, source_candidate_index, confidence. "
    "source_candidate_index must be the 0-based index of the image that directly supports the value."
)
_FACTS_FIELDS = {
    "ingredients",
    "serving_size",
    "servings_per_container",
}
_PACKAGING_TYPES = {"pouch", "jar", "bottle", "canister", "box", "stick_pack", "sachet", "tub", "other"}
_READABILITY_SCORES = {"high": 3, "medium": 2, "low": 1}
_FACTS_HEADER_MARKERS = ("supplement facts", "nutrition facts")
_FACTS_STRUCTURE_MARKERS = (
    "serving size",
    "servings per container",
    "amount per serving",
    "% daily value",
    "daily value",
    "calories",
)
_GALLERY_CLASSIFICATION_TASK = "gallery_classification_v2"
_FACTS_PANEL_TASK = "facts_panel_v2"
_FACTS_PANEL_DETAIL_TASK = "facts_panel_detail_v1"
_GENERIC_FIELD_TASK = "generic_field_v3"
_SUPPLEMENT_NOTE_METADATA_KEYS = {
    "daily value note",
    "daily value notes",
    "dv note",
    "dv notes",
}
_SUPPLEMENT_INGREDIENT_CATEGORIES = {"ingredient", "nutrient", "proprietary_blend", "blend_sub_ingredient"}
_AMOUNT_NUMBER_PATTERN = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_SUPPLEMENT_AMOUNT_UNIT_PATTERN = (
    r"mgs?|mcgs?|milligrams?|micrograms?|kgs?|kilograms?|grams?|g|lbs?|pounds?|oz|ounces?"
)
_INCLUDES_AMOUNT_NAME_PATTERN = re.compile(
    rf"^includes\s+(?P<amount>{_AMOUNT_NUMBER_PATTERN})\s*(?P<unit>{_SUPPLEMENT_AMOUNT_UNIT_PATTERN})\s+(?P<name>.+)$",
    re.IGNORECASE,
)
_TRAILING_AMOUNT_NAME_PATTERN = re.compile(
    rf"^(?P<name>.+?)\s+(?P<amount>{_AMOUNT_NUMBER_PATTERN})\s*(?P<unit>{_SUPPLEMENT_AMOUNT_UNIT_PATTERN})\b(?P<tail>.*)$",
    re.IGNORECASE,
)
_UNITLESS_NUMERIC_AMOUNT_PATTERN = re.compile(rf"^{_AMOUNT_NUMBER_PATTERN}$")
# Daily values can exceed 999% (e.g. B12 at 1,042%), so accept grouped thousands.
_DAILY_VALUE_PATTERN = re.compile(rf"(?P<value><\s*1\s*%|{_AMOUNT_NUMBER_PATTERN}\s*%)")
_FOOTNOTE_MARKER_PATTERN = re.compile(r"^[*+\u2020\u2021]+$")


@dataclass(frozen=True)
class ImageAnalysisResult:
    slot_index: int
    physical_id: str
    roles: tuple[str, ...]
    packaging_type_enum: str
    packaging_type_raw: str
    readability: str
    visible_text_summary: str
    confidence: float


@dataclass(frozen=True)
class VisionResolutionResult:
    fields: dict[str, FieldResult]
    supplement_facts_detail: SupplementFactsDetail | None = None


class VisionCache:
    def __init__(self, cache_file: Path | str) -> None:
        self.cache_file = Path(cache_file)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache = self._load()

    def get(self, model: str, task: str, fingerprint: str, context: str = "") -> dict | None:
        return self._cache.get(self._key(model, task, fingerprint, context))

    def set(self, model: str, task: str, fingerprint: str, value: dict, context: str = "") -> None:
        self._cache[self._key(model, task, fingerprint, context)] = value

    def save(self) -> None:
        self.cache_file.write_text(json.dumps(self._cache), encoding="utf-8")

    def _load(self) -> dict[str, dict]:
        if not self.cache_file.exists():
            return {}
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _key(self, model: str, task: str, fingerprint: str, context: str) -> str:
        digest = hashlib.sha256(f"{model}\n{task}\n{fingerprint}\n{context}".encode("utf-8")).hexdigest()
        return digest


class VisionFieldResolver:
    def __init__(
        self,
        client,
        model: str,
        cache_file: Path | str = ".cache/vision.json",
        classification_confidence_threshold: float = 0.55,
        generic_confidence_threshold: float = 0.65,
        max_facts_candidates: int = 3,
    ) -> None:
        self.client = client
        self.model = model
        self.cache = VisionCache(cache_file)
        self.classification_confidence_threshold = classification_confidence_threshold
        self.generic_confidence_threshold = generic_confidence_threshold
        self.max_facts_candidates = max(1, max_facts_candidates)

    def resolve_supplement_facts_detail_from_text(
        self,
        *,
        asin: str,
        facts_text: str,
        requested_fields: Iterable[str] = (),
    ) -> SupplementFactsDetail | None:
        """HTML fallback for the Supplement Facts panel (ADR-001's fallback leg).

        The panel parser was previously reachable only through the image path, so one
        unreachable gallery image dropped the whole panel even when the page HTML
        carried the same table. This runs the same extraction prompt over that text.
        Lives here so it shares the prompt and payload builder with the image path.
        """
        text = (facts_text or "").strip()
        if not text:
            return None

        fingerprint = f"html:{asin}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]}"
        payload = self.cache.get(self.model, _FACTS_PANEL_DETAIL_TASK, fingerprint)
        if payload is None:
            requested_summary = ", ".join(requested_fields) if requested_fields else "none"
            response_text = self.client.create_chat_completion(
                _FACTS_DETAIL_SYSTEM_PROMPT,
                "Extract detailed Supplement Facts from this product page text. "
                f"Requested fields for reuse: {requested_summary}.\n\n"
                f"Supplement Facts text:\n{text[:6000]}",
                asin=asin,
                field="__facts_panel_detail_html__",
                model=self.model,
            )
            payload = _parse_json_response(response_text)
            self.cache.set(self.model, _FACTS_PANEL_DETAIL_TASK, fingerprint, payload)
            self.cache.save()
        if not isinstance(payload, dict):
            return None

        detail = _build_supplement_facts_detail(payload=payload, source="html_facts_table")
        if detail is None or detail.confidence < self.classification_confidence_threshold:
            return None
        return detail

    def resolve_fields(
        self,
        *,
        asin: str,
        requested_fields: Iterable[str],
        images: list[GalleryImageRecord],
        field_contracts: Mapping[str, RunFieldContract] | None = None,
    ) -> dict[str, FieldResult]:
        return self.resolve_job(
            asin=asin,
            requested_fields=requested_fields,
            images=images,
            field_contracts=field_contracts,
            extract_supplement_facts_details=False,
        ).fields

    def resolve_job(
        self,
        *,
        asin: str,
        requested_fields: Iterable[str],
        images: list[GalleryImageRecord],
        field_contracts: Mapping[str, RunFieldContract] | None = None,
        extract_supplement_facts_details: bool = False,
    ) -> VisionResolutionResult:
        normalized_fields = list(dict.fromkeys(field for field in requested_fields if field))
        if not images:
            return VisionResolutionResult(fields={})
        if not normalized_fields and not extract_supplement_facts_details:
            return VisionResolutionResult(fields={})

        if field_contracts:
            LOGGER.info(
                "field_contract_event scope=vision_resolver asin=%s contract_count=%d contract_keys=%s",
                asin,
                len(field_contracts),
                ",".join(sorted(field_contracts)),
            )

        analyses = self._classify_images(asin=asin, images=images)
        if not analyses:
            return VisionResolutionResult(fields={})

        image_by_physical_id = {image.physical_id: image for image in images}
        results: dict[str, FieldResult] = {}
        supplement_facts_detail: SupplementFactsDetail | None = None

        if "packaging_type" in normalized_fields:
            candidate = self._select_packaging_candidate(analyses, image_by_physical_id)
            if candidate is not None:
                image, analysis = candidate
                extraction_url = select_extraction_image_url(image)
                if extraction_url and analysis.packaging_type_enum and analysis.confidence >= self.classification_confidence_threshold:
                    results["packaging_type"] = FieldResult(
                        value=analysis.packaging_type_enum,
                        source=extraction_url,
                        confidence=analysis.confidence,
                        evidence=analysis.packaging_type_raw or analysis.visible_text_summary,
                    )

        requested_fact_fields = [field for field in normalized_fields if field in _FACTS_FIELDS]
        if extract_supplement_facts_details:
            supplement_facts_detail = self._resolve_supplement_facts_detail(
                asin=asin,
                requested_fields=normalized_fields,
                analyses=analyses,
                image_by_physical_id=image_by_physical_id,
            )
            if supplement_facts_detail is not None:
                results.update(
                    _build_field_results_from_supplement_facts_detail(
                        detail=supplement_facts_detail,
                        requested_fields=normalized_fields,
                    )
                )
        if requested_fact_fields:
            unresolved_fact_fields = [field for field in requested_fact_fields if field not in results]
        else:
            unresolved_fact_fields = []
        if unresolved_fact_fields:
            facts_results = self._resolve_facts_fields(
                asin=asin,
                requested_fields=unresolved_fact_fields,
                analyses=analyses,
                image_by_physical_id=image_by_physical_id,
            )
            if facts_results:
                results.update(facts_results)

        for field_name in normalized_fields:
            if field_name in results or field_name in _FACTS_FIELDS or field_name == "packaging_type":
                continue
            generic_result = self._extract_generic_field(
                asin=asin,
                field_name=field_name,
                candidates=self._select_generic_candidates(field_name, analyses, image_by_physical_id),
                field_contract=field_contracts.get(field_name) if field_contracts else None,
            )
            if generic_result is not None:
                results[field_name] = generic_result

        return VisionResolutionResult(
            fields=results,
            supplement_facts_detail=supplement_facts_detail,
        )

    def _classify_images(self, *, asin: str, images: list[GalleryImageRecord]) -> list[ImageAnalysisResult]:
        # The prompt below is positional, so images must be resolved BEFORE the slot
        # lines are written. An unreachable image dropped later would leave more slots
        # than pictures and shift every classification onto the wrong image.
        candidates: list[tuple[GalleryImageRecord, str]] = []
        for image in images:
            classification_url = select_classification_image_url(image)
            if classification_url:
                candidates.append((image, classification_url))
        if not candidates:
            return []

        raw_inputs = [{"url": url, "detail": "low"} for _, url in candidates]
        resolver = getattr(self.client, "prepare_reachable_image_inputs", None)
        if resolver is None:
            prepared = list(enumerate(raw_inputs))
        else:
            prepared = resolver(raw_inputs)
        if len(prepared) != len(candidates):
            LOGGER.warning(
                "Classifying %d of %d gallery images asin=%s; unreachable images dropped from "
                "both the prompt and the request", len(prepared), len(candidates), asin,
            )
        if not prepared:
            return []

        # One prompt line per image actually sent, in the same order — by construction.
        image_inputs = [entry for _, entry in prepared]
        kept = [candidates[index] for index, _ in prepared]
        ordered_images = [image for image, _ in kept]
        prompt_lines = [
            f"- slot_index={image.slot_index}; physical_id={image.physical_id or '[missing]'}; variant={image.variant or '[missing]'}"
            for image, _ in kept
        ]
        fingerprint_parts = [f"{image.physical_id}:{url}" for image, url in kept]

        fingerprint = "|".join(fingerprint_parts)
        cached = self.cache.get(self.model, _GALLERY_CLASSIFICATION_TASK, fingerprint)
        if cached is None:
            response_text = self.client.create_chat_completion(
                _CLASSIFICATION_SYSTEM_PROMPT,
                "Classify these images independently in the same order.\n"
                + "\n".join(prompt_lines),
                asin=asin,
                field="__image_classification__",
                model=self.model,
                image_inputs=image_inputs,
            )
            cached = _parse_json_response(response_text)
            self.cache.set(self.model, _GALLERY_CLASSIFICATION_TASK, fingerprint, cached)
            self.cache.save()

        raw_images = cached.get("images") if isinstance(cached, dict) else None
        if not isinstance(raw_images, list):
            return []

        analyses: list[ImageAnalysisResult] = []
        for item in raw_images:
            if not isinstance(item, dict):
                continue
            slot_index = _coerce_int(item.get("slot_index"), default=-1)
            if slot_index < 0:
                continue
            roles = _normalize_roles(item.get("roles"))
            analyses.append(
                ImageAnalysisResult(
                    slot_index=slot_index,
                    physical_id=normalize_whitespace(str(item.get("physical_id", "") or "")),
                    roles=roles,
                    packaging_type_enum=_normalize_packaging_type(item.get("packaging_type_enum")),
                    packaging_type_raw=normalize_whitespace(str(item.get("packaging_type_raw", "") or "")),
                    readability=_normalize_readability(item.get("readability")),
                    visible_text_summary=normalize_whitespace(str(item.get("visible_text_summary", "") or "")),
                    confidence=_coerce_confidence(item.get("confidence")),
                )
            )
        return analyses

    def _select_packaging_candidate(
        self,
        analyses: list[ImageAnalysisResult],
        image_by_physical_id: dict[str, GalleryImageRecord],
    ) -> tuple[GalleryImageRecord, ImageAnalysisResult] | None:
        ranked: list[tuple[float, int, int, GalleryImageRecord, ImageAnalysisResult]] = []
        for analysis in analyses:
            if "packaging" not in analysis.roles:
                continue
            image = image_by_physical_id.get(analysis.physical_id)
            if image is None or not select_extraction_image_url(image):
                continue
            ranked.append(
                (
                    analysis.confidence,
                    _READABILITY_SCORES.get(analysis.readability, 0),
                    -analysis.slot_index,
                    image,
                    analysis,
                )
            )
        if not ranked:
            return None
        ranked.sort(reverse=True)
        _, _, _, image, analysis = ranked[0]
        return image, analysis

    def _select_facts_candidates(
        self,
        analyses: list[ImageAnalysisResult],
        image_by_physical_id: dict[str, GalleryImageRecord],
    ) -> list[tuple[GalleryImageRecord, ImageAnalysisResult]]:
        ranked: list[tuple[float, int, int, GalleryImageRecord, ImageAnalysisResult]] = []
        for analysis in analyses:
            if "facts_panel" not in analysis.roles:
                continue
            image = image_by_physical_id.get(analysis.physical_id)
            if image is None or not select_extraction_image_url(image):
                continue
            ranked.append(
                (
                    analysis.confidence,
                    _READABILITY_SCORES.get(analysis.readability, 0),
                    -analysis.slot_index,
                    image,
                    analysis,
                )
            )
        ranked.sort(reverse=True)
        return [(image, analysis) for _, _, _, image, analysis in ranked[: self.max_facts_candidates]]

    def _resolve_facts_fields(
        self,
        *,
        asin: str,
        requested_fields: list[str],
        analyses: list[ImageAnalysisResult],
        image_by_physical_id: dict[str, GalleryImageRecord],
    ) -> dict[str, FieldResult]:
        best_results: dict[str, FieldResult] = {}
        best_score: tuple[int, float] = (0, 0.0)
        for image, analysis in self._select_facts_candidates(analyses, image_by_physical_id):
            facts_payload = self._extract_facts_fields(
                asin=asin,
                image=image,
                analysis=analysis,
            )
            if facts_payload is None:
                continue
            candidate_results, payload_confidence = self._build_facts_results(
                image=image,
                requested_fields=requested_fields,
                payload=facts_payload,
            )
            coverage = len(candidate_results)
            if coverage == 0:
                continue
            score = (coverage, payload_confidence)
            if score > best_score:
                best_results = candidate_results
                best_score = score
            if coverage >= len(requested_fields):
                break
        return best_results

    def _resolve_supplement_facts_detail(
        self,
        *,
        asin: str,
        requested_fields: list[str],
        analyses: list[ImageAnalysisResult],
        image_by_physical_id: dict[str, GalleryImageRecord],
    ) -> SupplementFactsDetail | None:
        best_detail: SupplementFactsDetail | None = None
        best_score: tuple[int, float] = (0, 0.0)
        for image, analysis in self._select_facts_candidates(analyses, image_by_physical_id):
            payload = self._extract_facts_detail_payload(
                asin=asin,
                image=image,
                analysis=analysis,
                requested_fields=requested_fields,
            )
            if payload is None:
                continue
            detail = _build_supplement_facts_detail(
                payload=payload,
                source=select_extraction_image_url(image) or "",
            )
            if detail is None:
                continue
            if detail.confidence < self.classification_confidence_threshold:
                continue
            coverage = len(detail.metadata) + len(detail.ingredients) + (1 if detail.other_ingredients else 0)
            score = (coverage, detail.confidence)
            if score > best_score:
                best_detail = detail
                best_score = score
        return best_detail

    def _select_generic_candidates(
        self,
        field_name: str,
        analyses: list[ImageAnalysisResult],
        image_by_physical_id: dict[str, GalleryImageRecord],
        max_candidates: int = 3,
    ) -> list[GalleryImageRecord]:
        ranked: list[tuple[float, int, GalleryImageRecord]] = []
        tokens = _build_field_tokens(field_name)
        preferred_roles = _preferred_generic_roles(field_name)
        for analysis in analyses:
            image = image_by_physical_id.get(analysis.physical_id)
            if image is None or not select_extraction_image_url(image):
                continue
            role_score = max((preferred_roles.get(role, 0.0) for role in analysis.roles), default=0.0)
            if role_score <= 0:
                continue
            token_hits = sum(1 for token in tokens if token and token in analysis.visible_text_summary.lower())
            score = role_score + (0.25 * min(token_hits, 3)) + (0.1 * _READABILITY_SCORES.get(analysis.readability, 0)) + analysis.confidence
            ranked.append((score, -analysis.slot_index, image))

        ranked.sort(reverse=True)
        selected: list[GalleryImageRecord] = []
        seen_physical_ids: set[str] = set()
        for _, _, image in ranked:
            if image.physical_id in seen_physical_ids:
                continue
            seen_physical_ids.add(image.physical_id)
            selected.append(image)
            if len(selected) >= max_candidates:
                break
        return selected

    def _extract_facts_fields(
        self,
        *,
        asin: str,
        image: GalleryImageRecord,
        analysis: ImageAnalysisResult,
    ) -> dict | None:
        extraction_url = select_extraction_image_url(image)
        if not extraction_url:
            return None

        fingerprint = f"{image.physical_id}:{extraction_url}" if image.physical_id else extraction_url
        cached = self.cache.get(self.model, _FACTS_PANEL_TASK, fingerprint)
        if cached is None:
            response_text = self.client.create_chat_completion(
                _FACTS_SYSTEM_PROMPT,
                "Extract supplement facts from this image. "
                f"Classification context: {analysis.visible_text_summary or 'facts panel detected'}.",
                asin=asin,
                field="__facts_panel__",
                model=self.model,
                image_inputs=[{"url": extraction_url, "detail": "high"}],
            )
            cached = _parse_json_response(response_text)
            self.cache.set(self.model, _FACTS_PANEL_TASK, fingerprint, cached)
            self.cache.save()
        return cached if isinstance(cached, dict) else None

    def _extract_facts_detail_payload(
        self,
        *,
        asin: str,
        image: GalleryImageRecord,
        analysis: ImageAnalysisResult,
        requested_fields: list[str],
    ) -> dict | None:
        extraction_url = select_extraction_image_url(image)
        if not extraction_url:
            return None

        fingerprint = f"{image.physical_id}:{extraction_url}" if image.physical_id else extraction_url
        cached = self.cache.get(self.model, _FACTS_PANEL_DETAIL_TASK, fingerprint)
        if cached is None:
            requested_summary = ", ".join(requested_fields) if requested_fields else "none"
            response_text = self.client.create_chat_completion(
                _FACTS_DETAIL_SYSTEM_PROMPT,
                "Extract detailed Supplement Facts from this image. "
                f"Classification context: {analysis.visible_text_summary or 'facts panel detected'}. "
                f"Requested fields for reuse: {requested_summary}.",
                asin=asin,
                field="__facts_panel_detail__",
                model=self.model,
                image_inputs=[{"url": extraction_url, "detail": "high"}],
            )
            cached = _parse_json_response(response_text)
            self.cache.set(self.model, _FACTS_PANEL_DETAIL_TASK, fingerprint, cached)
            self.cache.save()
        return cached if isinstance(cached, dict) else None

    def _build_facts_results(
        self,
        *,
        image: GalleryImageRecord,
        requested_fields: list[str],
        payload: dict,
    ) -> tuple[dict[str, FieldResult], float]:
        confidence = _coerce_confidence(payload.get("confidence"))
        if confidence < self.classification_confidence_threshold:
            return {}, confidence

        extraction_url = select_extraction_image_url(image)
        if not extraction_url:
            return {}, confidence

        if not _is_valid_facts_payload(payload, requested_fields):
            return {}, confidence

        results: dict[str, FieldResult] = {}
        evidence = normalize_whitespace(str(payload.get("evidence", "") or ""))
        for field_name in requested_fields:
            raw_value = normalize_whitespace(str(payload.get(field_name, "") or ""))
            if not raw_value:
                continue
            field_hint = get_field_hint(field_name)
            value = parse_value(field_hint.parser, raw_value) or raw_value
            if not value:
                continue
            results[field_name] = FieldResult(
                value=value,
                source=extraction_url,
                confidence=confidence,
                evidence=evidence or raw_value,
            )
        return results, confidence

    def _extract_generic_field(
        self,
        *,
        asin: str,
        field_name: str,
        candidates: list[GalleryImageRecord],
        field_contract: RunFieldContract | None,
    ) -> FieldResult | None:
        resolved_candidates = [image for image in candidates if select_extraction_image_url(image)]
        if not resolved_candidates:
            return None

        image_inputs = [
            {"url": select_extraction_image_url(image), "detail": "high"}
            for image in resolved_candidates
        ]

        fingerprint = "|".join(
            f"{image.physical_id}:{input_item['url']}" if image.physical_id else input_item["url"]
            for image, input_item in zip(resolved_candidates, image_inputs)
        )
        cached = self.cache.get(self.model, _GENERIC_FIELD_TASK, fingerprint, context=field_name)
        if cached is None:
            field_hint = get_field_hint(field_name)
            labels = list(field_hint.label_aliases)
            requested_label = field_name
            if field_contract is not None and field_contract.requested_label:
                requested_label = field_contract.requested_label
                labels = [field_contract.requested_label, *labels]
            aliases = ", ".join(dict.fromkeys(label for label in labels if label))
            candidate_lines = "\n".join(
                f"- candidate_index={index}; slot_index={image.slot_index}; physical_id={image.physical_id or '[missing]'}"
                for index, image in enumerate(resolved_candidates)
            )
            contract_instruction = (
                build_output_type_instruction(field_contract) + "\n"
                if field_contract is not None
                else ""
            )
            support_instruction = _build_direct_support_instruction(requested_label)
            response_text = self.client.create_chat_completion(
                _GENERIC_SYSTEM_PROMPT,
                "Field name: "
                f"{field_name}\nRequested label: {requested_label}\nRelated labels: {aliases}\n"
                f"{contract_instruction}"
                f"{support_instruction}"
                "Candidate images are provided in the same order as listed below.\n"
                f"{candidate_lines}\n"
                "Return the best directly supported value only.",
                asin=asin,
                field=field_name,
                model=self.model,
                image_inputs=image_inputs,
            )
            cached = _parse_json_response(response_text)
            self.cache.set(self.model, _GENERIC_FIELD_TASK, fingerprint, cached, context=field_name)
            self.cache.save()

        if not isinstance(cached, dict):
            return None
        confidence = _coerce_confidence(cached.get("confidence"))
        if confidence < self.generic_confidence_threshold:
            return None
        raw_value = normalize_whitespace(str(cached.get("value", "") or ""))
        if not raw_value:
            return None

        field_hint = get_field_hint(field_name)
        value = parse_value(field_hint.parser, raw_value) or raw_value
        if not value:
            return None
        source_candidate_index = _coerce_candidate_index(cached.get("source_candidate_index"), len(resolved_candidates))
        source_image = resolved_candidates[source_candidate_index] if source_candidate_index is not None else resolved_candidates[0]
        source = select_extraction_image_url(source_image)
        return FieldResult(
            value=value,
            source=source,
            confidence=confidence,
            evidence=normalize_whitespace(str(cached.get("evidence", "") or "")),
        )


def _parse_json_response(response_text: str) -> dict:
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    data = json.loads(cleaned)
    return data if isinstance(data, dict) else {}


def _normalize_roles(raw_roles: object) -> tuple[str, ...]:
    if isinstance(raw_roles, str):
        raw_values = [raw_roles]
    elif isinstance(raw_roles, list):
        raw_values = [str(item) for item in raw_roles]
    else:
        raw_values = []
    normalized: list[str] = []
    for role in raw_values:
        value = normalize_whitespace(role).lower().replace(" ", "_")
        if value in {"packaging", "facts_panel", "marketing_text", "other"} and value not in normalized:
            normalized.append(value)
    return tuple(normalized or ["other"])


def _normalize_packaging_type(raw_value: object) -> str:
    value = normalize_whitespace(str(raw_value or "")).lower().replace(" ", "_").replace("-", "_")
    if value in _PACKAGING_TYPES:
        return value
    if value.endswith("s") and value[:-1] in _PACKAGING_TYPES:
        return value[:-1]
    return ""


def _normalize_readability(raw_value: object) -> str:
    value = normalize_whitespace(str(raw_value or "")).lower()
    return value if value in _READABILITY_SCORES else "low"


def _coerce_confidence(raw_value: object) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


_FALSE_STRINGS = {"false", "no", "0", "none", "off", ""}


def _coerce_bool(raw_value: object) -> bool:
    # Model JSON may return a boolean flag as the string "false"; bool("false")
    # is truthy in Python, so coerce false-like strings explicitly.
    if isinstance(raw_value, str):
        return raw_value.strip().lower() not in _FALSE_STRINGS
    return bool(raw_value)


def _coerce_int(raw_value: object, default: int = 0) -> int:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _coerce_candidate_index(raw_value: object, candidate_count: int) -> int | None:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    if 0 <= value < candidate_count:
        return value
    return None


def _is_valid_facts_payload(payload: dict, requested_fields: list[str]) -> bool:
    extracted_values = {
        field_name: normalize_whitespace(str(payload.get(field_name, "") or ""))
        for field_name in _FACTS_FIELDS
    }
    combined_text = normalize_whitespace(
        " ".join(
            value
            for value in (
                str(payload.get("evidence", "") or ""),
                *extracted_values.values(),
            )
            if value
        )
    ).lower()
    if not combined_text:
        return False

    has_header = any(marker in combined_text for marker in _FACTS_HEADER_MARKERS)
    structure_hits = sum(1 for marker in _FACTS_STRUCTURE_MARKERS if marker in combined_text)
    extracted_field_count = sum(1 for value in extracted_values.values() if value)
    requested_field_hits = sum(1 for field_name in requested_fields if extracted_values.get(field_name))

    if requested_field_hits == 0:
        return False
    if has_header:
        return extracted_field_count >= 1
    return structure_hits >= 2 and extracted_field_count >= 2


def _build_supplement_facts_detail(
    *,
    payload: dict,
    source: str,
) -> SupplementFactsDetail | None:
    metadata_entries: list[SupplementFactsMetadataEntry] = []
    ingredient_entries: list[SupplementFactsIngredient] = []
    unparsed_fragments: list[str] = []

    raw_unparsed = payload.get("unparsed_fragments")
    if isinstance(raw_unparsed, list):
        for fragment in raw_unparsed:
            normalized = normalize_whitespace(str(fragment or ""))
            if normalized and normalized not in unparsed_fragments:
                unparsed_fragments.append(normalized)

    raw_metadata = payload.get("metadata")
    if isinstance(raw_metadata, list):
        for item in raw_metadata:
            if not isinstance(item, dict):
                continue
            label = normalize_whitespace(str(item.get("label", "") or ""))
            raw_value_text = _coerce_detail_text(item.get("raw_value_text"))
            if not label or not raw_value_text:
                continue
            canonical_label = canonical_supplement_metadata_label(label)
            if not canonical_label:
                if _normalize_matching_key(label) in _SUPPLEMENT_NOTE_METADATA_KEYS:
                    _append_unique(unparsed_fragments, raw_value_text)
                continue
            metadata_entries.append(
                SupplementFactsMetadataEntry(
                    label=canonical_label,
                    raw_metadata_text=raw_value_text,
                    final_csv_value=raw_value_text,
                )
            )

    raw_ingredients = payload.get("ingredients")
    if isinstance(raw_ingredients, list):
        for item in raw_ingredients:
            if not isinstance(item, dict):
                continue
            ingredient = _build_supplement_facts_ingredient(
                item=item,
                unparsed_fragments=unparsed_fragments,
            )
            if ingredient is not None:
                ingredient_entries.append(ingredient)

    other_ingredients = _coerce_detail_text(payload.get("other_ingredients"))
    confidence = _coerce_confidence(payload.get("confidence"))
    evidence = _coerce_detail_text(payload.get("evidence"))
    panel_detected = _coerce_bool(payload.get("panel_detected", False))
    if not panel_detected:
        # The model reports panel_detected=false when the image is not a true
        # Supplement/Nutrition Facts panel; abstain rather than exporting rows
        # parsed from unsupported text (parity with the legacy facts path).
        return None
    if not metadata_entries and not ingredient_entries and not other_ingredients and not unparsed_fragments:
        return None
    return SupplementFactsDetail(
        metadata=metadata_entries,
        ingredients=ingredient_entries,
        other_ingredients=other_ingredients,
        unparsed_fragments=unparsed_fragments,
        source=source,
        confidence=confidence,
        evidence=evidence,
    )


def _build_supplement_facts_ingredient(
    *,
    item: dict,
    unparsed_fragments: list[str],
) -> SupplementFactsIngredient | None:
    name = _coerce_detail_text(item.get("name"))
    if not name:
        return None

    raw_amount_text = _coerce_detail_text(item.get("raw_amount_text"))
    daily_value_text = _coerce_detail_text(
        item.get("daily_value_text")
        if "daily_value_text" in item
        else item.get("daily_value")
    )
    category = _normalize_supplement_ingredient_category(item.get("category"))
    parent_blend_name = _coerce_detail_text(item.get("parent_blend_name"))
    footnote_markers = _coerce_footnote_markers(item.get("footnote_markers"))
    notes = _coerce_detail_text(item.get("notes"))

    name, raw_amount_text, tail_text = _split_amount_from_ingredient_name(
        name=name,
        raw_amount_text=raw_amount_text,
    )
    raw_amount_text, amount_markers = _split_trailing_footnote_markers(raw_amount_text)
    daily_value_text, daily_value_markers = _normalize_daily_value_and_markers(
        " ".join(part for part in [daily_value_text, tail_text] if part)
    )
    footnote_markers = _dedupe_markers([*footnote_markers, *amount_markers, *daily_value_markers])

    normalized_amount_mg = normalize_amount_to_mg(raw_amount_text)
    csv_value = ""
    if normalized_amount_mg is not None:
        csv_value = _format_mg_value(normalized_amount_mg)
    elif unitless_amount := _normalize_unitless_numeric_amount(raw_amount_text):
        csv_value = unitless_amount
    elif category == "blend_sub_ingredient" and not raw_amount_text:
        csv_value = "grammage not mentioned"
    elif raw_amount_text:
        unresolved_fragment = f"{name}: {raw_amount_text}"
        if daily_value_text:
            unresolved_fragment = f"{unresolved_fragment} {daily_value_text}"
        _append_unique(unparsed_fragments, unresolved_fragment)

    return SupplementFactsIngredient(
        name=name,
        raw_amount_text=raw_amount_text,
        normalized_amount_mg=normalized_amount_mg,
        csv_value=csv_value,
        category=category,
        parent_blend_name=parent_blend_name,
        daily_value_text=daily_value_text,
        footnote_markers=footnote_markers,
        notes=notes,
    )


def _normalize_supplement_ingredient_category(value: object) -> str:
    category = _coerce_detail_text(value).lower() or "ingredient"
    if category not in _SUPPLEMENT_INGREDIENT_CATEGORIES:
        return "ingredient"
    return category


def _split_amount_from_ingredient_name(
    *,
    name: str,
    raw_amount_text: str,
) -> tuple[str, str, str]:
    if raw_amount_text:
        return name, raw_amount_text, ""

    includes_match = _INCLUDES_AMOUNT_NAME_PATTERN.match(name)
    if includes_match:
        return (
            normalize_whitespace(includes_match.group("name")),
            _format_amount_text(includes_match.group("amount"), includes_match.group("unit")),
            "",
        )

    trailing_match = _TRAILING_AMOUNT_NAME_PATTERN.match(name)
    if trailing_match:
        return (
            normalize_whitespace(trailing_match.group("name")),
            _format_amount_text(trailing_match.group("amount"), trailing_match.group("unit")),
            normalize_whitespace(trailing_match.group("tail")),
        )

    return name, raw_amount_text, ""


def _format_amount_text(amount: str, unit: str) -> str:
    return normalize_whitespace(f"{amount} {unit.lower()}")


def _normalize_unitless_numeric_amount(value: str) -> str:
    text = normalize_whitespace(value)
    if not _UNITLESS_NUMERIC_AMOUNT_PATTERN.fullmatch(text):
        return ""
    try:
        return _format_mg_value(float(text.replace(",", "")))
    except ValueError:
        return ""


def _split_trailing_footnote_markers(value: str) -> tuple[str, list[str]]:
    parts = value.split()
    if not parts:
        return "", []

    markers: list[str] = []
    while parts and _FOOTNOTE_MARKER_PATTERN.fullmatch(parts[-1]):
        markers.insert(0, parts.pop())
    return normalize_whitespace(" ".join(parts)), markers


def _normalize_daily_value_and_markers(value: str) -> tuple[str, list[str]]:
    text = normalize_whitespace(value)
    if not text:
        return "", []

    daily_value = ""
    markers: list[str] = []
    match = _DAILY_VALUE_PATTERN.search(text)
    if match:
        daily_value = match.group("value").replace(" ", "").replace(",", "")
        before = text[: match.start()]
        after = text[match.end() :]
        markers.extend(_extract_marker_tokens(before))
        markers.extend(_extract_marker_tokens(after))
    else:
        markers.extend(_extract_marker_tokens(text))
    return daily_value, _dedupe_markers(markers)


def _extract_marker_tokens(value: str) -> list[str]:
    markers: list[str] = []
    for token in normalize_whitespace(value).split():
        stripped = token.strip("()[]{}.,;:")
        if _FOOTNOTE_MARKER_PATTERN.fullmatch(stripped):
            markers.append(stripped)
    return markers


def _coerce_footnote_markers(value: object) -> list[str]:
    if isinstance(value, list):
        raw_markers = [_coerce_detail_text(marker) for marker in value]
    else:
        raw_markers = _coerce_detail_text(value).replace(",", " ").split()

    markers: list[str] = []
    for marker in raw_markers:
        stripped = marker.strip("()[]{}.,;:")
        if stripped and _FOOTNOTE_MARKER_PATTERN.fullmatch(stripped):
            markers.append(stripped)
    return _dedupe_markers(markers)


def _dedupe_markers(values: Iterable[str]) -> list[str]:
    markers: list[str] = []
    for value in values:
        normalized = normalize_whitespace(value)
        if normalized and normalized not in markers:
            markers.append(normalized)
    return markers


def _coerce_detail_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [_coerce_detail_text(item) for item in value]
        return " | ".join(part for part in parts if part)
    if isinstance(value, dict):
        return normalize_whitespace(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return normalize_whitespace(str(value))


def _append_unique(values: list[str], value: str) -> None:
    normalized = normalize_whitespace(value)
    if normalized and normalized not in values:
        values.append(normalized)


def _build_field_results_from_supplement_facts_detail(
    *,
    detail: SupplementFactsDetail,
    requested_fields: list[str],
) -> dict[str, FieldResult]:
    results: dict[str, FieldResult] = {}
    for field_name in requested_fields:
        if field_name in {"serving_size", "servings_per_container"}:
            metadata_entry = _find_metadata_entry_for_field(field_name, detail.metadata)
            if metadata_entry is None:
                continue
            field_hint = get_field_hint(field_name)
            parsed_value = parse_value(field_hint.parser, metadata_entry.final_csv_value) or metadata_entry.final_csv_value
            if not parsed_value:
                continue
            results[field_name] = FieldResult(
                value=parsed_value,
                source=detail.source,
                confidence=detail.confidence,
                evidence=detail.evidence or metadata_entry.raw_metadata_text,
            )
            continue
        if field_name == "ingredients" and detail.ingredients:
            ingredient_names: list[str] = []
            seen_names: set[str] = set()
            for ingredient in detail.ingredients:
                key = _normalize_matching_key(ingredient.name)
                if not key or key in seen_names:
                    continue
                seen_names.add(key)
                ingredient_names.append(ingredient.name)
            if ingredient_names:
                raw_value = " | ".join(ingredient_names)
                results[field_name] = FieldResult(
                    value=raw_value,
                    source=detail.source,
                    confidence=detail.confidence,
                    evidence=detail.evidence or raw_value,
                )
    return results


def _find_metadata_entry_for_field(
    field_name: str,
    metadata_entries: list[SupplementFactsMetadataEntry],
) -> SupplementFactsMetadataEntry | None:
    hint = get_field_hint(field_name)
    alias_keys = {
        _normalize_matching_key(field_name),
        _normalize_matching_key(field_name.replace("_", " ")),
        *(_normalize_matching_key(alias) for alias in hint.label_aliases),
        *(_normalize_matching_key(alias) for alias in hint.input_aliases),
    }
    for entry in metadata_entries:
        if _normalize_matching_key(entry.label) in alias_keys:
            return entry
    return None


def _normalize_matching_key(value: str) -> str:
    return " ".join(
        normalize_whitespace(value).lower().replace("-", " ").replace("_", " ").split()
    )


def _format_mg_value(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _build_field_tokens(field_name: str) -> set[str]:
    hint = get_field_hint(field_name)
    raw_tokens = {
        field_name.replace("_", " "),
        *(hint.label_aliases or []),
        *(hint.input_aliases or []),
    }
    tokens: set[str] = set()
    for raw_token in raw_tokens:
        for token in normalize_whitespace(raw_token).lower().replace("-", " ").split():
            if len(token) >= 4:
                tokens.add(token)
    return tokens


def _preferred_generic_roles(field_name: str) -> dict[str, float]:
    if field_name in _FACTS_FIELDS:
        return {"facts_panel": 3.0, "packaging": 1.5, "marketing_text": 1.0}
    if field_name == "packaging_type":
        return {"packaging": 3.0, "marketing_text": 1.0}
    return {"marketing_text": 2.5, "packaging": 2.0, "facts_panel": 1.5, "other": 0.5}


def _build_direct_support_instruction(requested_label: str) -> str:
    normalized_label = normalize_whitespace(requested_label) or "the requested field"
    return (
        "Direct support rules:\n"
        "- Return a value only when the image explicitly supports the requested field itself.\n"
        "- Treat label/value pairs, selected variations, free-from claims, and clearly printed packaging details as direct support.\n"
        "- Do not infer the field from brand names, product names, ingredient names, dosage text, or general marketing copy.\n"
        f"- If the image only shows product identity text or a front-label ingredient phrase and does not explicitly answer '{normalized_label}', return an empty value.\n"
    )
