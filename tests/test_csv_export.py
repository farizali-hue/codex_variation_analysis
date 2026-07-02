from __future__ import annotations

import csv
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path

from amazon_scraper.csv_export import write_results_csv
from amazon_scraper.models import FieldResult, ScrapeResult


class ReservedColumnCollisionTests(unittest.TestCase):
    """A requested field that resolves to a reserved export name is rejected at
    intake, but export_columns / result keys can also arrive programmatically;
    the exporter must never emit duplicate headers or overwrite row metadata."""

    def _write_and_read(
        self,
        results: list[ScrapeResult],
        *,
        extract_supplement_facts_details: bool = False,
    ) -> tuple[list[str], list[dict[str, str]]]:
        temp_dir = Path.cwd() / ".test_artifacts" / "csv_export_reserved"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            output_path = temp_dir / "results.csv"
            write_results_csv(
                results=results,
                output_file=output_path,
                extract_supplement_facts_details=extract_supplement_facts_details,
            )
            # csv.reader (not DictReader) so duplicate headers stay visible.
            with output_path.open("r", encoding="utf-8", newline="") as file:
                raw_rows = list(csv.reader(file))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        header = raw_rows[0]
        rows = [dict(zip(header, row)) for row in raw_rows[1:]]
        return header, rows

    def _result(self, fields: dict[str, FieldResult]) -> ScrapeResult:
        return ScrapeResult(
            asin="B0REALASIN",
            product_type="supplement",
            status="OK",
            url="https://example.com/real",
            scraped_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
            title="Real Title",
            error=None,
            fields=fields,
        )

    def test_legacy_export_remaps_field_columns_that_collide_with_metadata(self) -> None:
        results = [
            self._result(
                {
                    "asin": FieldResult(value="FIELD_ASIN", source="html", confidence=0.9, evidence="ev-a"),
                    "error": FieldResult(value="FIELD_ERROR", source="image", confidence=0.7, evidence="ev-e"),
                    "flavor": FieldResult(value="Vanilla", source="html", confidence=0.8, evidence="ev-f"),
                }
            )
        ]

        header, rows = self._write_and_read(results)

        self.assertEqual(len(header), len(set(header)))
        self.assertEqual(rows[0]["asin"], "B0REALASIN")
        self.assertEqual(rows[0]["title"], "Real Title")
        self.assertEqual(rows[0]["error"], "")
        self.assertEqual(rows[0]["custom_asin"], "FIELD_ASIN")
        self.assertEqual(rows[0]["custom_asin_source"], "html")
        self.assertEqual(rows[0]["custom_asin_confidence"], "0.90")
        self.assertEqual(rows[0]["custom_asin_evidence"], "ev-a")
        self.assertEqual(rows[0]["custom_error"], "FIELD_ERROR")
        self.assertEqual(rows[0]["flavor"], "Vanilla")

    def test_detail_mode_remaps_extra_columns_that_collide_with_metadata(self) -> None:
        results = [
            self._result(
                {
                    "title": FieldResult(value="FIELD_TITLE", source="html", confidence=0.9, evidence="ev-t"),
                    "flavor": FieldResult(value="Chocolate", source="html", confidence=0.8, evidence="ev-f"),
                }
            )
        ]

        header, rows = self._write_and_read(results, extract_supplement_facts_details=True)

        self.assertEqual(len(header), len(set(header)))
        self.assertEqual(rows[0]["asin"], "B0REALASIN")
        self.assertEqual(rows[0]["title"], "Real Title")
        self.assertEqual(rows[0]["custom_title"], "FIELD_TITLE")
        self.assertEqual(rows[0]["flavor"], "Chocolate")


if __name__ == "__main__":
    unittest.main()
