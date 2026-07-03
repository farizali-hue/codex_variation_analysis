from __future__ import annotations

import unittest

from amazon_scraper.extraction import OpenAIFieldExtractor
from amazon_scraper.models import ChunkRecord, RunFieldContract


class _DummyClient:
    pass


class ExtractionPromptTests(unittest.TestCase):
    def test_html_prompt_includes_direct_support_abstention_rules(self) -> None:
        extractor = OpenAIFieldExtractor(_DummyClient())
        chunks = [
            ChunkRecord(
                asin="B0TEST",
                section="bullets",
                text="Pumpkin Seed Oil front label text",
                dom_hint="feature-bullet-1",
                order=1,
                source_priority=1,
            )
        ]
        prompt = extractor._build_user_prompt(
            "flavor",
            "Pumpkin Seed Oil Softgels",
            chunks,
            RunFieldContract(
                requested_label="Flavor",
                internal_key="flavor",
                output_type="text",
                merge_similar_values=True,
                matched_by="generated",
            ),
        )

        self.assertIn("Direct support rules:", prompt)
        self.assertIn("Treat label/value pairs, selected variations, free-from claims, and clearly printed packaging details as direct support.", prompt)
        self.assertIn("Do not infer the field from brand names, product names, ingredient names, dosage text, or general marketing copy.", prompt)
        self.assertIn("does not explicitly answer 'Flavor', return an empty value", prompt)


if __name__ == "__main__":
    unittest.main()
