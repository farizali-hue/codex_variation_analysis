from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import FieldResult
from .value_parsers import normalize_whitespace

_DEFAULT_PRESET_PATH = Path(__file__).resolve().parent.parent / "schemas" / "custom_field_presets.json"
_ALLOWED_OUTPUT_TYPES = ("number", "short_list", "text")
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_LIST_SPLIT_PATTERN = re.compile("(?:\\r?\\n|[;,|\\u2022]+)")
_AND_SPLIT_PATTERN = re.compile(r"\s+(?:and|&)\s+", re.IGNORECASE)

OUTPUT_TYPE_LABELS = {
    "number": "Number",
    "short_list": "Short list",
    "text": "Text",
}
OUTPUT_TYPE_HELP = {
    "number": "One numeric value only, like 30 or 500.",
    "short_list": "Multiple items stored in one cell and joined with ' | '.",
    "text": "One cleaned phrase or value, like vanilla or unflavored.",
}
MERGE_SIMILAR_VALUES_HELP = (
    "Turn this on for open text fields like flavor so obvious equivalents can collapse "
    "into one normalized value."
)

_TEXT_EQUIVALENTS = {
    "unflavoured": "unflavored",
    "unflavored": "unflavored",
    "no flavour": "unflavored",
    "no flavor": "unflavored",
    "plain": "unflavored",
}


@dataclass(frozen=True)
class CustomFieldPreset:
    output_type: str = "text"
    merge_similar_values: bool = False


def load_custom_field_presets(schema_path: Path | None = None) -> dict[str, CustomFieldPreset]:
    path = schema_path or _DEFAULT_PRESET_PATH
    if not path.exists():
        return {}

    try:
        raw_data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    if not isinstance(raw_data, dict):
        return {}

    presets: dict[str, CustomFieldPreset] = {}
    for field_name, payload in raw_data.items():
        if not isinstance(payload, dict):
            continue
        output_type = str(payload.get("output_type", "text") or "text").strip().lower()
        if output_type not in _ALLOWED_OUTPUT_TYPES:
            output_type = "text"
        presets[_normalize_field_name(field_name)] = CustomFieldPreset(
            output_type=output_type,
            merge_similar_values=bool(payload.get("merge_similar_values", False)),
        )
    return presets


def save_custom_field_presets(
    presets: dict[str, CustomFieldPreset],
    schema_path: Path | None = None,
) -> None:
    path = schema_path or _DEFAULT_PRESET_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        field_name: {
            "output_type": preset.output_type,
            "merge_similar_values": preset.merge_similar_values,
        }
        for field_name, preset in sorted(presets.items())
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def apply_custom_field_preset(
    field_name: str,
    result: FieldResult,
    presets: dict[str, CustomFieldPreset] | None = None,
) -> FieldResult:
    preset = get_custom_field_preset(field_name, presets=presets)
    if preset is None:
        return result

    raw_value = result.value
    normalized_value = normalize_custom_field_value(raw_value, preset)
    return FieldResult(
        value=normalized_value,
        source=result.source,
        confidence=result.confidence,
        evidence=result.evidence or normalize_whitespace(raw_value),
    )


def get_custom_field_preset(
    field_name: str,
    presets: dict[str, CustomFieldPreset] | None = None,
) -> CustomFieldPreset | None:
    normalized_name = _normalize_field_name(field_name)
    available_presets = presets if presets is not None else load_custom_field_presets()
    return available_presets.get(normalized_name)


def normalize_custom_field_value(raw_value: str, preset: CustomFieldPreset) -> str:
    text = normalize_whitespace(raw_value)
    if not text:
        return ""

    if preset.output_type == "number":
        return _normalize_number_value(text)
    if preset.output_type == "short_list":
        return _normalize_short_list_value(text, merge_similar_values=preset.merge_similar_values)
    return _normalize_text_value(text, merge_similar_values=preset.merge_similar_values)


def serialize_custom_field_presets(
    presets: dict[str, CustomFieldPreset],
) -> dict[str, dict[str, object]]:
    return {
        field_name: {
            "output_type": preset.output_type,
            "merge_similar_values": preset.merge_similar_values,
        }
        for field_name, preset in presets.items()
    }


def deserialize_custom_field_presets(
    raw_presets: dict[str, dict[str, object]] | None,
) -> dict[str, CustomFieldPreset]:
    presets: dict[str, CustomFieldPreset] = {}
    for field_name, payload in (raw_presets or {}).items():
        if not isinstance(payload, dict):
            continue
        output_type = str(payload.get("output_type", "text") or "text").strip().lower()
        if output_type not in _ALLOWED_OUTPUT_TYPES:
            output_type = "text"
        presets[_normalize_field_name(field_name)] = CustomFieldPreset(
            output_type=output_type,
            merge_similar_values=bool(payload.get("merge_similar_values", False)),
        )
    return presets


def _normalize_field_name(field_name: str) -> str:
    return normalize_whitespace(str(field_name or "").replace("-", "_").lower()).replace(" ", "_")


def _normalize_number_value(text: str) -> str:
    match = _NUMBER_PATTERN.search(text)
    if not match:
        return ""
    number_text = match.group(0)
    if "." in number_text:
        number_text = number_text.rstrip("0").rstrip(".")
    return number_text


def _normalize_short_list_value(text: str, *, merge_similar_values: bool) -> str:
    parts = [normalize_whitespace(part) for part in _LIST_SPLIT_PATTERN.split(text) if normalize_whitespace(part)]
    if len(parts) <= 1 and _AND_SPLIT_PATTERN.search(text):
        parts = [normalize_whitespace(part) for part in _AND_SPLIT_PATTERN.split(text) if normalize_whitespace(part)]
    if not parts:
        parts = [text]

    normalized_parts: list[str] = []
    seen: set[str] = set()
    for part in parts:
        normalized_part = _normalize_text_value(part, merge_similar_values=merge_similar_values)
        if not normalized_part:
            continue
        dedupe_key = normalized_part.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized_parts.append(normalized_part)
    return " | ".join(normalized_parts)


def _normalize_text_value(text: str, *, merge_similar_values: bool) -> str:
    cleaned = normalize_whitespace(text)
    if not merge_similar_values:
        return cleaned

    lowered = cleaned.lower().replace("&", " and ")
    lowered = lowered.replace("flavour", "flavor").replace("flavoured", "flavored")
    lowered = re.sub(r"[^a-z0-9+/ ]+", " ", lowered)
    lowered = normalize_whitespace(lowered)
    if lowered in _TEXT_EQUIVALENTS:
        return _TEXT_EQUIVALENTS[lowered]
    return lowered
