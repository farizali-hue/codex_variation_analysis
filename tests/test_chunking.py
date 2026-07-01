from __future__ import annotations

import unittest

from amazon_scraper.chunking import build_chunk_records


class ChunkingTests(unittest.TestCase):
    def test_build_chunk_records_splits_fact_rows(self) -> None:
        chunks = build_chunk_records(
            asin="B0TEST",
            title="Example",
            facts_table="Serving Size: 3 Capsules\nIngredients: Pumpkin Seed Oil",
            bullets=[],
            description="",
            variations="",
        )

        fact_chunks = [chunk for chunk in chunks if chunk.section == "facts_table"]

        self.assertEqual([chunk.text for chunk in fact_chunks], [
            "Serving Size: 3 Capsules",
            "Ingredients: Pumpkin Seed Oil",
        ])
        self.assertEqual([chunk.dom_hint for chunk in fact_chunks], ["facts-table-1", "facts-table-2"])


if __name__ == "__main__":
    unittest.main()
