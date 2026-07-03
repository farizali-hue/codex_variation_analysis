from __future__ import annotations

import unittest

from amazon_scraper.input_normalization import (
    build_jobs_from_table_rows,
    build_jobs_from_user_input,
    collect_requested_field_columns,
    normalize_requested_field_input,
    parse_asin_input,
    resolve_field_name,
)
from amazon_scraper.models import RunFieldContract, ScrapeJob


class InputNormalizationTests(unittest.TestCase):
    def test_parse_asin_input_accepts_mixed_separators_and_deduplicates(self) -> None:
        asins, issues = parse_asin_input("B0DJWV4BQ1\nB0FSGHP1FC, B0DJWV4BQ1;B0FRVXJTW2")

        self.assertEqual(asins, ["B0DJWV4BQ1", "B0FSGHP1FC", "B0FRVXJTW2"])
        self.assertEqual([issue.level for issue in issues], ["warning"])

    def test_resolve_field_name_accepts_alias(self) -> None:
        selection = resolve_field_name("ingredient list")

        self.assertEqual(selection.canonical_name, "ingredients")
        self.assertEqual(selection.matched_by, "alias")

    def test_resolve_field_name_accepts_fuzzy_typo(self) -> None:
        selection = resolve_field_name("packagng type")

        self.assertEqual(selection.canonical_name, "packaging_type")
        self.assertEqual(selection.matched_by, "fuzzy")

    def test_resolve_field_name_prefers_generated_field_over_extraction_only_label_alias(self) -> None:
        selection = resolve_field_name("flavor")

        self.assertEqual(selection.canonical_name, "flavor")
        self.assertEqual(selection.matched_by, "generated")

    def test_resolve_field_name_keeps_safe_input_aliases(self) -> None:
        selection = resolve_field_name("Packaging")

        self.assertEqual(selection.canonical_name, "packaging_type")
        self.assertEqual(selection.matched_by, "alias")

    def test_resolve_field_name_prefers_generated_field_for_ambiguous_single_word_input(self) -> None:
        selection = resolve_field_name("servings")

        self.assertEqual(selection.canonical_name, "servings")
        self.assertEqual(selection.matched_by, "generated")

    def test_resolve_field_name_accepts_new_custom_field(self) -> None:
        selection = resolve_field_name("No of capsules")

        self.assertEqual(selection.canonical_name, "number_of_capsules")
        self.assertEqual(selection.matched_by, "generated")

    def test_resolve_field_name_accepts_custom_field_even_when_supported_suggestions_exist(self) -> None:
        selection = resolve_field_name("total_servings")

        self.assertEqual(selection.canonical_name, "total_servings")
        self.assertEqual(selection.matched_by, "generated")

    def test_resolve_field_name_rejects_reserved_export_column_names(self) -> None:
        for raw_input in ("ASIN", "Title", "URL", "Status", "Error", "Scraped At", "Product Type"):
            with self.subTest(raw_input=raw_input):
                with self.assertRaisesRegex(ValueError, "reserved"):
                    resolve_field_name(raw_input)

    def test_build_jobs_reports_reserved_field_and_keeps_remaining_fields(self) -> None:
        result = build_jobs_from_user_input(
            raw_asins="B0DJWV4BQ1",
            raw_fields=["ASIN", "ingredients"],
            product_type="",
            product_type_defaults={},
        )

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].requested_fields, ["ingredients"])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("reserved", result.errors[0])

    def test_build_jobs_uses_product_type_defaults_when_fields_are_blank(self) -> None:
        result = build_jobs_from_user_input(
            raw_asins="B0DJWV4BQ1\nB0FSGHP1FC",
            raw_fields=[],
            product_type="supplement",
            product_type_defaults={
                "supplement": ["serving_size", "ingredients"],
            },
        )

        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.jobs[0].requested_fields, ["serving_size", "ingredients"])
        self.assertFalse(result.errors)

    def test_build_jobs_accepts_custom_fields(self) -> None:
        result = build_jobs_from_user_input(
            raw_asins="B0DJWV4BQ1",
            raw_fields=["No of capsules", "daily value", "total_servings"],
            product_type="",
            product_type_defaults={},
        )

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(
            result.jobs[0].requested_fields,
            ["number_of_capsules", "daily_value", "total_servings"],
        )
        self.assertFalse(result.errors)

    def test_normalize_requested_field_input_preserves_raw_labels_and_order(self) -> None:
        normalized_fields, selections, issues = normalize_requested_field_input(
            "No of capsules, packagng type, No of capsules"
        )

        self.assertEqual(normalized_fields, ["number_of_capsules", "packaging_type"])
        self.assertEqual([selection.raw_input for selection in selections], ["No of capsules", "packagng type"])
        self.assertEqual(
            [selection.canonical_name for selection in selections],
            ["number_of_capsules", "packaging_type"],
        )
        self.assertEqual([selection.matched_by for selection in selections], ["generated", "fuzzy"])
        self.assertEqual([issue.level for issue in issues], ["warning"])

    def test_build_jobs_from_table_rows_allows_mixed_product_types_and_fields(self) -> None:
        result = build_jobs_from_table_rows(
            [
                {
                    "asin": "B0DJWV4BQ1",
                    "product_type": "supplement",
                    "required_fields": "",
                },
                {
                    "asin": "B0FSGHP1FC",
                    "product_type": "protein_powder",
                    "required_fields": "packaging, ingredient list",
                },
            ],
            product_type_defaults={
                "supplement": ["serving_size", "ingredients"],
                "protein_powder": ["packaging_type"],
            },
        )

        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.jobs[0].product_type, "supplement")
        self.assertEqual(result.jobs[0].requested_fields, ["serving_size", "ingredients"])
        self.assertEqual(result.jobs[1].product_type, "protein_powder")
        self.assertEqual(result.jobs[1].requested_fields, ["packaging_type", "ingredients"])

    def test_collect_requested_field_columns_includes_run_scoped_contract_keys(self) -> None:
        columns = collect_requested_field_columns(
            [
                ScrapeJob(
                    asin="B0TESTING1",
                    requested_fields=["ingredients"],
                    field_contracts=[
                        RunFieldContract(
                            requested_label="Flavor",
                            internal_key="flavor",
                            output_type="text",
                            matched_by="generated",
                        )
                    ],
                )
            ]
        )

        self.assertEqual(columns, ["ingredients", "flavor"])


if __name__ == "__main__":
    unittest.main()
