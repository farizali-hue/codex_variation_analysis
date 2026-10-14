"""End-to-end batch orchestrator for Amazon PDP extraction.

This module owns the real runtime flow:
- load one PDP with Selenium
- extract HTML text sections and gallery metadata
- try image-first field resolution
- fall back to HTML retrieval and chat extraction
- assemble the final ScrapeResult

If you are learning the codebase, start with AmazonScraper.run_batch() and
AmazonScraper.run_job().
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from urllib3.exceptions import MaxRetryError

from .chunking import build_chunk_records
from .contract_value_normalization import normalize_field_result_for_contract
from .custom_field_presets import apply_custom_field_preset, load_custom_field_presets
from .extraction import OpenAIFieldExtractor, build_query_text_for_contract, resolve_field_from_facts_rows, resolve_field_from_text_chunks
from .feature_flags import (
    run_scoped_schema_enabled as read_run_scoped_schema_enabled,
    run_scoped_text_merge_mode as read_run_scoped_text_merge_mode,
)
from .gallery_images import extract_gallery_images
from .html_extractors import (
    detect_page_state,
    extract_bullets,
    extract_description_text,
    extract_facts_table,
    extract_title,
    extract_variations,
)
from .models import RunFieldContract, ScrapeJob, ScrapeResult
from .openai_client import OpenAIClient, OpenAIConfig, OpenAIBudgetExceededError, OpenAIRunCancelledError
from .retrieval import DenseRetriever, EmbeddingCache
from .run_field_contracts import get_job_field_contract_map, resolve_job_requested_fields
from .run_scoped_canonicalization import RunScopedCanonicalizer
from .vision_extraction import VisionFieldResolver

LOGGER = logging.getLogger(__name__)


ProgressCallback = Callable[[str, int, int, str], None]


class ScrapeCancelledError(RuntimeError):
    def __init__(self, message: str = "Batch run cancelled") -> None:
        self.partial_results: list[ScrapeResult] = []
        super().__init__(message)


class DriverSessionLostError(RuntimeError):
    def __init__(self, message: str = "Browser session lost") -> None:
        self.partial_results: list[ScrapeResult] = []
        super().__init__(message)


class AmazonScraper:
    """Selenium-backed scraper with OpenAI retrieval/extraction over Amazon PDP content."""

    _CAPTCHA_MARKERS = (
        "enter the characters you see below",
        "type the characters you see in this image",
    )
    _NOT_FOUND_MARKERS = (
        "sorry! we couldn't find that page",
        "dogs of amazon",
    )
    _EXPANDABLE_SECTION_LABELS = (
        "ingredients",
        "top highlights",
        "item details",
        "product details",
        "product information",
        "important information",
    )
    _EXPANDABLE_SECTION_HINTS = (
        "productfacts",
        "ingredient",
        "highlight",
        "itemdetail",
        "proddetails",
        "detailbullets",
        "technicalspecification",
        "productoverview",
    )

    def __init__(
        self,
        base_url: str = "https://www.amazon.com",
        timeout_seconds: int = 20,
        headless: bool = False,
        delivery_zip: str = "10001",
        captcha_timeout_seconds: int = 180,
        embedding_cache_file: Path | str = ".cache/embeddings.sqlite3",
        debug_html_dir: Path | str | None = "logs/html_snapshots",
        openai_config: OpenAIConfig | None = None,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
        run_scoped_schema_enabled: bool | None = None,
        run_scoped_text_merge_mode: str | None = None,
        extract_supplement_facts_details: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headless = headless
        self.delivery_zip = delivery_zip.strip()
        self.captcha_timeout_seconds = captcha_timeout_seconds
        self.debug_html_dir = Path(debug_html_dir) if debug_html_dir else None
        self._prepared_sessions: set[str] = set()
        self.cancel_event = cancel_event or threading.Event()
        self.progress_callback = progress_callback
        self._driver_lock = threading.Lock()
        self._active_driver: webdriver.Chrome | None = None
        self.run_scoped_schema_enabled = (
            read_run_scoped_schema_enabled(default=False)
            if run_scoped_schema_enabled is None
            else run_scoped_schema_enabled
        )
        self.run_scoped_text_merge_mode = (
            read_run_scoped_text_merge_mode(default="off")
            if run_scoped_text_merge_mode is None
            else _normalize_text_merge_mode(run_scoped_text_merge_mode)
        )
        self.extract_supplement_facts_details = bool(extract_supplement_facts_details)
        self.openai_config = openai_config or OpenAIConfig.from_env()
        self.openai_client = OpenAIClient(self.openai_config, cancel_event=self.cancel_event)
        self.embedding_cache = EmbeddingCache(embedding_cache_file)
        self.retriever = DenseRetriever(
            client=self.openai_client,
            embedding_model=self.openai_config.embedding_model,
            cache=self.embedding_cache,
        )
        self.extractor = OpenAIFieldExtractor(self.openai_client)
        self.vision_resolver = VisionFieldResolver(
            client=self.openai_client,
            model=self.openai_config.resolved_vision_model(),
        )
        self.run_scoped_canonicalizer = RunScopedCanonicalizer(
            client=self.openai_client,
            embedding_model=self.openai_config.embedding_model,
            embedding_cache=self.embedding_cache,
        )
        self._run_scope_active = False
        self.custom_field_presets = load_custom_field_presets()

    def run_batch(self, jobs: Iterable[ScrapeJob], delay_seconds: float = 1.5) -> List[ScrapeResult]:
        """Run jobs with a shared browser session to reduce startup cost."""
        job_list = list(jobs)
        fields_requested_total = sum(len(self._resolve_requested_fields(job)) for job in job_list)
        self.openai_client.reset_batch_usage()
        self._begin_run_scope()
        LOGGER.info(
            "Starting batch scrape jobs=%d delay_seconds=%.2f run_scoped_schema_enabled=%s run_scoped_text_merge_mode=%s",
            len(job_list),
            delay_seconds,
            str(self.run_scoped_schema_enabled).lower(),
            self.run_scoped_text_merge_mode,
        )
        driver = self._build_driver()
        self._set_active_driver(driver)
        results: List[ScrapeResult] = []
        try:
            self._notify_progress("starting", 0, len(job_list), "")
            self._raise_if_cancelled()
            self._prepare_session(driver)
            for index, job in enumerate(job_list, start=1):
                self._raise_if_cancelled()
                self._notify_progress("job_started", index - 1, len(job_list), job.asin)
                LOGGER.info("Running job %d/%d asin=%s product_type=%s", index, len(job_list), job.asin, job.product_type)
                results.append(self.run_job(job, driver))
                self._notify_progress("job_finished", len(results), len(job_list), job.asin)
                self._sleep_with_cancel(delay_seconds)
        except OpenAIBudgetExceededError as exc:
            self._finalize_run_scoped_results(job_list[: len(results)], results)
            exc.partial_results = list(results)
            LOGGER.error(
                "Stopping batch because estimated OpenAI cost reached the configured limit estimated_cost_usd=%.6f max_cost_usd=%.6f completed_jobs=%d total_jobs=%d",
                exc.estimated_cost_usd,
                exc.max_cost_usd,
                len(results),
                len(job_list),
            )
            self._log_batch_api_usage(
                jobs_total=len(job_list),
                jobs_completed=len(results),
                fields_requested_total=fields_requested_total,
                stopped=True,
            )
            raise
        except ScrapeCancelledError as exc:
            self._finalize_run_scoped_results(job_list[: len(results)], results)
            exc.partial_results = list(results)
            LOGGER.warning(
                "Batch cancelled completed_jobs=%d total_jobs=%d",
                len(results),
                len(job_list),
            )
            self._log_batch_api_usage(
                jobs_total=len(job_list),
                jobs_completed=len(results),
                fields_requested_total=fields_requested_total,
                stopped=True,
            )
            self._notify_progress("cancelled", len(results), len(job_list), "")
            raise
        except DriverSessionLostError as exc:
            self._finalize_run_scoped_results(job_list[: len(results)], results)
            exc.partial_results = list(results)
            LOGGER.error(
                "Stopping batch because the WebDriver session was lost completed_jobs=%d total_jobs=%d error=%s",
                len(results),
                len(job_list),
                exc,
            )
            self._log_batch_api_usage(
                jobs_total=len(job_list),
                jobs_completed=len(results),
                fields_requested_total=fields_requested_total,
                stopped=True,
            )
            self._notify_progress("driver_lost", len(results), len(job_list), "")
            raise
        else:
            self._finalize_run_scoped_results(job_list, results)
            LOGGER.info("Batch scrape finished jobs=%d", len(results))
            self._log_batch_api_usage(
                jobs_total=len(job_list),
                jobs_completed=len(results),
                fields_requested_total=fields_requested_total,
                stopped=False,
            )
            self._notify_progress("completed", len(results), len(job_list), "")
            return results
        finally:
            self._set_active_driver(None)
            LOGGER.debug("Closing shared WebDriver session")
            try:
                driver.quit()
            except Exception:
                LOGGER.debug("Ignoring WebDriver shutdown error during batch cleanup", exc_info=True)
            self._end_run_scope()

    def run_job(self, job: ScrapeJob, driver: webdriver.Chrome | None = None) -> ScrapeResult:
        """Execute one scrape job and return normalized field outputs."""
        requested_fields = self._resolve_requested_fields(job)
        field_contract_map = get_job_field_contract_map(
            job,
            run_scoped_schema_enabled=self.run_scoped_schema_enabled,
        )
        self.openai_client.reset_job_usage()
        started_run_scope = False
        if self.run_scoped_schema_enabled and not self._run_scope_active:
            self._begin_run_scope()
            started_run_scope = True
        own_driver = driver is None
        if own_driver:
            LOGGER.debug("No driver provided; creating job-scoped WebDriver asin=%s", job.asin)
            driver = self._build_driver()
            self._set_active_driver(driver)

        assert driver is not None
        try:
            self._raise_if_cancelled()
            self._log_job_contract_metadata(job, requested_fields, field_contract_map)
            self._prepare_session(driver)
            url = f"{self.base_url}/dp/{job.asin}"
            LOGGER.debug("Fetching page asin=%s url=%s", job.asin, url)
            html = self._fetch_page_source(driver, url)
            soup = BeautifulSoup(html, "html.parser")
            status = detect_page_state(html, soup)
            self._write_debug_html_snapshot(
                asin=job.asin,
                url=url,
                status=status,
                html=html,
                requested_fields=requested_fields,
                field_contracts=field_contract_map,
            )
            if status != "OK":
                LOGGER.warning("Page state not OK asin=%s status=%s", job.asin, status)
            if status in {"BLOCKED", "NOT_FOUND"}:
                return ScrapeResult(
                    asin=job.asin,
                    product_type=job.product_type,
                    status=status,
                    url=url,
                    scraped_at=datetime.now(tz=timezone.utc),
                    title="",
                    fields={},
                    error=status,
                    page_scrape_status=status,
                    facts_extraction_status="failed" if self.extract_supplement_facts_details else "not_requested",
                    facts_failure_reason=f"page_status:{status}",
                )

            title = extract_title(soup)
            facts_table = extract_facts_table(soup)
            bullets = extract_bullets(soup)
            description = extract_description_text(soup)
            variations = extract_variations(soup)
            LOGGER.debug(
                "Extracted content asin=%s title_present=%s facts=%d bullets=%d description_chars=%d variations=%d",
                job.asin,
                bool(title),
                len(facts_table),
                len(bullets),
                len(description),
                len(variations),
            )

            chunks = build_chunk_records(
                asin=job.asin,
                title=title,
                facts_table=facts_table,
                bullets=bullets,
                description=description,
                variations=variations,
            )
            gallery_images = extract_gallery_images(html)
            LOGGER.info("Gallery images discovered asin=%s count=%d", job.asin, len(gallery_images))
            if not gallery_images:
                LOGGER.warning(
                    "No gallery images parsed asin=%s; vision extraction will be skipped. "
                    "Check the saved HTML gallery payload before treating the panel as absent.",
                    job.asin,
                )
            supplement_facts_detail = None
            facts_status = "not_requested" if not self.extract_supplement_facts_details else ""
            facts_failure_reason = ""
            try:
                vision_result = self.vision_resolver.resolve_job(
                    asin=job.asin,
                    requested_fields=requested_fields,
                    images=gallery_images,
                    field_contracts=field_contract_map or None,
                    extract_supplement_facts_details=self.extract_supplement_facts_details,
                )
                image_field_results = dict(vision_result.fields)
                supplement_facts_detail = vision_result.supplement_facts_detail
            except (OpenAIBudgetExceededError, OpenAIRunCancelledError, ScrapeCancelledError):
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Image-first extraction failed asin=%s gallery_images=%d error=%s; continuing with HTML fallback",
                    job.asin,
                    len(gallery_images),
                    exc,
                    exc_info=True,
                )
                image_field_results = {}
                supplement_facts_detail = None
                facts_failure_reason = f"image_path_failed: {exc}"[:300]
            LOGGER.debug(
                "Resolved image evidence asin=%s gallery_images=%d image_fields=%d",
                job.asin,
                len(gallery_images),
                len(image_field_results),
            )

            if self.extract_supplement_facts_details and supplement_facts_detail is not None:
                facts_status = "image"
            elif self.extract_supplement_facts_details:
                # HTML fallback: the page's own facts table still carries the panel when
                # the image path failed or found no readable panel.
                supplement_facts_detail = self._resolve_facts_detail_from_html(
                    asin=job.asin, facts_table=facts_table, requested_fields=requested_fields,
                )
                if supplement_facts_detail is not None:
                    facts_status = "html_fallback"
                elif facts_failure_reason:
                    facts_status = "failed"
                else:
                    facts_status = "no_panel_found"
            field_results = dict(image_field_results)
            for field_name in requested_fields:
                self._raise_if_cancelled()
                if field_name in field_results and field_results[field_name].value:
                    continue
                direct_result = resolve_field_from_facts_rows(field_name, chunks)
                if direct_result is None:
                    direct_result = resolve_field_from_text_chunks(field_name, chunks)
                if direct_result is not None:
                    LOGGER.debug(
                        "Resolved field directly from extracted evidence name=%s source=%s value=%s",
                        field_name,
                        direct_result.source,
                        direct_result.value,
                    )
                    field_results[field_name] = direct_result
                    continue

                query_text = build_query_text_for_contract(field_name, field_contract_map.get(field_name))
                ranked_chunks = self.retriever.rank_chunks(query_text=query_text, chunks=chunks)
                field_results[field_name] = self.extractor.extract_field(
                    field_name=field_name,
                    title=title,
                    chunks=ranked_chunks,
                    asin=job.asin,
                    retry_attempt=0,
                    field_contract=field_contract_map.get(field_name),
                )
            normalized_field_results = {
                field_name: self._normalize_field_result(
                    field_name=field_name,
                    field_result=field_result,
                    field_contract_map=field_contract_map,
                )
                for field_name, field_result in field_results.items()
            }
            normalized_field_results = self._apply_run_scoped_field_merges(
                asin=job.asin,
                field_results=normalized_field_results,
                field_contract_map=field_contract_map,
            )
            scrape_result = ScrapeResult(
                asin=job.asin,
                product_type=job.product_type,
                status=status,
                url=url,
                scraped_at=datetime.now(tz=timezone.utc),
                title=title,
                fields=normalized_field_results,
                error=None if status in {"OK", "PARTIAL"} else status,
                supplement_facts_detail=supplement_facts_detail,
                page_scrape_status=status,
                facts_extraction_status=facts_status,
                facts_failure_reason=facts_failure_reason,
            )
            if started_run_scope:
                self._finalize_run_scoped_result(job, scrape_result)
            return scrape_result
        except ScrapeCancelledError:
            LOGGER.warning("Job cancelled asin=%s product_type=%s", job.asin, job.product_type)
            raise
        except OpenAIRunCancelledError as exc:
            LOGGER.warning("OpenAI operation cancelled during job asin=%s product_type=%s", job.asin, job.product_type)
            raise ScrapeCancelledError("Batch run cancelled") from exc
        except OpenAIBudgetExceededError:
            LOGGER.error("Budget limit reached during job asin=%s product_type=%s", job.asin, job.product_type)
            raise
        except Exception as exc:
            if self._is_driver_session_lost(exc):
                LOGGER.error("WebDriver session lost during job asin=%s product_type=%s", job.asin, job.product_type)
                if self._is_cancel_requested():
                    raise ScrapeCancelledError("Batch run cancelled") from exc
                raise DriverSessionLostError(str(exc)) from exc
            LOGGER.exception("Job failed asin=%s product_type=%s error=%s", job.asin, job.product_type, exc)
            return ScrapeResult(
                asin=job.asin,
                product_type=job.product_type or "ad_hoc",
                status="NAVIGATION_ERROR",
                url=f"{self.base_url}/dp/{job.asin}",
                scraped_at=datetime.now(tz=timezone.utc),
                fields={},
                error=str(exc),
                page_scrape_status="NAVIGATION_ERROR",
                facts_extraction_status="failed" if self.extract_supplement_facts_details else "not_requested",
                facts_failure_reason=str(exc)[:300],
            )
        finally:
            self._log_job_api_usage(job.asin)
            if own_driver:
                self._set_active_driver(None)
                LOGGER.debug("Closing job-scoped WebDriver asin=%s", job.asin)
                try:
                    driver.quit()
                except Exception:
                    LOGGER.debug("Ignoring WebDriver shutdown error for job-scoped driver asin=%s", job.asin, exc_info=True)
            if started_run_scope:
                self._end_run_scope()

    def _log_job_api_usage(self, asin: str) -> None:
        usage = self.openai_client.get_job_usage()
        batch_usage = self.openai_client.get_batch_usage()
        LOGGER.info(
            "api_usage scope=asin asin=%s embedding_calls=%d embedding_tokens=%d embedding_cost_usd=%.6f chat_calls=%d chat_prompt_tokens=%d chat_prompt_cost_usd=%.6f chat_completion_tokens=%d chat_completion_cost_usd=%.6f chat_total_tokens=%d estimated_cost_usd=%.6f batch_estimated_cost_usd=%.6f max_cost_usd=%s",
            asin,
            usage.embedding_calls,
            usage.embedding_tokens,
            usage.embedding_cost_usd,
            usage.chat_calls,
            usage.chat_prompt_tokens,
            usage.chat_prompt_cost_usd,
            usage.chat_completion_tokens,
            usage.chat_completion_cost_usd,
            usage.chat_total_tokens,
            usage.estimated_cost_usd,
            batch_usage.estimated_cost_usd,
            _format_optional_usd(self.openai_config.max_cost_usd),
        )

    def _log_batch_api_usage(
        self,
        *,
        jobs_total: int,
        jobs_completed: int,
        fields_requested_total: int,
        stopped: bool,
    ) -> None:
        usage = self.openai_client.get_batch_usage()
        LOGGER.info(
            "api_usage scope=batch jobs_total=%d jobs_completed=%d fields_requested_total=%d embedding_calls=%d embedding_tokens=%d embedding_cost_usd=%.6f chat_calls=%d chat_prompt_tokens=%d chat_prompt_cost_usd=%.6f chat_completion_tokens=%d chat_completion_cost_usd=%.6f chat_total_tokens=%d estimated_cost_usd=%.6f max_cost_usd=%s stopped=%s",
            jobs_total,
            jobs_completed,
            fields_requested_total,
            usage.embedding_calls,
            usage.embedding_tokens,
            usage.embedding_cost_usd,
            usage.chat_calls,
            usage.chat_prompt_tokens,
            usage.chat_prompt_cost_usd,
            usage.chat_completion_tokens,
            usage.chat_completion_cost_usd,
            usage.chat_total_tokens,
            usage.estimated_cost_usd,
            _format_optional_usd(self.openai_config.max_cost_usd),
            str(stopped).lower(),
        )

    def _resolve_requested_fields(self, job: ScrapeJob) -> List[str]:
        return resolve_job_requested_fields(
            job,
            run_scoped_schema_enabled=self.run_scoped_schema_enabled,
        )

    def _normalize_field_result(
        self,
        *,
        field_name: str,
        field_result,
        field_contract_map: dict[str, RunFieldContract],
    ):
        if field_name in field_contract_map:
            normalized_result = normalize_field_result_for_contract(
                field_result,
                field_contract_map[field_name],
            )
            LOGGER.info(
                "field_contract_event scope=runtime_normalization internal_key=%s output_type=%s merge_similar_values=%s action=apply_type_normalization raw_value=%s normalized_value=%s",
                field_name,
                field_contract_map[field_name].output_type,
                str(field_contract_map[field_name].merge_similar_values).lower(),
                field_result.value,
                normalized_result.value,
            )
            return normalized_result

        return apply_custom_field_preset(
            field_name,
            field_result,
            presets=self.custom_field_presets,
        )

    def _apply_run_scoped_field_merges(
        self,
        *,
        asin: str,
        field_results: dict[str, object],
        field_contract_map: dict[str, RunFieldContract],
    ) -> dict[str, object]:
        if not self.run_scoped_schema_enabled or not field_contract_map:
            return field_results

        if not self._run_scoped_text_merge_is_active():
            return field_results

        merged_results = dict(field_results)
        apply_to_result = self._run_scoped_text_merge_applies_to_results()
        for field_name, field_result in field_results.items():
            field_contract = field_contract_map.get(field_name)
            if field_contract is None:
                continue
            merged_results[field_name] = self.run_scoped_canonicalizer.register_field_result(
                asin=asin,
                field_contract=field_contract,
                result=field_result,
                apply_to_result=apply_to_result,
            )
        return merged_results

    def _finalize_run_scoped_results(
        self,
        jobs: Iterable[ScrapeJob],
        results: Iterable[ScrapeResult],
    ) -> None:
        if not self.run_scoped_schema_enabled or not self._run_scoped_text_merge_is_active():
            return

        for job, result in zip(jobs, results):
            self._finalize_run_scoped_result(job, result)

    def _finalize_run_scoped_result(
        self,
        job: ScrapeJob,
        result: ScrapeResult,
    ) -> None:
        if not self.run_scoped_schema_enabled or not self._run_scoped_text_merge_is_active():
            return

        field_contract_map = get_job_field_contract_map(
            job,
            run_scoped_schema_enabled=self.run_scoped_schema_enabled,
        )
        apply_to_result = self._run_scoped_text_merge_applies_to_results()
        for field_name, field_contract in field_contract_map.items():
            if field_name not in result.fields:
                continue
            result.fields[field_name] = self.run_scoped_canonicalizer.finalize_field_result(
                field_contract=field_contract,
                result=result.fields[field_name],
                apply_to_result=apply_to_result,
            )
        self._write_run_scoped_merge_metadata(
            asin=job.asin,
            merge_events=self.run_scoped_canonicalizer.pop_merge_events(asin=job.asin),
        )

    def _begin_run_scope(self) -> None:
        if not self.run_scoped_schema_enabled or self._run_scope_active:
            return
        self.run_scoped_canonicalizer.reset()
        self._run_scope_active = True

    def _end_run_scope(self) -> None:
        if not self._run_scope_active:
            return
        self.run_scoped_canonicalizer.reset()
        self._run_scope_active = False

    def _log_job_contract_metadata(
        self,
        job: ScrapeJob,
        requested_fields: list[str],
        field_contract_map: dict[str, RunFieldContract],
    ) -> None:
        LOGGER.info(
            "job_contract_summary asin=%s run_scoped_schema_enabled=%s run_scoped_text_merge_mode=%s contract_count=%d generated_contract_count=%d contract_keys=%s requested_fields=%s",
            job.asin,
            str(self.run_scoped_schema_enabled).lower(),
            self.run_scoped_text_merge_mode,
            len(field_contract_map),
            sum(1 for contract in field_contract_map.values() if contract.matched_by == "generated"),
            ",".join(sorted(field_contract_map)),
            ",".join(requested_fields),
        )
        for contract in field_contract_map.values():
            LOGGER.info(
                "field_resolution_event asin=%s requested_label=%s internal_key=%s matched_by=%s product_type=%s",
                job.asin,
                contract.requested_label,
                contract.internal_key,
                contract.matched_by,
                job.product_type,
            )
            LOGGER.info(
                "field_contract_event asin=%s requested_label=%s internal_key=%s output_type=%s merge_similar_values=%s matched_by=%s",
                job.asin,
                contract.requested_label,
                contract.internal_key,
                contract.output_type,
                str(contract.merge_similar_values).lower(),
                contract.matched_by,
            )

    def _run_scoped_text_merge_is_active(self) -> bool:
        return self.run_scoped_schema_enabled and self.run_scoped_text_merge_mode in {"shadow", "apply"}

    def _run_scoped_text_merge_applies_to_results(self) -> bool:
        return self.run_scoped_text_merge_mode == "apply"

    def cancel(self) -> None:
        self.cancel_event.set()
        driver = self._get_active_driver()
        if driver is None:
            return
        try:
            LOGGER.warning("Cancellation requested; closing active WebDriver session")
            driver.quit()
        except Exception:
            LOGGER.debug("Ignoring WebDriver shutdown error during cancellation", exc_info=True)

    def _resolve_facts_detail_from_html(self, *, asin: str, facts_table: str, requested_fields: list[str]):
        """Second leg of ADR-001: recover the panel from page HTML when images fail."""
        resolver = getattr(self.vision_resolver, "resolve_supplement_facts_detail_from_text", None)
        if resolver is None or not (facts_table or "").strip():
            return None
        try:
            detail = resolver(asin=asin, facts_text=facts_table, requested_fields=requested_fields)
        except (OpenAIBudgetExceededError, OpenAIRunCancelledError, ScrapeCancelledError):
            raise
        except Exception:
            LOGGER.warning("HTML facts-panel fallback failed asin=%s", asin, exc_info=True)
            return None
        if detail is not None:
            LOGGER.info("Recovered Supplement Facts panel from HTML asin=%s ingredients=%d",
                        asin, len(detail.ingredients))
        return detail

    def _build_driver(self) -> webdriver.Chrome:
        """Build a Chrome driver with conservative automation flags."""
        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=en-US")
        options.add_argument("--log-level=3")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])

        try:
            LOGGER.debug("Starting Chrome WebDriver headless=%s", self.headless)
            return webdriver.Chrome(options=options)
        except WebDriverException as exc:
            LOGGER.exception("Chrome WebDriver startup failed")
            raise RuntimeError(
                "Unable to start Chrome WebDriver. Install Chrome + a matching driver, "
                "or ensure Selenium Manager can provision it."
            ) from exc

    def _prepare_session(self, driver: webdriver.Chrome) -> None:
        self._raise_if_cancelled()
        session_id = driver.session_id
        if session_id in self._prepared_sessions:
            return

        LOGGER.info("Preparing Amazon session delivery_zip=%s", self.delivery_zip or "unchanged")
        driver.get(f"{self.base_url}/?language=en_US")
        self._wait_for_non_blocked_page(driver, purpose="session initialization")

        if self.delivery_zip and not self._ensure_delivery_zip(driver):
            raise RuntimeError(f"Unable to verify Amazon delivery ZIP {self.delivery_zip}")

        self._prepared_sessions.add(session_id)

    def _ensure_delivery_zip(self, driver: webdriver.Chrome) -> bool:
        self._raise_if_cancelled()
        LOGGER.info("Applying Amazon delivery ZIP zip=%s", self.delivery_zip)
        wait = WebDriverWait(driver, self.timeout_seconds)

        trigger = self._find_first_clickable(
            wait,
            [
                (By.ID, "nav-global-location-popover-link"),
                (By.ID, "glow-ingress-block"),
            ],
        )
        if trigger is None:
            LOGGER.warning("Location popover trigger not found; unable to verify Amazon delivery ZIP")
            return False

        driver.execute_script("arguments[0].click();", trigger)
        zip_input = self._find_first_present(
            wait,
            [
                (By.ID, "GLUXZipUpdateInput"),
                (By.CSS_SELECTOR, "input[data-action='GLUXPostalUpdateInput']"),
                (By.CSS_SELECTOR, "input[aria-label='or enter a US zip code']"),
            ],
        )
        if zip_input is None:
            LOGGER.warning("ZIP input not found in Amazon location popover")
            return False

        zip_input.clear()
        zip_input.send_keys(self.delivery_zip)

        apply_button = self._find_first_clickable(
            wait,
            [
                (By.CSS_SELECTOR, "#GLUXZipUpdate input.a-button-input"),
                (By.CSS_SELECTOR, "#GLUXZipUpdate .a-button-input"),
                (By.ID, "GLUXZipUpdate-announce"),
            ],
        )
        if apply_button is None:
            LOGGER.warning("ZIP apply button not found; unable to force delivery ZIP")
            return False

        driver.execute_script("arguments[0].click();", apply_button)
        return self._finish_delivery_zip(driver, wait)

    def _finish_delivery_zip(self, driver: webdriver.Chrome, wait: WebDriverWait) -> bool:
        """Wait for Apply, optional confirmation, and the actual delivery header."""
        confirmation_clicked = False

        def check_update(d):
            nonlocal confirmation_clicked
            self._raise_if_cancelled()
            try:
                for error in d.find_elements(By.CSS_SELECTOR, "#GLUXZipError, #GLUXZipServerError"):
                    if error.is_displayed() and error.text.strip():
                        return (False, error.text.strip())

                # These controls also exist in hidden markup after the dialog closes.
                dialog_open = any(
                    element.is_displayed()
                    for element in d.find_elements(
                        By.CSS_SELECTOR,
                        "#GLUXZipUpdateInput, #GLUXConfirmClose, #GLUXConfirmClose-announce, button[name='glowDoneButton']",
                    )
                )
                if not dialog_open and self._delivery_header_matches(d):
                    return (True, "")
                if not confirmation_clicked:
                    confirmation_clicked = self._click_delivery_confirmation(d)
            except StaleElementReferenceException:
                # Apply and confirmation can replace the dialog or reload the page.
                return False
            return False

        try:
            applied, error = wait.until(check_update)
        except TimeoutException:
            LOGGER.warning(
                "Timed out verifying Amazon delivery ZIP zip=%s confirmation_clicked=%s",
                self.delivery_zip,
                confirmation_clicked,
            )
            return False
        if not applied:
            LOGGER.warning("Amazon delivery ZIP rejected zip=%s error=%s", self.delivery_zip, error)
            return False
        LOGGER.info("Amazon delivery ZIP applied and verified zip=%s", self.delivery_zip)
        return True

    def _delivery_header_matches(self, driver: webdriver.Chrome) -> bool:
        for element in driver.find_elements(By.ID, "nav-global-location-popover-link"):
            if element.is_displayed() and re.search(rf"(?<!\d){re.escape(self.delivery_zip)}(?!\d)", element.text):
                return True
        return False

    def _fetch_page_source(self, driver: webdriver.Chrome, url: str) -> str:
        """Fetch page HTML and wait for either product title or known blocked/not-found markers."""
        attempts = 2
        for attempt in range(1, attempts + 1):
            self._raise_if_cancelled()
            driver.get(url)
            self._wait_for_non_blocked_page(
                driver,
                purpose=f"loading product page url={url} attempt={attempt}",
            )

            wait = WebDriverWait(driver, self.timeout_seconds)
            try:
                wait.until(
                    lambda d: d.find_elements(By.ID, "productTitle")
                    or self._is_not_found_page(d.page_source)
                    or self._is_captcha_page(d.page_source)
                )
            except TimeoutException:
                LOGGER.warning("Timed out waiting for stable page markers url=%s attempt=%d", url, attempt)

            has_title = bool(driver.find_elements(By.ID, "productTitle"))
            if has_title:
                self._expand_product_detail_sections(driver)

            html = driver.page_source
            if has_title or self._is_not_found_page(html):
                return html

            if attempt < attempts:
                LOGGER.info("Expected product markers missing; reopening target PDP url=%s attempt=%d", url, attempt + 1)

        return driver.page_source

    def _write_debug_html_snapshot(
        self,
        *,
        asin: str,
        url: str,
        status: str,
        html: str,
        requested_fields: list[str] | None = None,
        field_contracts: dict[str, RunFieldContract] | None = None,
    ) -> None:
        if self.debug_html_dir is None:
            return

        snapshot_path = self.debug_html_dir / f"{asin}.html"
        metadata_path = self.debug_html_dir / f"{asin}.json"
        self.debug_html_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(html, encoding="utf-8")
        metadata = {
            "asin": asin,
            "url": url,
            "status": status,
            "saved_at_utc": datetime.now(tz=timezone.utc).isoformat(),
            "html_chars": len(html),
            "extract_supplement_facts_details": self.extract_supplement_facts_details,
            "run_scoped_schema_enabled": self.run_scoped_schema_enabled,
            "run_scoped_text_merge_mode": self.run_scoped_text_merge_mode,
            "requested_fields": list(requested_fields or []),
            "run_scoped_merge_event_count": 0,
            "run_scoped_merge_events": [],
            "field_contracts": [
                {
                    "requested_label": contract.requested_label,
                    "internal_key": contract.internal_key,
                    "output_type": contract.output_type,
                    "merge_similar_values": contract.merge_similar_values,
                    "matched_by": contract.matched_by,
                }
                for contract in (field_contracts or {}).values()
            ],
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        LOGGER.info(
            "Saved ASIN HTML snapshot asin=%s status=%s html_path=%s metadata_path=%s chars=%d",
            asin,
            status,
            snapshot_path,
            metadata_path,
            len(html),
        )

    def _write_run_scoped_merge_metadata(
        self,
        *,
        asin: str,
        merge_events,
    ) -> None:
        if self.debug_html_dir is None:
            return

        metadata_path = self.debug_html_dir / f"{asin}.json"
        if not metadata_path.exists():
            return

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        serialized_events = [event.to_dict() for event in merge_events]
        metadata["run_scoped_merge_event_count"] = len(serialized_events)
        metadata["run_scoped_merge_events"] = serialized_events
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def _expand_product_detail_sections(self, driver: webdriver.Chrome) -> None:
        candidate_selector = (
            "[aria-expanded='false'], "
            "details:not([open]) > summary, "
            ".a-expander-container:not(.a-expander-expanded) .a-expander-prompt, "
            ".a-expander-container:not(.a-expander-expanded) .a-expander-header"
        )
        total_expanded = 0
        clicked_section_keys: set[str] = set()

        for pass_index in range(1, 4):
            self._raise_if_cancelled()
            expanded_labels: list[str] = []
            for element in driver.find_elements(By.CSS_SELECTOR, candidate_selector):
                try:
                    if not element.is_displayed():
                        continue

                    metadata = self._describe_expandable_section(driver, element)
                    label_text = str(metadata.get("text", "") or "")
                    hint_text = str(metadata.get("hints", "") or "")
                    section_key = self._match_expandable_section_key(label_text, hint_text)
                    if section_key is None or section_key in clicked_section_keys:
                        continue

                    target = self._resolve_click_target(driver, element)
                    if target is None or not target.is_displayed():
                        continue

                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
                    driver.execute_script("arguments[0].click();", target)

                    clicked_section_keys.add(section_key)
                    expanded_labels.append(section_key)
                    time.sleep(0.15)
                except StaleElementReferenceException:
                    continue
                except WebDriverException:
                    continue

            if not expanded_labels:
                break

            total_expanded += len(expanded_labels)
            LOGGER.info(
                "Expanded Amazon product detail sections pass=%d count=%d labels=%s",
                pass_index,
                len(expanded_labels),
                ", ".join(expanded_labels[:5]),
            )
            time.sleep(0.5)

        if total_expanded:
            driver.execute_script("window.scrollTo(0, 0);")

    def _describe_expandable_section(self, driver: webdriver.Chrome, element) -> dict[str, str]:
        metadata = driver.execute_script(
            r"""
            const node = arguments[0];
            const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
            const textParts = [];
            const hintParts = [];
            let current = node;
            for (let depth = 0; current && depth < 4; depth += 1, current = current.parentElement) {
                const ariaLabel = current.getAttribute ? current.getAttribute('aria-label') || '' : '';
                const text = normalize(`${ariaLabel} ${current.innerText || current.textContent || ''}`);
                if (text) {
                    textParts.push(text);
                }
                const hint = normalize(`${current.id || ''} ${current.className || ''} ${current.getAttribute ? current.getAttribute('aria-controls') || '' : ''} ${current.getAttribute ? current.getAttribute('data-feature-name') || '' : ''}`);
                if (hint) {
                    hintParts.push(hint);
                }
            }
            return {text: textParts.join(' | '), hints: hintParts.join(' | ')};
            """,
            element,
        )
        if isinstance(metadata, dict):
            return {"text": str(metadata.get("text", "") or ""), "hints": str(metadata.get("hints", "") or "")}
        return {"text": "", "hints": ""}

    @classmethod
    def _match_expandable_section_key(cls, label_text: str, hint_text: str) -> str | None:
        normalized_label = cls._normalize_metadata_text(label_text)
        normalized_hint = cls._normalize_metadata_text(hint_text)
        for label in cls._EXPANDABLE_SECTION_LABELS:
            if label in normalized_label:
                return label
        for hint in cls._EXPANDABLE_SECTION_HINTS:
            if hint in normalized_hint:
                return hint
        return None

    @classmethod
    def _should_expand_product_section(cls, label_text: str, hint_text: str) -> bool:
        return cls._match_expandable_section_key(label_text, hint_text) is not None

    @staticmethod
    def _normalize_metadata_text(value: str) -> str:
        return " ".join(value.casefold().split())

    def _wait_for_non_blocked_page(self, driver: webdriver.Chrome, purpose: str) -> None:
        wait = WebDriverWait(driver, self.timeout_seconds)
        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except TimeoutException:
            LOGGER.warning("Timed out waiting for document body during %s", purpose)
            return

        deadline = time.monotonic() + (self.captcha_timeout_seconds if not self.headless else self.timeout_seconds)
        warned = False
        while time.monotonic() < deadline:
            self._raise_if_cancelled()
            if not self._is_captcha_page(driver.page_source):
                return
            if not warned:
                LOGGER.warning("Amazon CAPTCHA encountered during %s; waiting for manual resolution", purpose)
                warned = True
            time.sleep(1)

        LOGGER.warning("Timed out waiting for CAPTCHA clearance during %s", purpose)

    def _find_first_clickable(self, wait: WebDriverWait, locators: list[tuple[str, str]]):
        for locator in locators:
            try:
                return wait.until(EC.element_to_be_clickable(locator))
            except TimeoutException:
                continue
        return None

    def _find_first_present(self, wait: WebDriverWait, locators: list[tuple[str, str]]):
        for locator in locators:
            try:
                return wait.until(EC.presence_of_element_located(locator))
            except TimeoutException:
                continue
        return None

    def _click_delivery_confirmation(self, driver: webdriver.Chrome) -> bool:
        # Observed 2026-09-15: Continue remains hidden while Done is visible.
        # Done is also visible *before* Apply, so wait for the accepted ZIP first.
        zip_accepted = any(
            element.is_displayed() and element.text.strip() == self.delivery_zip
            for element in driver.find_elements(By.ID, "GLUXZipConfirmationValue")
        )
        confirmation_selectors = [
            "input#GLUXConfirmClose, #GLUXConfirmClose input.a-button-input",
        ]
        if zip_accepted:
            confirmation_selectors.append("button[name='glowDoneButton']")
        for selector in confirmation_selectors:
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                if element.is_displayed() and element.is_enabled():
                    label = element.text.strip() or element.get_attribute("aria-labelledby") or "Continue"
                    LOGGER.info("Clicking Amazon delivery confirmation button label=%s", label)
                    driver.execute_script("arguments[0].click();", element)
                    return True

        confirmation_xpaths = [
            "//div[@role='dialog' or contains(@class, 'a-modal')]//span[normalize-space()='Continue']",
            "//div[@role='dialog' or contains(@class, 'a-modal')]//button[normalize-space()='Continue']",
            "//div[@role='dialog' or contains(@class, 'a-modal')]//input[@type='submit' and (@aria-labelledby='GLUXConfirmClose-announce' or @value='Continue')]",
            "//span[@id='GLUXConfirmClose-announce']",
        ]
        for xpath in confirmation_xpaths:
            for element in driver.find_elements(By.XPATH, xpath):
                if not element.is_displayed():
                    continue
                target = self._resolve_click_target(driver, element)
                if target is None or not target.is_displayed() or not target.is_enabled():
                    continue
                label = element.text.strip() or target.get_attribute("value") or target.get_attribute("aria-label") or "unknown"
                LOGGER.info("Clicking Amazon delivery confirmation button label=%s", label)
                driver.execute_script("arguments[0].click();", target)
                return True
        return False

    def _resolve_click_target(self, driver: webdriver.Chrome, element):
        target = driver.execute_script(
            """
            let node = arguments[0];
            while (node) {
                const tag = (node.tagName || '').toLowerCase();
                if (tag === 'button' || tag === 'a') {
                    return node;
                }
                if (tag === 'input') {
                    const type = (node.getAttribute('type') || '').toLowerCase();
                    if (type === 'button' || type === 'submit') {
                        return node;
                    }
                }
                if ((node.getAttribute && node.getAttribute('role')) === 'button') {
                    return node;
                }
                node = node.parentElement;
            }
            return arguments[0];
            """,
            element,
        )
        return target

    def _is_captcha_page(self, html: str) -> bool:
        lower_html = html.lower()
        return any(marker in lower_html for marker in self._CAPTCHA_MARKERS)

    def _is_not_found_page(self, html: str) -> bool:
        lower_html = html.lower()
        return any(marker in lower_html for marker in self._NOT_FOUND_MARKERS)

    def _raise_if_cancelled(self) -> None:
        if self._is_cancel_requested():
            raise ScrapeCancelledError("Batch run cancelled")

    def _is_cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    def _sleep_with_cancel(self, duration_seconds: float) -> None:
        remaining = max(0.0, duration_seconds)
        while remaining > 0:
            self._raise_if_cancelled()
            sleep_slice = min(0.2, remaining)
            time.sleep(sleep_slice)
            remaining -= sleep_slice

    def _set_active_driver(self, driver: webdriver.Chrome | None) -> None:
        with self._driver_lock:
            self._active_driver = driver

    def _get_active_driver(self) -> webdriver.Chrome | None:
        with self._driver_lock:
            return self._active_driver

    def _notify_progress(self, event: str, completed_jobs: int, total_jobs: int, asin: str) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event, completed_jobs, total_jobs, asin)
        except Exception:
            LOGGER.debug("Ignoring progress callback error", exc_info=True)

    @staticmethod
    def _is_driver_session_lost(exc: BaseException) -> bool:
        if isinstance(exc, (DriverSessionLostError, InvalidSessionIdException, NoSuchWindowException)):
            return True
        if isinstance(exc, MaxRetryError):
            lowered = str(exc).lower()
            return "localhost" in lowered and "failed to establish a new connection" in lowered
        if isinstance(exc, WebDriverException):
            lowered = str(exc).lower()
            markers = (
                "invalid session id",
                "chrome not reachable",
                "disconnected",
                "target window already closed",
                "no such window",
                "session deleted because of page crash",
                "connection refused",
            )
            return any(marker in lowered for marker in markers)
        return False


def _format_optional_usd(value: float | None) -> str:
    if value is None:
        return "none"
    return f"{value:.6f}"


def _normalize_text_merge_mode(raw_value: str) -> str:
    normalized = str(raw_value or "").strip().lower()
    if normalized in {"off", "shadow", "apply"}:
        return normalized
    return "off"

