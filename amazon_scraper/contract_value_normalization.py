from __future__ import annotations

import re

from .models import FieldResult, RunFieldContract
from .value_parsers import normalize_whitespace

_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_LIST_SPLIT_PATTERN = re.compile(r"(?:\r?\n|[;,|\u2022]+)")
_AND_SPLIT_PATTERN = re.compile(r"\s+(?:and|&)\s+", re.IGNORECASE)


def normalize_field_result_for_contract(
    result: FieldResult,
    field_contract: RunFieldContract,
) -> FieldResult:
    raw_value = result.value
    normalized_value = normalize_value_for_contract(raw_value, field_contract)
    return FieldResult(
        value=normalized_value,
        source=result.source,
        confidence=result.confidence,
        evidence=result.evidence or normalize_whitespace(raw_value),
    )


def normalize_value_for_contract(raw_value: str, field_contract: RunFieldContract) -> str:
    text = normalize_whitespace(raw_value)
    if not text:
        return ""

    if field_contract.output_type == "number":
        return _normalize_number_value(text)
    if field_contract.output_type == "short_list":
        return _normalize_short_list_value(text)
    return normalize_whitespace(text)


def build_output_type_instruction(field_contract: RunFieldContract) -> str:
    if field_contract.output_type == "number":
        return (
            "Output contract: return only one numeric value as the field value. "
            "Do not include units, labels, or extra words in the value."
        )
    if field_contract.output_type == "short_list":
        return (
            "Output contract: return a flat list of distinct items in one string. "
            "Separate multiple items with ' | '. Do not return a sentence."
        )
    return (
        "Output contract: return one cleaned text value only. "
        "Do not return explanations or multiple alternatives."
    )


def _normalize_number_value(text: str) -> str:
    match = _NUMBER_PATTERN.search(text)
    if not match:
        return ""
    number_text = match.group(0)
    if "." in number_text:
        number_text = number_text.rstrip("0").rstrip(".")
    return number_text


def _normalize_short_list_value(text: str) -> str:
    parts = [normalize_whitespace(part) for part in _LIST_SPLIT_PATTERN.split(text) if normalize_whitespace(part)]
    if len(parts) <= 1 and _AND_SPLIT_PATTERN.search(text):
        parts = [normalize_whitespace(part) for part in _AND_SPLIT_PATTERN.split(text) if normalize_whitespace(part)]
    if not parts:
        parts = [text]

    normalized_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized_part = normalize_whitespace(part)
        if not normalized_part:
            continue
        dedupe_key = normalized_part.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized_parts.append(normalized_part)
    return " | ".join(normalized_parts)
