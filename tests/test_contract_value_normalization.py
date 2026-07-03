from __future__ import annotations

import unittest

from amazon_scraper.contract_value_normalization import (
    build_output_type_instruction,
    normalize_field_result_for_contract,
)
from amazon_scraper.models import FieldResult, RunFieldContract


class ContractValueNormalizationTests(unittest.TestCase):
    def test_number_contract_extracts_single_numeric_value(self) -> None:
        result = normalize_field_result_for_contract(
            FieldResult(value="120 quick release softgels", source="image", confidence=0.9, evidence=""),
            RunFieldContract(
                requested_label="Capsule counts",
                internal_key="capsule_counts",
                output_type="number",
            ),
        )

        self.assertEqual(result.value, "120")

    def test_number_contract_preserves_thousands_separators(self) -> None:
        contract = RunFieldContract(
            requested_label="Capsule counts",
            internal_key="capsule_counts",
            output_type="number",
        )
        cases = {
            "1,000 capsules": "1000",
            "1,000 mg": "1000",
            "1,234,567 units": "1234567",
            "1,000.50 mg": "1000.5",
            "500 mg": "500",
            "2.50": "2.5",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                result = normalize_field_result_for_contract(
                    FieldResult(value=raw, source="image", confidence=0.9, evidence=""),
                    contract,
                )
                self.assertEqual(result.value, expected)

    def test_short_list_contract_normalizes_delimiters_and_deduplicates(self) -> None:
        result = normalize_field_result_for_contract(
            FieldResult(
                value="Gluten, Soy; Dairy | Gluten",
                source="image",
                confidence=0.9,
                evidence="",
            ),
            RunFieldContract(
                requested_label="Free from",
                internal_key="free_from",
                output_type="short_list",
            ),
        )

        self.assertEqual(result.value, "Gluten | Soy | Dairy")

    def test_short_list_contract_keeps_commas_inside_parentheses(self) -> None:
        # Botanical qualifiers like "(Curcuma longa, root)" are part of ONE item;
        # splitting on the inner comma fabricates two broken list entries.
        result = normalize_field_result_for_contract(
            FieldResult(
                value="Turmeric Root Extract (Curcuma longa, 95% curcuminoids), Black Pepper",
                source="image",
                confidence=0.9,
                evidence="",
            ),
            RunFieldContract(
                requested_label="Ingredients",
                internal_key="ingredients",
                output_type="short_list",
            ),
        )

        self.assertEqual(
            result.value,
            "Turmeric Root Extract (Curcuma longa, 95% curcuminoids) | Black Pepper",
        )

    def test_short_list_contract_keeps_and_inside_parentheses_as_one_item(self) -> None:
        result = normalize_field_result_for_contract(
            FieldResult(value="Chocolate (rich and creamy)", source="image", confidence=0.9, evidence=""),
            RunFieldContract(
                requested_label="Flavor options",
                internal_key="flavor_options",
                output_type="short_list",
            ),
        )

        self.assertEqual(result.value, "Chocolate (rich and creamy)")

    def test_text_contract_keeps_one_cleaned_text_value(self) -> None:
        result = normalize_field_result_for_contract(
            FieldResult(value="  French   Vanilla  ", source="image", confidence=0.9, evidence=""),
            RunFieldContract(
                requested_label="Flavor",
                internal_key="flavor",
                output_type="text",
                merge_similar_values=True,
            ),
        )

        self.assertEqual(result.value, "French Vanilla")

    def test_contract_preserves_blank_value_when_only_abstention_evidence_exists(self) -> None:
        evidence = "No evidence chunk explicitly states a flavor value or flavor label/value pair."
        result = normalize_field_result_for_contract(
            FieldResult(value="", source="html", confidence=0.2, evidence=evidence),
            RunFieldContract(
                requested_label="Flavor",
                internal_key="flavor",
                output_type="text",
                merge_similar_values=True,
            ),
        )

        self.assertEqual(result.value, "")
        self.assertEqual(result.evidence, evidence)

    def test_output_type_instruction_is_specific_to_number_contract(self) -> None:
        instruction = build_output_type_instruction(
            RunFieldContract(
                requested_label="Capsule counts",
                internal_key="capsule_counts",
                output_type="number",
            )
        )

        self.assertIn("only one numeric value", instruction)


if __name__ == "__main__":
    unittest.main()
