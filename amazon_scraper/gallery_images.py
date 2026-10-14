from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any


_GALLERY_PAYLOAD_PATTERN = re.compile(r"jQuery\.parseJSON\('(?P<payload>.*?)'\)", re.DOTALL)
_MEDIA_ID_PATTERN = re.compile(r"/images/I/([^./]+)")
_IMAGE_BLOCK_PATTERN = re.compile(r"ImageBlock(?:ATF|BTF)")
_JS_STRING = r"(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
_JS_VALUE_PATTERN = re.compile(
    rf"(?P<string>{_JS_STRING})"
    rf"|(?P<json_call>(?:A\.\$\.parseJSON|jQuery\.parseJSON|JSON\.parse)\(\s*(?P<json_string>{_JS_STRING})\s*\))"
    r"|(?P<literal>\b(?:true|false|null|undefined)\b)"
    r"|(?P<call>[A-Za-z_$][A-Za-z0-9_$.]*\([^{}[\]]*?\))",
    re.DOTALL,
)


@dataclass(frozen=True)
class GalleryImageRecord:
    slot_index: int
    variant: str
    thumb_url: str = ""
    large_url: str = ""
    hi_res_url: str = ""
    main_urls: list[str] = field(default_factory=list)
    physical_id: str = ""
    media_type: str = "image"


def extract_gallery_images(html: str) -> list[GalleryImageRecord]:
    payload = _extract_gallery_payload(html)
    color_images = payload.get("colorImages")
    if not isinstance(color_images, dict):
        return []

    variant_key = _resolve_variant_key(payload, color_images)
    if variant_key is None:
        return []

    raw_entries = color_images.get(variant_key)
    if not isinstance(raw_entries, list):
        return []

    images: list[GalleryImageRecord] = []
    seen_keys: set[str] = set()
    for slot_index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            continue

        record = GalleryImageRecord(
            slot_index=slot_index,
            variant=str(raw_entry.get("variant", "") or ""),
            thumb_url=_clean_url(raw_entry.get("thumb")),
            large_url=_clean_url(raw_entry.get("large")),
            hi_res_url=_clean_url(raw_entry.get("hiRes")),
            main_urls=_extract_main_urls(raw_entry.get("main")),
            physical_id=_resolve_physical_id(raw_entry),
        )
        dedupe_key = record.physical_id or _first_non_empty(
            record.hi_res_url,
            record.large_url,
            *(record.main_urls or []),
            record.thumb_url,
        )
        if not dedupe_key or dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        images.append(record)

    return images


def select_classification_image_url(image: GalleryImageRecord) -> str:
    return _first_non_empty(image.large_url, *(image.main_urls or []), image.hi_res_url)


def select_extraction_image_url(image: GalleryImageRecord) -> str:
    return _first_non_empty(image.hi_res_url, image.large_url, *(image.main_urls or []))


def _extract_gallery_payload(html: str) -> dict[str, Any]:
    candidates: list[tuple[int, dict[str, Any]]] = []

    for match in _GALLERY_PAYLOAD_PATTERN.finditer(html or ""):
        payload = _parse_json_gallery_payload(match.group("payload"))
        if payload is not None:
            candidates.append((match.start(), payload))

    for marker in _IMAGE_BLOCK_PATTERN.finditer(html or ""):
        payload = _extract_direct_gallery_payload(html or "", marker.start())
        if payload is not None:
            candidates.append((marker.start(), payload))

    for _, payload in sorted(candidates, key=lambda item: item[0]):
        if _payload_has_gallery_images(payload):
            return payload
    return {}


def _parse_json_gallery_payload(raw_payload: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(f'"{raw_payload}"')
        payload = json.loads(decoded)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _extract_direct_gallery_payload(html: str, marker_index: int) -> dict[str, Any] | None:
    search_start = html.find("var data = {", marker_index)
    if search_start == -1:
        return None

    brace_start = html.find("{", search_start)
    if brace_start == -1:
        return None

    raw_object = _extract_balanced_braces(html, brace_start)
    if not raw_object:
        return None

    try:
        normalized = _JS_VALUE_PATTERN.sub(_normalize_js_value, raw_object)
        payload = ast.literal_eval(normalized)
    except (SyntaxError, ValueError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _extract_balanced_braces(text: str, start_index: int) -> str:
    depth = 0
    quote_char = ""
    is_escaped = False

    for index in range(start_index, len(text)):
        char = text[index]

        if quote_char:
            if is_escaped:
                is_escaped = False
            elif char == "\\":
                is_escaped = True
            elif char == quote_char:
                quote_char = ""
            continue

        if char in ("'", '"'):
            quote_char = char
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start_index : index + 1]

    return ""


def _normalize_js_value(match: re.Match[str]) -> str:
    """Decode data literals without executing any JavaScript from the page.

    Amazon's MediaBlock embeds colorImages.initial as A.$.parseJSON('...').
    Consume strings as whole tokens so words such as 'null' and call-like text
    in URLs, variant names, or labels are never rewritten.
    """
    if match.group("string") is not None:
        return match.group(0)
    if match.group("json_call") is not None:
        return repr(json.loads(ast.literal_eval(match.group("json_string"))))
    if match.group("call") is not None:
        # Preserve the existing treatment of unrelated metadata (Date.now()).
        return "None"
    replacements = {
        "true": "True",
        "false": "False",
        "null": "None",
        "undefined": "None",
    }
    return replacements[match.group("literal")]


def _payload_has_gallery_images(payload: dict[str, Any]) -> bool:
    color_images = payload.get("colorImages")
    if not isinstance(color_images, dict):
        return False
    return _resolve_variant_key(payload, color_images) is not None


def _resolve_variant_key(payload: dict[str, Any], color_images: dict[str, Any]) -> str | None:
    preferred_keys = [
        str(payload.get("landingAsinColor", "") or "").strip(),
        str(payload.get("defaultColor", "") or "").strip(),
        "initial",
    ]
    for key in preferred_keys:
        if key and isinstance(color_images.get(key), list):
            return key

    for key, value in color_images.items():
        if isinstance(value, list):
            return str(key)
    return None


def _extract_main_urls(raw_main: object) -> list[str]:
    if not isinstance(raw_main, dict):
        return []

    ranked_urls: list[tuple[int, str]] = []
    for url, dimensions in raw_main.items():
        cleaned_url = _clean_url(url)
        if not cleaned_url:
            continue
        ranked_urls.append((_coerce_area(dimensions), cleaned_url))

    ranked_urls.sort(key=lambda item: item[0], reverse=True)
    return [url for _, url in ranked_urls]


def _resolve_physical_id(raw_entry: dict[str, Any]) -> str:
    for key in ("physicalIdForMedia", "physical_id", "physicalId"):
        value = str(raw_entry.get(key, "") or "").strip()
        if value:
            return value

    for candidate in (
        _clean_url(raw_entry.get("hiRes")),
        _clean_url(raw_entry.get("large")),
        _first_main_url(raw_entry.get("main")),
        _clean_url(raw_entry.get("thumb")),
    ):
        if not candidate:
            continue
        match = _MEDIA_ID_PATTERN.search(candidate)
        if match:
            return match.group(1)
    return ""


def _first_main_url(raw_main: object) -> str:
    main_urls = _extract_main_urls(raw_main)
    return main_urls[0] if main_urls else ""


def _coerce_area(dimensions: object) -> int:
    if not isinstance(dimensions, (list, tuple)) or len(dimensions) != 2:
        return 0
    try:
        width = int(dimensions[0])
        height = int(dimensions[1])
    except (TypeError, ValueError):
        return 0
    if width <= 0 or height <= 0:
        return 0
    return width * height


def _clean_url(value: object) -> str:
    return str(value or "").strip()


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""
