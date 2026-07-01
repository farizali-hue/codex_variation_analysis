from __future__ import annotations

import unittest

from amazon_scraper.scraper import AmazonScraper


class ExpandableSectionTests(unittest.TestCase):
    def test_matches_named_product_detail_sections(self) -> None:
        self.assertTrue(AmazonScraper._should_expand_product_section("Item details", ""))
        self.assertTrue(AmazonScraper._should_expand_product_section("", "productFactsDesktopExpander_feature_div"))
        self.assertTrue(AmazonScraper._should_expand_product_section("Top highlights", "accordion-row"))

    def test_ignores_variation_text(self) -> None:
        self.assertFalse(AmazonScraper._should_expand_product_section("Vanilla", "flavor-name swatch"))

    def test_returns_canonical_section_key(self) -> None:
        self.assertEqual(
            AmazonScraper._match_expandable_section_key(
                "Top highlights brand everyday dose item form powder",
                "accordion-row productFactsDesktopExpander_feature_div",
            ),
            "top highlights",
        )


if __name__ == "__main__":
    unittest.main()
