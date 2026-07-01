from __future__ import annotations

import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path

from run_product_batch import build_output_path_for_run, read_jobs


class ReadJobsTests(unittest.TestCase):
    def test_read_jobs_normalizes_human_friendly_field_names(self) -> None:
        csv_content = (
            "asin,product_type,required_fields\n"
            'B0DJWV4BQ1,supplement,"packaging, ingredient list"\n'
        )

        temp_dir = Path.cwd() / ".test_artifacts" / "read_jobs_normalized"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            csv_path = temp_dir / "input.csv"
            csv_path.write_text(csv_content, encoding="utf-8")

            jobs = read_jobs(csv_path, product_type_defaults={"supplement": ["serving_size"]})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].requested_fields, ["packaging_type", "ingredients"])

    def test_read_jobs_accepts_custom_fields(self) -> None:
        csv_content = (
            "asin,product_type,required_fields\n"
            'B0DJWV4BQ1,supplement,"totally unknown field"\n'
        )

        temp_dir = Path.cwd() / ".test_artifacts" / "read_jobs_unknown"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            csv_path = temp_dir / "input.csv"
            csv_path.write_text(csv_content, encoding="utf-8")

            jobs = read_jobs(csv_path, product_type_defaults={"supplement": ["serving_size"]})
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].requested_fields, ["totally_unknown_field"])

    def test_build_output_path_for_run_preserves_path_when_detail_flag_is_off(self) -> None:
        output_path = build_output_path_for_run(
            Path("outputs") / "product_batch_results.csv",
            extract_supplement_facts_details=False,
            started_at=datetime(2026, 4, 21, 20, 15, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(output_path, Path("outputs") / "product_batch_results.csv")

    def test_build_output_path_for_run_makes_detail_mode_outputs_unique(self) -> None:
        output_path = build_output_path_for_run(
            Path("outputs") / "product_batch_results.csv",
            extract_supplement_facts_details=True,
            started_at=datetime(2026, 4, 21, 20, 15, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(
            output_path,
            Path("outputs") / "product_batch_results_20260421T201530Z.csv",
        )


if __name__ == "__main__":
    unittest.main()
