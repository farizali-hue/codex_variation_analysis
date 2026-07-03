from __future__ import annotations

import csv
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import run_product_batch
from amazon_scraper.models import ScrapeJob, ScrapeResult
from amazon_scraper.openai_client import OpenAIConfig
from amazon_scraper.scraper import DriverSessionLostError
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


class MainPartialResultsTests(unittest.TestCase):
    """The CLI must salvage completed rows when the batch aborts mid-run.

    run_batch() attaches ``partial_results`` to DriverSessionLostError (and
    ScrapeCancelledError) exactly so callers can persist the rows that already
    cost real browser time and OpenAI spend. Dropping them silently is data loss.
    """

    def setUp(self) -> None:
        self.temp_dir = Path.cwd() / ".test_artifacts" / "main_partial_results"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def test_main_writes_partial_csv_when_driver_session_is_lost(self) -> None:
        output_csv = self.temp_dir / "batch_results.csv"
        partial_result = ScrapeResult(
            asin="B0PARTIAL1",
            product_type="supplement",
            status="OK",
            url="https://www.amazon.com/dp/B0PARTIAL1",
            scraped_at=datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc),
            title="Partial Product",
            fields={},
        )

        class _FakeScraper:
            def __init__(self, **kwargs) -> None:
                del kwargs

            def run_batch(self, jobs, delay_seconds=1.5):
                del jobs, delay_seconds
                error = DriverSessionLostError("chrome not reachable")
                error.partial_results = [partial_result]
                raise error

        config = OpenAIConfig(api_key="k", embedding_model="e", chat_model="c")
        jobs = [ScrapeJob(asin="B0PARTIAL1", product_type="supplement", requested_fields=["packaging_type"])]
        argv = ["run_product_batch.py", "--output-csv", str(output_csv), "--no-console-log"]

        with mock.patch.object(run_product_batch, "configure_logging"), \
                mock.patch.object(run_product_batch, "apply_openai_config_overrides", return_value=config), \
                mock.patch.object(run_product_batch, "load_product_type_field_defaults", return_value={}), \
                mock.patch.object(run_product_batch, "read_jobs", return_value=jobs), \
                mock.patch.object(run_product_batch, "AmazonScraper", _FakeScraper), \
                mock.patch("sys.argv", argv):
            exit_code = run_product_batch.main()

        self.assertNotEqual(exit_code, 0)
        self.assertTrue(output_csv.exists(), "partial results CSV must be written on driver loss")
        with output_csv.open(encoding="utf-8", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual([row["asin"] for row in rows], ["B0PARTIAL1"])


if __name__ == "__main__":
    unittest.main()
