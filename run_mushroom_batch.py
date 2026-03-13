from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path
from typing import List

from amazon_scraper.csv_export import write_results_csv
from amazon_scraper.logging_utils import configure_logging
from amazon_scraper.models import ScrapeJob
from amazon_scraper.openai_client import OpenAIConfig, OpenAIConfigError
from amazon_scraper.scraper import AmazonScraper

LOGGER = logging.getLogger(__name__)


def parse_requested_fields(raw_value: str) -> List[str]:
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return []
    separators = ["|", ";", ","]
    values = [cleaned]
    for separator in separators:
        if separator in cleaned:
            values = [item.strip() for item in cleaned.split(separator)]
            break
    return [value for value in values if value]


def read_jobs(input_csv: Path) -> List[ScrapeJob]:
    """Load scrape jobs from CSV rows with asin plus requested fields."""
    jobs: List[ScrapeJob] = []
    with input_csv.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row_number, row in enumerate(reader, start=2):
            asin = (row.get("asin") or "").strip()
            product_type = (row.get("product_type") or "").strip()
            requested_fields = parse_requested_fields(row.get("required_fields") or row.get("fields") or "")
            if not asin or not requested_fields:
                LOGGER.warning(
                    "Skipping invalid input row=%d asin=%r product_type=%r requested_fields=%r",
                    row_number,
                    asin,
                    product_type,
                    requested_fields,
                )
                continue
            jobs.append(ScrapeJob(asin=asin, product_type=product_type, requested_fields=requested_fields))
    return jobs


def collect_export_columns(jobs: List[ScrapeJob]) -> List[str]:
    columns: List[str] = []
    for job in jobs:
        for column in job.requested_fields:
            if column not in columns:
                columns.append(column)
    return columns


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Amazon mushroom coffee scraper batch.")
    parser.add_argument("--input-csv", default="inputs/mushroom_coffee_asins.csv")
    parser.add_argument("--output-csv", default="outputs/mushroom_coffee_results.csv")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--delay-seconds", type=float, default=1.5)
    parser.add_argument("--log-level", default="DEBUG", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-file", default="logs/scraper.log", help="File path to write logs.")
    parser.add_argument("--no-console-log", action="store_true", help="Disable terminal log output.")
    args = parser.parse_args()

    configure_logging(
        level=args.log_level,
        log_file=args.log_file,
        console=not args.no_console_log,
    )

    try:
        input_csv = Path(args.input_csv)
        openai_config = OpenAIConfig.from_env()
        jobs = read_jobs(input_csv)
        if not jobs:
            raise RuntimeError(f"No valid jobs found in {input_csv}")
        LOGGER.info("Loaded %d jobs from %s", len(jobs), input_csv)

        export_columns = collect_export_columns(jobs)
        scraper = AmazonScraper(
            headless=args.headless,
            openai_config=openai_config,
        )
        LOGGER.info(
            "Starting batch run jobs=%d headless=%s delay_seconds=%.2f export_columns=%d",
            len(jobs),
            args.headless,
            args.delay_seconds,
            len(export_columns),
        )
        results = scraper.run_batch(jobs, delay_seconds=args.delay_seconds)
        output_path = write_results_csv(results=results, output_file=args.output_csv, export_columns=export_columns)

        ok_count = sum(1 for result in results if result.status == "OK")
        partial_count = sum(1 for result in results if result.status == "PARTIAL")
        failed_count = len(results) - ok_count - partial_count
        LOGGER.info(
            "Batch completed total=%d ok=%d partial=%d failed=%d output=%s",
            len(results),
            ok_count,
            partial_count,
            failed_count,
            output_path,
        )
        return 0
    except OpenAIConfigError:
        LOGGER.exception("Missing required OpenAI configuration")
        return 1
    except Exception:
        LOGGER.exception("Batch run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
