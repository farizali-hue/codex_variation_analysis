from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List


_DEFAULT_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "product_type_fields.json"


def normalize_product_type(product_type: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", (product_type or "").strip().lower())
    return normalized.strip("_")


def load_product_type_field_defaults(schema_path: Path | None = None) -> Dict[str, List[str]]:
    path = schema_path or _DEFAULT_SCHEMA_PATH
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as file:
        raw_mapping = json.load(file)

    if not isinstance(raw_mapping, dict):
        raise ValueError(f"Product type schema must be a JSON object: {path}")

    normalized_mapping: Dict[str, List[str]] = {}
    for raw_key, raw_fields in raw_mapping.items():
        key = normalize_product_type(str(raw_key))
        if not key:
            continue
        if not isinstance(raw_fields, list):
            raise ValueError(f"Product type field list must be an array for {raw_key!r}: {path}")
        fields: List[str] = []
        for raw_field in raw_fields:
            field_name = str(raw_field or "").strip()
            if field_name and field_name not in fields:
                fields.append(field_name)
        if fields:
            normalized_mapping[key] = fields

    return normalized_mapping
