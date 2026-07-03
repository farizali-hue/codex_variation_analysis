"""Consolidate the rich long-form ledger into canonical ingredient columns.

Reads `*_supplement_facts_rows.csv` (the lossless ledger, which keeps amount_text,
category, parent_blend, provenance — see csv_export._build_supplement_facts_rows_export),
canonicalizes each ingredient name via an injected canonicalizer, and pivots into a wide
table of canonical (base-nutrient) columns. Items the canonicalizer can't confidently place
go to a `*_needs_review.csv` instead of being guessed.

The canonicalizer is injected (real engine in the runner; a stub in tests), so this module
has no LLM dependency and is fully unit-testable.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Protocol

LOGGER = logging.getLogger(__name__)

_DEFAULT_VALUE_FIELDS = ("amount_mg", "amount_text")
_REVIEW_FIELDS = ["asin", "type", "raw_name", "canonical_column", "reason", "value", "evidence"]


class Canonicalizer(Protocol):
    def canonicalize(self, raw_name: str, *, context: str = "") -> object: ...


def consolidate_from_ledger(
    rows_csv: Path | str,
    *,
    canonicalizer: Canonicalizer,
    output_csv: Path | str | None = None,
    review_csv: Path | str | None = None,
    value_fields: tuple[str, ...] = _DEFAULT_VALUE_FIELDS,
) -> tuple[Path, Path, dict[str, int]]:
    """Return (output_path, review_path, stats)."""
    input_path = Path(rows_csv)
    output_path = Path(output_csv) if output_csv else _default_output_path(input_path)
    review_path = Path(review_csv) if review_csv else output_path.with_name(f"{output_path.stem}_needs_review.csv")

    with input_path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))

    asin_order: list[str] = []
    columns: list[str] = []
    wide: dict[str, dict[str, str]] = {}
    review_rows: list[dict[str, str]] = []
    llm_decisions = 0

    for row in rows:
        asin = (row.get("asin", "") or "").strip()
        name = (row.get("name", "") or "").strip()
        if not name:
            continue
        if asin not in wide:
            wide[asin] = {}
            asin_order.append(asin)

        value = _first_value(row, value_fields)
        evidence = (row.get("evidence", "") or "").strip() or name
        decision = canonicalizer.canonicalize(name, context=evidence)
        if getattr(decision, "method", "") not in ("cache", ""):
            llm_decisions += 1

        if getattr(decision, "needs_review", False):
            review_rows.append({
                "asin": asin, "type": "unresolved_ingredient", "raw_name": name,
                "canonical_column": getattr(decision, "suggestion", "") or "",
                "reason": getattr(decision, "review_reason", "") or "", "value": value, "evidence": evidence,
            })
            continue

        canonical = getattr(decision, "canonical", "") or ""
        if not canonical:
            continue
        if canonical not in columns:
            columns.append(canonical)

        existing = wide[asin].get(canonical, "")
        if existing and value and existing != value:
            review_rows.append({
                "asin": asin, "type": "value_collision", "raw_name": name,
                "canonical_column": canonical, "reason": f"{existing} != {value}",
                "value": value, "evidence": evidence,
            })
            continue
        if value and not existing:
            wide[asin][canonical] = value
        else:
            wide[asin].setdefault(canonical, existing)

    _write_wide(output_path, asin_order, columns, wide)
    _write_review(review_path, review_rows)

    stats = {
        "asins": len(asin_order), "canonical_columns": len(columns),
        "review_items": len(review_rows), "llm_or_new_decisions": llm_decisions,
    }
    LOGGER.info("ledger consolidation done %s -> %s (%s)", input_path, output_path, stats)
    return output_path, review_path, stats


def _default_output_path(input_path: Path) -> Path:
    stem = input_path.stem.replace("_supplement_facts_rows", "")
    return input_path.with_name(f"{stem}_canonical.csv")


def _first_value(row: dict[str, str], value_fields: tuple[str, ...]) -> str:
    for field_name in value_fields:
        value = (row.get(field_name, "") or "").strip()
        if value:
            return value
    return ""


def _write_wide(path: Path, asin_order: list[str], columns: list[str], wide: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["asin", *columns]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for asin in asin_order:
            record = {"asin": asin}
            for column in columns:
                record[column] = wide[asin].get(column, "")
            writer.writerow(record)


def _write_review(path: Path, review_rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=_REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(review_rows)
