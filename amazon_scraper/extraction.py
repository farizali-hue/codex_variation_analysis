from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import List

from .contract_value_normalization import build_output_type_instruction
from .field_hints import get_field_hint
from .models import ChunkRecord, FieldResult, RunFieldContract
from .openai_client import OpenAIClient
from .value_parsers import normalize_whitespace, parse_value

LOGGER = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You extract one product field from Amazon PDP evidence. "
    "Use only the supplied evidence chunks. "
    "Return a value only when the evidence directly supports the requested field itself. "
    "Do not infer a field from product names, brand names, ingredient names, dosage text, or general marketing copy. "
    "A product identity term or hero ingredient phrase is not enough unless the evidence explicitly presents it as the requested field. "
    "If the field is not directly supported, return an empty value and explain that in evidence. "
    "Return JSON with keys value, evidence, source, confidence. "
    "confidence must be a number between 0 and 1."
)
_LABEL_VALUE_PATTERN = re.compile(r"^\s*([^:]{1,200}?)\s*:\s*(.+?)\s*$")


@dataclass(frozen=True)
class ExtractionResponse:
    value: str
    evidence: str
    source: str
    confidence: float


class OpenAIFieldExtractor:
    def __init__(self, client: OpenAIClient, confidence_threshold: float = 0.45) -> None:
        self.client = client
        self.confidence_threshold = confidence_threshold

    def extract_field(
        self,
        field_name: str,
        title: str,
        chunks: List[ChunkRecord],
        *,
        asin: str,
        retry_attempt: int = 0,
        field_contract: RunFieldContract | None = None,
    ) -> FieldResult:
        if not chunks:
            return FieldResult()

        if field_contract is not None:
            LOGGER.info(
                "field_contract_event scope=html_extractor asin=%s requested_label=%s internal_key=%s output_type=%s merge_similar_values=%s matched_by=%s",
                asin,
                field_contract.requested_label,
                field_contract.internal_key,
                field_contract.output_type,
                str(field_contract.merge_similar_values).lower(),
                field_contract.matched_by,
            )

        user_prompt = self._build_user_prompt(field_name, title, chunks, field_contract)
        response_text = self.client.create_chat_completion(
            _SYSTEM_PROMPT,
            user_prompt,
            asin=asin,
            field=field_name,
            retry_attempt=retry_attempt,
        )
        payload = self._parse_response(response_text)

        field_hint = get_field_hint(field_name)
        normalized_value = parse_value(field_hint.parser, payload.value)
        if not normalized_value and field_hint.parser == "text":
            normalized_value = normalize_whitespace(payload.value)

        result = FieldResult(
            value=normalized_value,
            source=normalize_whitespace(payload.source),
            confidence=payload.confidence,
            evidence=normalize_whitespace(payload.evidence),
        )
        if not result.evidence:
            result.evidence = chunks[0].text
        if not result.source:
            result.source = chunks[0].section

        if result.confidence < self.confidence_threshold or not result.value:
            result.value = ""

        return result

    def _build_user_prompt(
        self,
        field_name: str,
        title: str,
        chunks: List[ChunkRecord],
        field_contract: RunFieldContract | None,
    ) -> str:
        field_hint = get_field_hint(field_name)
        labels = list(field_hint.label_aliases)
        primary_label = field_name
        if field_contract is not None and field_contract.requested_label:
            primary_label = field_contract.requested_label
            labels = [field_contract.requested_label, *labels]
        aliases = ", ".join(dict.fromkeys(label for label in labels if label))
        evidence_lines = [
            f"{index}. section={chunk.section}; dom_hint={chunk.dom_hint}; text={chunk.text}"
            for index, chunk in enumerate(chunks, start=1)
        ]
        evidence_block = "\n".join(evidence_lines)
        contract_instruction = (
            build_output_type_instruction(field_contract) + "\n"
            if field_contract is not None
            else ""
        )
        support_instruction = _build_direct_support_instruction(primary_label)
        return (
            f"Field name: {field_name}\n"
            f"Requested label: {primary_label}\n"
            f"Related labels: {aliases}\n"
            f"Product title: {title or '[missing title]'}\n"
            f"{contract_instruction}"
            f"{support_instruction}"
            "Evidence chunks:\n"
            f"{evidence_block}\n"
            "Return the best supported field value only."
        )

    def _parse_response(self, response_text: str) -> ExtractionResponse:
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            # Raising here would bubble into run_job's catch-all and downgrade
            # the WHOLE job to NAVIGATION_ERROR, dropping every already-resolved
            # field. Abstain on this one field instead (vision-path parity).
            LOGGER.warning(
                "Discarding malformed extraction response as abstention response_chars=%d",
                len(response_text),
            )
            return ExtractionResponse(value="", evidence="", source="", confidence=0.0)
        confidence = _coerce_confidence(data.get("confidence", 0))
        return ExtractionResponse(
            value=str(data.get("value", "") or ""),
            evidence=str(data.get("evidence", "") or ""),
            source=str(data.get("source", "") or ""),
            confidence=confidence,
        )


def build_query_text(field_name: str) -> str:
    return build_query_text_for_contract(field_name, None)


def build_query_text_for_contract(field_name: str, field_contract: RunFieldContract | None) -> str:
    field_hint = get_field_hint(field_name)
    labels = list(field_hint.label_aliases)
    if field_contract is not None and field_contract.requested_label:
        labels = [field_contract.requested_label, *labels]
    aliases = ", ".join(dict.fromkeys(label for label in labels if label))
    query_text = f"Amazon product field: {field_name}. Related labels: {aliases}."
    if field_contract is not None:
        query_text += f" {build_output_type_instruction(field_contract)}"
    return query_text


def resolve_field_from_facts_rows(field_name: str, chunks: List[ChunkRecord]) -> FieldResult | None:
    field_hint = get_field_hint(field_name)
    alias_keys = {_normalize_label(alias) for alias in field_hint.label_aliases if alias}
    if not alias_keys:
        return None

    for chunk in chunks:
        if chunk.section != "facts_table":
            continue

        parts = _split_label_value(chunk.text)
        if parts is None:
            continue

        label, raw_value = parts
        if _normalize_label(label) not in alias_keys:
            continue

        parsed_value = parse_value(field_hint.parser, raw_value)
        if not parsed_value:
            parsed_value = parse_value(field_hint.parser, chunk.text)
        if not parsed_value and field_hint.parser == "text":
            parsed_value = normalize_whitespace(raw_value)
        if not parsed_value:
            continue

        return FieldResult(
            value=parsed_value,
            source=f"{chunk.section}: {chunk.dom_hint}",
            confidence=0.99,
            evidence=chunk.text,
        )

    return None


def resolve_field_from_text_chunks(field_name: str, chunks: List[ChunkRecord]) -> FieldResult | None:
    del field_name, chunks
    return None



def _split_label_value(text: str) -> tuple[str, str] | None:
    match = _LABEL_VALUE_PATTERN.match(text)
    if not match:
        return None
    return normalize_whitespace(match.group(1)), normalize_whitespace(match.group(2))


def _normalize_label(value: str) -> str:
    collapsed = normalize_whitespace(value).lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", collapsed)
    return " ".join(collapsed.split())



def _coerce_confidence(raw_value: object) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


def _build_direct_support_instruction(requested_label: str) -> str:
    normalized_label = normalize_whitespace(requested_label) or "the requested field"
    return (
        "Direct support rules:\n"
        "- Return a value only when the evidence explicitly supports the requested field itself.\n"
        "- Treat label/value pairs, selected variations, free-from claims, and clearly printed packaging details as direct support.\n"
        "- Do not infer the field from brand names, product names, ingredient names, dosage text, or general marketing copy.\n"
        f"- If the evidence only shows product identity text or a front-label ingredient phrase and does not explicitly answer '{normalized_label}', return an empty value.\n"
    )
