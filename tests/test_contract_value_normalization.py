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
