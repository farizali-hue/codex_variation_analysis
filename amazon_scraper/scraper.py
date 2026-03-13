from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from .chunking import build_chunk_records
from .extraction import OpenAIFieldExtractor, build_query_text
from .html_extractors import (
    detect_page_state,
    extract_bullets,
    extract_description_text,
    extract_facts_table,
    extract_title,
    extract_variations,
)
from .models import ScrapeJob, ScrapeResult
from .openai_client import OpenAIClient, OpenAIConfig
from .retrieval import DenseRetriever, EmbeddingCache

LOGGER = logging.getLogger(__name__)


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

    def __init__(
        self,
        base_url: str = "https://www.amazon.com",
        timeout_seconds: int = 20,
        headless: bool = False,
        delivery_zip: str = "10001",
        captcha_timeout_seconds: int = 180,
        embedding_cache_file: Path | str = ".cache/embeddings.json",
        openai_config: OpenAIConfig | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headless = headless
        self.delivery_zip = delivery_zip.strip()
        self.captcha_timeout_seconds = captcha_timeout_seconds
        self._prepared_sessions: set[str] = set()
        self.openai_config = openai_config or OpenAIConfig.from_env()
        self.openai_client = OpenAIClient(self.openai_config)
        self.embedding_cache = EmbeddingCache(embedding_cache_file)
        self.retriever = DenseRetriever(
            client=self.openai_client,
            embedding_model=self.openai_config.embedding_model,
            cache=self.embedding_cache,
        )
        self.extractor = OpenAIFieldExtractor(self.openai_client)

    def run_batch(self, jobs: Iterable[ScrapeJob], delay_seconds: float = 1.5) -> List[ScrapeResult]:
        """Run jobs with a shared browser session to reduce startup cost."""
        job_list = list(jobs)
        LOGGER.info("Starting batch scrape jobs=%d delay_seconds=%.2f", len(job_list), delay_seconds)
        driver = self._build_driver()
        results: List[ScrapeResult] = []
        try:
            self._prepare_session(driver)
            for index, job in enumerate(job_list, start=1):
                LOGGER.info("Running job %d/%d asin=%s product_type=%s", index, len(job_list), job.asin, job.product_type)
                results.append(self.run_job(job, driver))
                time.sleep(delay_seconds)
        finally:
            LOGGER.debug("Closing shared WebDriver session")
            driver.quit()
        LOGGER.info("Batch scrape finished jobs=%d", len(results))
        return results

    def run_job(self, job: ScrapeJob, driver: webdriver.Chrome | None = None) -> ScrapeResult:
        """Execute one scrape job and return normalized field outputs."""
        requested_fields = self._resolve_requested_fields(job)
        own_driver = driver is None
        if own_driver:
            LOGGER.debug("No driver provided; creating job-scoped WebDriver asin=%s", job.asin)
            driver = self._build_driver()

        assert driver is not None
        try:
            self._prepare_session(driver)
            url = f"{self.base_url}/dp/{job.asin}"
            LOGGER.debug("Fetching page asin=%s url=%s", job.asin, url)
            html = self._fetch_page_source(driver, url)
            soup = BeautifulSoup(html, "html.parser")
            status = detect_page_state(html, soup)
            if status != "OK":
                LOGGER.warning("Page state not OK asin=%s status=%s", job.asin, status)

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
            field_results = {}
            for field_name in requested_fields:
                query_text = build_query_text(field_name)
                ranked_chunks = self.retriever.rank_chunks(query_text=query_text, chunks=chunks)
                field_results[field_name] = self.extractor.extract_field(
                    field_name=field_name,
                    title=title,
                    chunks=ranked_chunks,
                )
            return ScrapeResult(
                asin=job.asin,
                product_type=job.product_type,
                status=status,
                url=url,
                scraped_at=datetime.now(tz=timezone.utc),
                title=title,
                fields=field_results,
                error=None if status in {"OK", "PARTIAL"} else status,
            )
        except Exception as exc:
            LOGGER.exception("Job failed asin=%s product_type=%s error=%s", job.asin, job.product_type, exc)
            return ScrapeResult(
                asin=job.asin,
                product_type=job.product_type or "ad_hoc",
                status="NAVIGATION_ERROR",
                url=f"{self.base_url}/dp/{job.asin}",
                scraped_at=datetime.now(tz=timezone.utc),
                fields={},
                error=str(exc),
            )
        finally:
            if own_driver:
                LOGGER.debug("Closing job-scoped WebDriver asin=%s", job.asin)
                driver.quit()

    def _resolve_requested_fields(self, job: ScrapeJob) -> List[str]:
        if job.requested_fields:
            return job.requested_fields

        raise RuntimeError(f"Job for asin {job.asin} must include required_fields")

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
        session_id = driver.session_id
        if session_id in self._prepared_sessions:
            return

        LOGGER.info("Preparing Amazon session delivery_zip=%s", self.delivery_zip or "unchanged")
        driver.get(f"{self.base_url}/?language=en_US")
        self._wait_for_non_blocked_page(driver, purpose="session initialization")

        if self.delivery_zip:
            self._ensure_delivery_zip(driver)

        self._prepared_sessions.add(session_id)

    def _ensure_delivery_zip(self, driver: webdriver.Chrome) -> None:
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
            LOGGER.warning("Location popover trigger not found; keeping current Amazon delivery location")
            return

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
            return

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
            return

        driver.execute_script("arguments[0].click();", apply_button)
        time.sleep(2)

        if not self._click_delivery_confirmation(driver):
            confirm_button = self._find_first_clickable(
                wait,
                [
                    (By.CSS_SELECTOR, "#GLUXConfirmClose input.a-button-input"),
                    (By.CSS_SELECTOR, "#GLUXConfirmClose .a-button-input"),
                    (By.ID, "GLUXConfirmClose-announce"),
                    (By.CSS_SELECTOR, "button[name='glowDoneButton']"),
                ],
            )
            if confirm_button is not None:
                driver.execute_script("arguments[0].click();", confirm_button)

        try:
            wait.until(lambda d: self.delivery_zip in d.page_source)
            LOGGER.info("Amazon delivery ZIP applied zip=%s", self.delivery_zip)
        except TimeoutException:
            LOGGER.warning("Timed out waiting for Amazon to reflect delivery ZIP zip=%s", self.delivery_zip)

    def _fetch_page_source(self, driver: webdriver.Chrome, url: str) -> str:
        """Fetch page HTML and wait for either product title or known blocked/not-found markers."""
        attempts = 2
        for attempt in range(1, attempts + 1):
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

            html = driver.page_source
            if driver.find_elements(By.ID, "productTitle") or self._is_not_found_page(html):
                return html

            if attempt < attempts:
                LOGGER.info("Expected product markers missing; reopening target PDP url=%s attempt=%d", url, attempt + 1)

        return driver.page_source

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
                if target is None:
                    continue
                label = element.text.strip() or target.get_attribute("value") or target.get_attribute("aria-label") or "unknown"
                LOGGER.info("Clicking Amazon delivery confirmation button label=%s", label)
                driver.execute_script("arguments[0].click();", target)
                return True
        LOGGER.warning("Amazon delivery confirmation button not found after ZIP update")
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
