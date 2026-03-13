from __future__ import annotations

import re


AMOUNT_UNIT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|grams?|milligrams?|ml|oz)\b", re.IGNORECASE)
SERVING_SIZE_PATTERN = re.compile(
    r"\b(\d+\s*(?:servings?|sticks?|packets?|pods?|capsules?|sachets?|count|ct))\b",
    re.IGNORECASE,
)
ROAST_LEVEL_PATTERN = re.compile(
    r"\b((?:light|medium|medium dark|medium-dark|dark|blonde|bold medium)\s+roast)\b",
    re.IGNORECASE,
)


def parse_value(parser_name: str, raw_value: str) -> str:
    """Parse a raw extracted value into normalized output based on parser name."""
    text = normalize_whitespace(raw_value)
    if not text:
        return ""

    if parser_name == "yes_no_or_status":
        return parse_yes_no_or_status(text)
    if parser_name == "amount_with_unit":
        return parse_amount_with_unit(text)
    if parser_name == "serving_size":
        return parse_serving_size(text)
    if parser_name == "roast_level":
        return parse_roast_level(text)
    return text


def validate_value(validator_name: str, value: str) -> bool:
    """Validate parsed values before they are accepted as final field output."""
    if validator_name == "non_empty":
        return bool(normalize_whitespace(value))
    return True


def parse_yes_no_or_status(text: str) -> str:
    """Map caffeine phrasing into a stable Yes/No output when possible."""
    lowered = text.lower()
    if any(token in lowered for token in ("decaf", "decaffeinated", "caffeine free", "caffeine-free", "no caffeine")):
        return "No"
    if any(token in lowered for token in ("caffeinated", "contains caffeine", "with caffeine", "low caffeine")):
        return "Yes"
    if "caffeine" in lowered:
        return "Yes"
    return text


def parse_amount_with_unit(text: str) -> str:
    """Extract first amount+unit token, e.g. '50 mg', from free text."""
    match = AMOUNT_UNIT_PATTERN.search(text)
    if not match:
        return ""
    amount = match.group(1)
    unit = match.group(2).lower()
    return f"{amount} {unit}"


def parse_serving_size(text: str) -> str:
    """Extract serving-count style values, e.g. '30 Servings', from free text."""
    match = SERVING_SIZE_PATTERN.search(text)
    if not match:
        return ""
    return normalize_whitespace(match.group(1))


def parse_roast_level(text: str) -> str:
    """Extract common roast-level phrases from free text."""
    match = ROAST_LEVEL_PATTERN.search(text)
    if not match:
        return ""
    return normalize_whitespace(match.group(1)).title()


def normalize_whitespace(value: str) -> str:
    """Collapse repeated whitespace and trim edges for deterministic comparisons."""
    return " ".join(value.split()).strip()
