from __future__ import annotations

import unittest

import amazon_scraper.scraper as scraper_module
from amazon_scraper.models import ScrapeJob
from amazon_scraper.openai_client import OpenAIConfig
from amazon_scraper.scraper import AmazonScraper


class _FakeDriver:
    session_id = "fake-session"

    def quit(self) -> None:
        return None


class ScraperStatusGuardTests(unittest.TestCase):
    def test_blocked_page_returns_without_retrieval_or_extraction(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
        )
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: "<html><body>captcha</body></html>"

        calls = {"rank": 0, "extract": 0}

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                calls["rank"] += 1
                return chunks

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                calls["extract"] += 1
                raise AssertionError("Extractor should not be called for blocked pages")

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        original_detect_page_state = scraper_module.detect_page_state
        try:
            scraper_module.detect_page_state = lambda html, soup: "BLOCKED"

            result = scraper.run_job(
                ScrapeJob(asin="B0DJWV4BQ1", product_type="supplement", requested_fields=["packaging_type"]),
                driver=_FakeDriver(),
            )
        finally:
            scraper_module.detect_page_state = original_detect_page_state

        self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(result.error, "BLOCKED")
        self.assertEqual(calls["rank"], 0)
        self.assertEqual(calls["extract"], 0)


if __name__ == "__main__":
    unittest.main()
