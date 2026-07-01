from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from amazon_scraper.csv_export import write_results_csv
from amazon_scraper.input_normalization import (
    build_jobs_from_user_input,
    collect_requested_field_columns,
    split_raw_field_input,
)
from amazon_scraper.logging_utils import configure_logging
from amazon_scraper.models import ScrapeJob
from amazon_scraper.openai_client import OpenAIConfig, OpenAIConfigError, OpenAIBudgetExceededError
from amazon_scraper.product_type_schemas import load_product_type_field_defaults, normalize_product_type
from amazon_scraper.scraper import AmazonScraper

LOGGER = logging.getLogger(__name__)


def parse_requested_fields(raw_value: str) -> List[str]:
    return split_raw_field_input(raw_value)


def resolve_requested_fields(
    *,
    product_type: str,
    explicit_fields: List[str],
    product_type_defaults: Dict[str, List[str]],
) -> List[str]:
    if explicit_fields:
        return explicit_fields
    product_type_key = normalize_product_type(product_type)
    return list(product_type_defaults.get(product_type_key, []))


def read_jobs(input_csv: Path, product_type_defaults: Dict[str, List[str]] | None = None) -> List[ScrapeJob]:
    """Load scrape jobs from CSV rows with asin plus requested fields or product-type defaults."""
    jobs: List[ScrapeJob] = []
    defaults = product_type_defaults or {}
    with input_csv.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row_number, row in enumerate(reader, start=2):
            asin = (row.get("asin") or "").strip()
            product_type = (row.get("product_type") or "").strip()
            requested_fields = resolve_requested_fields(
                product_type=product_type,
                explicit_fields=parse_requested_fields(row.get("required_fields") or row.get("fields") or ""),
                product_type_defaults=defaults,
            )
            build_result = build_jobs_from_user_input(
                raw_asins=[asin],
                raw_fields=requested_fields,
                product_type=product_type,
                product_type_defaults=defaults,
            )
            for issue in build_result.issues:
                log_method = LOGGER.warning if issue.level == "warning" else LOGGER.error
                log_method(
                    "Input row validation row=%d asin=%r product_type=%r detail=%s",
                    row_number,
                    asin,
                    product_type,
                    issue.message,
                )
            if not build_result.jobs:
                LOGGER.warning(
                    "Skipping invalid input row=%d asin=%r product_type=%r requested_fields=%r",
                    row_number,
                    asin,
                    product_type,
                    requested_fields,
                )
                continue
            jobs.extend(build_result.jobs)
    return jobs


def apply_openai_config_overrides(args: argparse.Namespace) -> OpenAIConfig:
    config = OpenAIConfig.from_env()
    overridden = replace(
        config,
        max_cost_usd=_resolve_optional_non_negative("--max-cost-usd", args.max_cost_usd, config.max_cost_usd),
        embedding_cost_per_1m_tokens=_resolve_optional_non_negative(
            "--embedding-cost-per-1m-tokens",
            args.embedding_cost_per_1m_tokens,
            config.embedding_cost_per_1m_tokens,
        ),
        chat_input_cost_per_1m_tokens=_resolve_optional_non_negative(
            "--chat-input-cost-per-1m-tokens",
            args.chat_input_cost_per_1m_tokens,
            config.chat_input_cost_per_1m_tokens,
        ),
        chat_output_cost_per_1m_tokens=_resolve_optional_non_negative(
            "--chat-output-cost-per-1m-tokens",
            args.chat_output_cost_per_1m_tokens,
            config.chat_output_cost_per_1m_tokens,
        ),
    )
    overridden.validate_cost_controls()
    return overridden


def _resolve_optional_non_negative(name: str, cli_value: float | None, env_value: float | None) -> float | None:
    value = env_value if cli_value is None else cli_value
    if value is None:
        return None
    if value < 0:
        raise OpenAIConfigError(f"{name} must be >= 0")
    return value


def _format_optional_usd(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{value:.6f}"


def log_cost_controls(config: OpenAIConfig) -> None:
    if not config.has_cost_estimation() and config.max_cost_usd is None:
        return
    LOGGER.info(
        "OpenAI cost controls enabled embedding_cost_per_1m_tokens=%.6f chat_input_cost_per_1m_tokens=%.6f chat_output_cost_per_1m_tokens=%.6f max_cost_usd=%s",
        config.embedding_cost_per_1m_tokens or 0.0,
        config.chat_input_cost_per_1m_tokens or 0.0,
        config.chat_output_cost_per_1m_tokens or 0.0,
        _format_optional_usd(config.max_cost_usd),
    )


def build_output_path_for_run(
    output_path: Path | str,
    *,
    extract_supplement_facts_details: bool,
    started_at: datetime | None = None,
) -> Path:
    resolved = Path(output_path)
    if not extract_supplement_facts_details:
        return resolved

    timestamp = (started_at or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    suffix = timestamp.strftime("%Y%m%dT%H%M%SZ")
    return resolved.with_name(f"{resolved.stem}_{suffix}{resolved.suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Amazon product scraper batch.")
    parser.add_argument("--input-csv", default="inputs/sample_product_asins.csv")
    parser.add_argument("--output-csv", default="outputs/product_batch_results.csv")
    parser.add_argument(
        "--extract-supplement-facts-details",
        action="store_true",
        help="Parse and flatten Supplement Facts details into run-scoped metadata and ingredient columns.",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument("--delay-seconds", type=float, default=1.5)
    parser.add_argument("--log-level", default="DEBUG", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--log-file", default="logs/scraper.log", help="File path to write logs.")
    parser.add_argument(
        "--debug-html-dir",
        default="logs/html_snapshots",
        help="Directory where final per-ASIN HTML snapshots are stored for debugging.",
    )
    parser.add_argument("--no-console-log", action="store_true", help="Disable terminal log output.")
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        help="Stop the batch after estimated OpenAI spend reaches this USD amount.",
    )
    parser.add_argument(
        "--embedding-cost-per-1m-tokens",
        type=float,
        help="Embedding input price in USD per 1M tokens.",
    )
    parser.add_argument(
        "--chat-input-cost-per-1m-tokens",
        type=float,
        help="Chat prompt price in USD per 1M tokens.",
    )
    parser.add_argument(
        "--chat-output-cost-per-1m-tokens",
        type=float,
        help="Chat completion price in USD per 1M tokens.",
    )
    args = parser.parse_args()

    configure_logging(
        level=args.log_level,
        log_file=args.log_file,
        console=not args.no_console_log,
    )

    export_columns: List[str] = []
    try:
        input_csv = Path(args.input_csv)
        openai_config = apply_openai_config_overrides(args)
        log_cost_controls(openai_config)
        product_type_defaults = load_product_type_field_defaults()
        LOGGER.info("Loaded product type schemas count=%d", len(product_type_defaults))
        jobs = read_jobs(input_csv, product_type_defaults=product_type_defaults)
        if not jobs:
            raise RuntimeError(f"No valid jobs found in {input_csv}")
        LOGGER.info("Loaded %d jobs from %s", len(jobs), input_csv)

        export_columns = collect_requested_field_columns(jobs)
        output_path = build_output_path_for_run(
            args.output_csv,
            extract_supplement_facts_details=args.extract_supplement_facts_details,
        )
        scraper = AmazonScraper(
            headless=args.headless,
            openai_config=openai_config,
            debug_html_dir=args.debug_html_dir,
            extract_supplement_facts_details=args.extract_supplement_facts_details,
        )
        LOGGER.info(
            "Starting batch run jobs=%d headless=%s delay_seconds=%.2f export_columns=%d console_log=%s extract_supplement_facts_details=%s",
            len(jobs),
            args.headless,
            args.delay_seconds,
            len(export_columns),
            str(not args.no_console_log).lower(),
            str(args.extract_supplement_facts_details).lower(),
        )
        results = scraper.run_batch(jobs, delay_seconds=args.delay_seconds)
        output_path = write_results_csv(
            results=results,
            output_file=output_path,
            export_columns=export_columns,
            extract_supplement_facts_details=args.extract_supplement_facts_details,
        )

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
    except OpenAIBudgetExceededError as exc:
        partial_results = getattr(exc, "partial_results", [])
        if partial_results:
            output_path = build_output_path_for_run(
                args.output_csv,
                extract_supplement_facts_details=args.extract_supplement_facts_details,
            )
            output_path = write_results_csv(
                results=partial_results,
                output_file=output_path,
                export_columns=export_columns,
                extract_supplement_facts_details=args.extract_supplement_facts_details,
            )
            LOGGER.error(
                "Batch stopped at configured OpenAI spend limit estimated_cost_usd=%.6f max_cost_usd=%.6f partial_rows=%d output=%s",
                exc.estimated_cost_usd,
                exc.max_cost_usd,
                len(partial_results),
                output_path,
            )
        else:
            LOGGER.error(
                "Batch stopped at configured OpenAI spend limit estimated_cost_usd=%.6f max_cost_usd=%.6f before any rows completed",
                exc.estimated_cost_usd,
                exc.max_cost_usd,
            )
        return 2
    except OpenAIConfigError:
        LOGGER.exception("OpenAI configuration is invalid")
        return 2
    except Exception:
        LOGGER.exception("Batch run failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
