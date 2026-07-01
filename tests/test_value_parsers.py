from __future__ import annotations

import unittest

from amazon_scraper.value_parsers import normalize_amount_to_mg, parse_value


class ValueParserTests(unittest.TestCase):
    def test_parse_serving_size_extracts_serving_count(self) -> None:
        self.assertEqual(parse_value("serving_size", "Serving Size: 3 Capsules"), "3 Capsules")

    def test_parse_packaging_type_normalizes_common_variants(self) -> None:
        self.assertEqual(parse_value("packaging_type", "Resealable stand-up pouch"), "pouch")
        self.assertEqual(parse_value("packaging_type", "Stick Packs"), "stick_pack")

    def test_parse_servings_per_container_returns_numeric_count(self) -> None:
        self.assertEqual(parse_value("servings_per_container", "Servings Per Container: 30"), "30")
        self.assertEqual(parse_value("servings_per_container", "30 servings"), "30")

    def test_normalize_amount_to_mg_converts_supported_mass_units(self) -> None:
        self.assertEqual(normalize_amount_to_mg("500 mg"), 500.0)
        self.assertEqual(normalize_amount_to_mg("1 g"), 1000.0)
        self.assertEqual(normalize_amount_to_mg("250 mcg"), 0.25)

    def test_normalize_amount_to_mg_accepts_grouped_numbers_and_broader_mass_units(self) -> None:
        self.assertEqual(normalize_amount_to_mg("12,000 mg"), 12000.0)
        self.assertEqual(normalize_amount_to_mg("12,000 mgs"), 12000.0)
        self.assertAlmostEqual(normalize_amount_to_mg("1 lb") or 0.0, 453592.37)
        self.assertAlmostEqual(normalize_amount_to_mg("2 oz") or 0.0, 56699.04625)

    def test_normalize_amount_to_mg_returns_none_for_non_mass_or_missing_values(self) -> None:
        self.assertIsNone(normalize_amount_to_mg(""))
        self.assertIsNone(normalize_amount_to_mg("1000 IU"))
        self.assertIsNone(normalize_amount_to_mg("2 capsules"))


if __name__ == "__main__":
    unittest.main()
