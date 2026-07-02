from __future__ import annotations

import re


AMOUNT_NUMBER_PATTERN = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
AMOUNT_UNIT_PATTERN = re.compile(
    rf"(?<![\d,])({AMOUNT_NUMBER_PATTERN})\s*"
    r"(mgs?|mcgs?|milligrams?|micrograms?|kgs?|kilograms?|grams?|g|lbs?|pounds?|oz|ounces?|ml)\b",
    re.IGNORECASE,
)
SERVING_SIZE_PATTERN = re.compile(
    rf"\b({AMOUNT_NUMBER_PATTERN}\s*(?:servings?|sticks?|packets?|pods?|capsules?|sachets?|count|ct))\b",
    re.IGNORECASE,
)
# Require container context (label or servings/count suffix) before accepting a
# number: a bare number in unrelated text must abstain, not become a canonical
# count. Numbers accept grouped thousands ("1,000").
SERVINGS_PER_CONTAINER_PATTERN = re.compile(
    rf"\bservings?\s+per\s+(?:container|package)\s*:?\s*({AMOUNT_NUMBER_PATTERN})\b"
    rf"|\b({AMOUNT_NUMBER_PATTERN})\s*(?:servings?|count|ct)\b",
    re.IGNORECASE,
)
UNIT_NORMALIZATION = {
    "mg": "mg",
    "mgs": "mg",
    "milligram": "mg",
    "milligrams": "mg",
    "mcg": "mcg",
    "mcgs": "mcg",
    "microgram": "mcg",
    "micrograms": "mcg",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "ml": "ml",
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
}

_NUMERIC_VALUE_PATTERN = re.compile(AMOUNT_NUMBER_PATTERN)


def normalize_numeric_string(text: str) -> str:
    """Return the first numeric value in ``text`` as a bare number string.

    Preserves grouped thousands ("1,000" -> "1000") and trims trailing decimal
    zeros ("2.50" -> "2.5"); returns "" when no number is present. Shared by the
    run-scoped contract and custom-preset number normalizers so thousands-separator
    handling lives in exactly one place.
    """
    match = _NUMERIC_VALUE_PATTERN.search(text)
    if not match:
        return ""
    number_text = match.group(0).replace(",", "")
    if "." in number_text:
        number_text = number_text.rstrip("0").rstrip(".")
    return number_text


def parse_value(parser_name: str, raw_value: str) -> str:
    """Parse a raw extracted value into normalized output based on parser name."""
    text = normalize_whitespace(raw_value)
    if not text:
        return ""

    if parser_name == "serving_size":
        return parse_serving_size(text)
    if parser_name == "servings_per_container":
        return parse_servings_per_container(text)
    if parser_name == "packaging_type":
        return parse_packaging_type(text)
    return text


def validate_value(validator_name: str, value: str) -> bool:
    """Validate parsed values before they are accepted as final field output."""
    if validator_name == "non_empty":
        return bool(normalize_whitespace(value))
    return True


def parse_serving_size(text: str) -> str:
    """Extract serving-count style values, e.g. '30 Servings', from free text."""
    match = SERVING_SIZE_PATTERN.search(text)
    if not match:
        return ""
    return normalize_whitespace(match.group(1))


def parse_servings_per_container(text: str) -> str:
    """Extract numeric servings-per-container counts when present."""
    match = SERVINGS_PER_CONTAINER_PATTERN.search(text)
    if not match:
        return ""
    number_text = match.group(1) or match.group(2) or ""
    return normalize_whitespace(number_text).replace(",", "")


def parse_packaging_type(text: str) -> str:
    """Normalize free-text packaging descriptions into a stable enum."""
    lowered = normalize_whitespace(text).lower().replace("-", " ")
    if "stick pack" in lowered or "stickpack" in lowered or "stick packs" in lowered:
        return "stick_pack"
    if "sachet" in lowered or "sachets" in lowered:
        return "sachet"
    if "pouch" in lowered or "bag" in lowered:
        return "pouch"
    if "canister" in lowered:
        return "canister"
    if "bottle" in lowered:
        return "bottle"
    if "jar" in lowered:
        return "jar"
    if "box" in lowered or "carton" in lowered:
        return "box"
    if "tub" in lowered:
        return "tub"
    if lowered:
        return "other"
    return ""


def normalize_amount_to_mg(text: str) -> float | None:
    match = AMOUNT_UNIT_PATTERN.search(normalize_whitespace(text))
    if not match:
        return None

    try:
        amount = float(match.group(1).replace(",", ""))
    except ValueError:
        return None

    unit = UNIT_NORMALIZATION.get(match.group(2).strip().lower(), "")
    if unit == "mg":
        return amount
    if unit == "g":
        return amount * 1000.0
    if unit == "kg":
        return amount * 1_000_000.0
    if unit == "lb":
        return amount * 453_592.37
    if unit == "oz":
        return amount * 28_349.523125
    if unit == "mcg":
        return amount / 1000.0
    return None


def normalize_whitespace(value: str) -> str:
    """Collapse repeated whitespace and trim edges for deterministic comparisons."""
    return " ".join(value.split()).strip()
