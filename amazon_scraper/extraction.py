from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List

from .field_hints import get_field_hint
from .models import ChunkRecord, FieldResult
from .openai_client import OpenAIClient
from .value_parsers import normalize_whitespace, parse_value


_SYSTEM_PROMPT = (
    "You extract one product field from Amazon PDP evidence. "
    "Use only the supplied evidence chunks. "
    "If the field is not directly supported, return an empty value and explain that in evidence. "
    "Return JSON with keys value, evidence, source, confidence. "
    "confidence must be a number between 0 and 1."
)


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

    def extract_field(self, field_name: str, title: str, chunks: List[ChunkRecord]) -> FieldResult:
        if not chunks:
            return FieldResult()

        user_prompt = self._build_user_prompt(field_name, title, chunks)
        response_text = self.client.create_chat_completion(_SYSTEM_PROMPT, user_prompt)
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

    def _build_user_prompt(self, field_name: str, title: str, chunks: List[ChunkRecord]) -> str:
        field_hint = get_field_hint(field_name)
        aliases = ", ".join(field_hint.label_aliases)
        evidence_lines = [
            f"{index}. section={chunk.section}; dom_hint={chunk.dom_hint}; text={chunk.text}"
            for index, chunk in enumerate(chunks, start=1)
        ]
        evidence_block = "\n".join(evidence_lines)
        return (
            f"Field name: {field_name}\n"
            f"Related labels: {aliases}\n"
            f"Product title: {title or '[missing title]'}\n"
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
        data = json.loads(cleaned)
        confidence = _coerce_confidence(data.get("confidence", 0))
        return ExtractionResponse(
            value=str(data.get("value", "") or ""),
            evidence=str(data.get("evidence", "") or ""),
            source=str(data.get("source", "") or ""),
            confidence=confidence,
        )


def build_query_text(field_name: str) -> str:
    field_hint = get_field_hint(field_name)
    aliases = ", ".join(field_hint.label_aliases)
    return f"Amazon product field: {field_name}. Related labels: {aliases}."


def _coerce_confidence(raw_value: object) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))
