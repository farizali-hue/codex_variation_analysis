from __future__ import annotations

import unittest

from amazon_scraper.extraction import OpenAIFieldExtractor
from amazon_scraper.models import ChunkRecord


class _FakeChatClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls: list[str] = []

    def create_chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        asin: str,
        field: str,
        retry_attempt: int = 0,
        model: str | None = None,
        image_inputs: list[dict[str, str]] | None = None,
    ) -> str:
        del system_prompt, asin, retry_attempt, model, image_inputs
        self.calls.append(field)
        return self.response_text


def _build_chunks() -> list[ChunkRecord]:
    return [
        ChunkRecord(
            asin="B0TESTING1",
            section="facts_table",
            text="Serving Size: 3 Capsules",
            dom_hint="facts-table-1",
            order=0,
            source_priority=90,
        )
    ]


class ExtractFieldTests(unittest.TestCase):
    def test_extract_field_parses_valid_json_response(self) -> None:
        extractor = OpenAIFieldExtractor(
            _FakeChatClient('{"value": "3 Capsules", "evidence": "Serving Size: 3 Capsules", "source": "facts_table", "confidence": 0.9}')
        )

        result = extractor.extract_field(
            "serving_size",
            "Test Product",
            _build_chunks(),
            asin="B0TESTING1",
        )

        self.assertEqual(result.value, "3 Capsules")
        self.assertEqual(result.confidence, 0.9)

    def test_extract_field_abstains_on_malformed_json_instead_of_raising(self) -> None:
        # A single malformed model response must not raise: in run_job that
        # exception downgrades the WHOLE job to NAVIGATION_ERROR and drops every
        # already-resolved field. Abstaining on the one field matches the
        # vision-path fallback behavior.
        extractor = OpenAIFieldExtractor(
            _FakeChatClient("Sorry, I cannot answer that in JSON.")
        )

        result = extractor.extract_field(
            "serving_size",
            "Test Product",
            _build_chunks(),
            asin="B0TESTING1",
        )

        self.assertEqual(result.value, "")
        self.assertEqual(result.confidence, 0.0)
        # Provenance fallbacks still apply so the abstention is auditable.
        self.assertEqual(result.source, "facts_table")
        self.assertEqual(result.evidence, "Serving Size: 3 Capsules")

    def test_extract_field_abstains_on_truncated_json_code_fence(self) -> None:
        extractor = OpenAIFieldExtractor(
            _FakeChatClient('```json\n{"value": "3 Capsules", "confidence": 0.9')
        )

        result = extractor.extract_field(
            "serving_size",
            "Test Product",
            _build_chunks(),
            asin="B0TESTING1",
        )

        self.assertEqual(result.value, "")
        self.assertEqual(result.confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
