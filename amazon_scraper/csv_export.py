from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable

from .models import ScrapeResult

LOGGER = logging.getLogger(__name__)


def write_results_csv(
    results: Iterable[ScrapeResult],
    output_file: Path | str,
    export_columns: list[str] | None = None,
) -> Path:
    """Write normalized scrape output with value/source/confidence columns for all requested fields."""
    result_list = list(results)
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    columns = export_columns or _collect_export_columns(result_list)

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

    with output_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for result in result_list:
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
            writer.writerow(row)

    LOGGER.info("CSV export complete rows=%d path=%s", len(result_list), output_path)
    return output_path


def _collect_export_columns(results: list[ScrapeResult]) -> list[str]:
    columns: list[str] = []
    for result in results:
        for field_name in result.fields.keys():
            if field_name not in columns:
                columns.append(field_name)
    return columns
