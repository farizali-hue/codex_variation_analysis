from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable

from .models import ScrapeResult, SupplementFactsDetail
from .supplement_facts_contract import canonical_supplement_metadata_label, normalize_supplement_metadata_key
from .value_parsers import normalize_whitespace

LOGGER = logging.getLogger(__name__)

_DETAIL_MODE_HANDLED_FIELD_KEYS = {"ingredients", "serving_size", "servings_per_container"}


def write_results_csv(
    results: Iterable[ScrapeResult],
    output_file: Path | str,
    export_columns: list[str] | None = None,
    *,
    extract_supplement_facts_details: bool = False,
) -> Path:
    """Write normalized scrape output using the legacy or supplement-facts-detail schema."""
    result_list = list(results)
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if extract_supplement_facts_details:
        fieldnames, rows = _build_detail_mode_export(result_list, export_columns=export_columns)
    else:
        fieldnames, rows = _build_legacy_export(result_list, export_columns=export_columns)

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    if extract_supplement_facts_details:
        _write_supplement_facts_rows_csv(result_list, _supplement_facts_rows_output_path(output_path))

    LOGGER.info(
        "CSV export complete rows=%d path=%s extract_supplement_facts_details=%s",
        len(result_list),
        output_path,
        str(extract_supplement_facts_details).lower(),
    )
    return output_path


def _supplement_facts_rows_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}_supplement_facts_rows{output_path.suffix}")


def _write_supplement_facts_rows_csv(results: list[ScrapeResult], output_path: Path) -> None:
    fieldnames, rows = _build_supplement_facts_rows_export(results)
    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _build_supplement_facts_rows_export(results: list[ScrapeResult]) -> tuple[list[str], list[dict[str, str]]]:
    fieldnames = [
        "asin",
        "row_order",
        "name",
        "amount_text",
        "amount_mg",
        "daily_value_text",
        "footnote_markers",
        "notes",
        "category",
        "parent_blend_name",
        "source",
        "confidence",
        "evidence",
    ]
    rows: list[dict[str, str]] = []
    for result in results:
        detail = result.supplement_facts_detail
        if detail is None:
            continue
        for row_order, ingredient in enumerate(detail.ingredients, start=1):
            rows.append(
                {
                    "asin": result.asin,
                    "row_order": str(row_order),
                    "name": ingredient.name,
                    "amount_text": ingredient.raw_amount_text,
                    "amount_mg": _format_optional_number(ingredient.normalized_amount_mg),
                    "daily_value_text": ingredient.daily_value_text,
                    "footnote_markers": " | ".join(ingredient.footnote_markers),
                    "notes": ingredient.notes,
                    "category": ingredient.category,
                    "parent_blend_name": ingredient.parent_blend_name,
                    "source": detail.source,
                    "confidence": f"{detail.confidence:.2f}" if detail.confidence else "",
                    "evidence": detail.evidence,
                }
            )
    return fieldnames, rows


def build_result_rows(
    results: Iterable[ScrapeResult],
    *,
    export_columns: list[str] | None = None,
    extract_supplement_facts_details: bool = False,
) -> tuple[list[str], list[dict[str, str]]]:
    result_list = list(results)
    if extract_supplement_facts_details:
        return _build_detail_mode_export(result_list, export_columns=export_columns)
    return _build_legacy_export(result_list, export_columns=export_columns)


def _build_legacy_export(
    results: list[ScrapeResult],
    *,
    export_columns: list[str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    columns = export_columns or _collect_export_columns(results)
    fieldnames = [
        "asin",
        "product_type",
        "status",
        "url",
        "scraped_at",
        "title",
    ]
    for column in columns:
        fieldnames.append(column)
        fieldnames.append(f"{column}_source")
        fieldnames.append(f"{column}_confidence")
        fieldnames.append(f"{column}_evidence")
    fieldnames.append("error")

    rows: list[dict[str, str]] = []
    for result in results:
        row = {
            "asin": result.asin,
            "product_type": result.product_type,
            "status": result.status,
            "url": result.url,
            "scraped_at": result.scraped_at.isoformat(),
            "title": result.title,
            "error": result.error or "",
        }
        for column in columns:
            field_result = result.fields.get(column)
            row[column] = field_result.value if field_result else ""
            row[f"{column}_source"] = field_result.source if field_result else ""
            row[f"{column}_confidence"] = f"{field_result.confidence:.2f}" if field_result else ""
            row[f"{column}_evidence"] = field_result.evidence if field_result else ""
        rows.append(row)
    return fieldnames, rows


def _build_detail_mode_export(
    results: list[ScrapeResult],
    *,
    export_columns: list[str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    metadata_columns = _collect_detail_metadata_columns(results)
    ingredient_columns = _collect_detail_ingredient_columns(results)
    extra_columns = _collect_detail_extra_columns(results, export_columns=export_columns)
    fieldnames = [
        "asin",
        "product_type",
        "status",
        "url",
        "scraped_at",
        "title",
        "error",
        "Other ingredients",
        "unparsed_ingredients",
        *metadata_columns,
        *ingredient_columns,
        *extra_columns,
    ]
    metadata_column_by_key = {
        normalize_supplement_metadata_key(column): column
        for column in metadata_columns
    }
    ingredient_column_by_key = {
        _normalize_ingredient_key(column): column
        for column in ingredient_columns
    }

    rows: list[dict[str, str]] = []
    for result in results:
        row = {field_name: "" for field_name in fieldnames}
        row.update(
            {
                "asin": result.asin,
                "product_type": result.product_type,
                "status": result.status,
                "url": result.url,
                "scraped_at": result.scraped_at.isoformat(),
                "title": result.title,
                "error": result.error or "",
            }
        )
        detail = result.supplement_facts_detail
        if detail is not None:
            row["Other ingredients"] = detail.other_ingredients
            row["unparsed_ingredients"] = " | ".join(detail.unparsed_fragments)
            for metadata_entry in detail.metadata:
                canonical_label = canonical_supplement_metadata_label(metadata_entry.label)
                column_name = metadata_column_by_key.get(normalize_supplement_metadata_key(canonical_label))
                if column_name and not row[column_name]:
                    row[column_name] = metadata_entry.final_csv_value
            for ingredient in detail.ingredients:
                column_name = ingredient_column_by_key.get(_normalize_ingredient_key(ingredient.name))
                if column_name and ingredient.csv_value and not row[column_name]:
                    row[column_name] = ingredient.csv_value
        for column in extra_columns:
            field_result = result.fields.get(column)
            row[column] = field_result.value if field_result else ""
        rows.append(row)
    return fieldnames, rows


def _collect_export_columns(results: list[ScrapeResult]) -> list[str]:
    columns: list[str] = []
    for result in results:
        for field_name in result.fields.keys():
            if field_name not in columns:
                columns.append(field_name)
    return columns


def _collect_detail_metadata_columns(results: list[ScrapeResult]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for result in results:
        detail = result.supplement_facts_detail
        if detail is None:
            continue
        for entry in detail.metadata:
            column = canonical_supplement_metadata_label(entry.label)
            key = normalize_supplement_metadata_key(column)
            if not key or key in seen:
                continue
            seen.add(key)
            columns.append(column)
    return columns


def _collect_detail_ingredient_columns(results: list[ScrapeResult]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for result in results:
        detail = result.supplement_facts_detail
        if detail is None:
            continue
        for ingredient in detail.ingredients:
            key = _normalize_ingredient_key(ingredient.name)
            if not key or key in seen:
                continue
            seen.add(key)
            columns.append(ingredient.name)
    return columns


def _collect_detail_extra_columns(
    results: list[ScrapeResult],
    *,
    export_columns: list[str] | None = None,
) -> list[str]:
    requested_columns = export_columns or _collect_export_columns(results)
    columns: list[str] = []
    for column in requested_columns:
        normalized = normalize_whitespace(column)
        if not normalized or normalized in _DETAIL_MODE_HANDLED_FIELD_KEYS:
            continue
        if normalized not in columns:
            columns.append(normalized)
    return columns


def _normalize_ingredient_key(value: str) -> str:
    return " ".join(normalize_whitespace(value).lower().split())


def _format_optional_number(value: float | None) -> str:
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")
