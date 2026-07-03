from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from amazon_scraper.models import RunFieldContract
from amazon_scraper.openai_client import OpenAIConfig
from amazon_scraper.scraper import AmazonScraper


class DebugHtmlSnapshotTests(unittest.TestCase):
    def test_writes_html_and_metadata_per_asin(self) -> None:
        temp_dir = Path.cwd() / ".test_artifacts" / "debug_html"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            scraper = AmazonScraper(
                debug_html_dir=str(temp_dir),
                openai_config=OpenAIConfig(
                    api_key="dummy",
                    embedding_model="text-embedding-3-small",
                    chat_model="gpt-5.4-mini",
                ),
                run_scoped_text_merge_mode="off",
            )

            scraper._write_debug_html_snapshot(
                asin="B0TEST123",
                url="https://www.amazon.com/dp/B0TEST123",
                status="OK",
                html="<html><body>example</body></html>",
                requested_fields=["flavor"],
                field_contracts={
                    "flavor": RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                },
            )

            html_path = temp_dir / "B0TEST123.html"
            metadata_path = temp_dir / "B0TEST123.json"

            self.assertTrue(html_path.exists())
            self.assertTrue(metadata_path.exists())
            self.assertEqual(html_path.read_text(encoding="utf-8"), "<html><body>example</body></html>")

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["asin"], "B0TEST123")
            self.assertEqual(metadata["status"], "OK")
            self.assertEqual(metadata["url"], "https://www.amazon.com/dp/B0TEST123")
            self.assertEqual(metadata["run_scoped_text_merge_mode"], "off")
            self.assertEqual(metadata["requested_fields"], ["flavor"])
            self.assertEqual(metadata["field_contracts"][0]["internal_key"], "flavor")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
