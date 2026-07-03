from __future__ import annotations

from .value_parsers import normalize_whitespace

_METADATA_LABEL_BY_KEY = {
    "serving size": "Serving Size",
    "servings per container": "Servings Per Container",
    "serving per container": "Servings Per Container",
    "servings per package": "Servings Per Container",
    "serving per package": "Servings Per Container",
    "servings per contianer": "Servings Per Container",
    "serving per contianer": "Servings Per Container",
}


def canonical_supplement_metadata_label(label: str) -> str:
    return _METADATA_LABEL_BY_KEY.get(normalize_supplement_metadata_key(label), "")


def normalize_supplement_metadata_key(label: str) -> str:
    return " ".join(
        normalize_whitespace(label).lower().replace("-", " ").replace("_", " ").split()
    )
