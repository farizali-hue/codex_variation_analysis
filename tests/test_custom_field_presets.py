from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from amazon_scraper.custom_field_presets import (
    CustomFieldPreset,
    apply_custom_field_preset,
    load_custom_field_presets,
    save_custom_field_presets,
)
from amazon_scraper.models import FieldResult


class CustomFieldPresetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path.cwd() / ".test_artifacts" / "custom_field_presets"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.schema_path = self.temp_dir / "custom_field_presets.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_load_round_trip(self) -> None:
        presets = {
            "flavor_note": CustomFieldPreset(output_type="text", merge_similar_values=True),
            "capsule_count": CustomFieldPreset(output_type="number", merge_similar_values=False),
        }

        save_custom_field_presets(presets, schema_path=self.schema_path)
        loaded = load_custom_field_presets(schema_path=self.schema_path)

        self.assertEqual(loaded, presets)

    def test_apply_number_preset_extracts_single_numeric_value(self) -> None:
        result = apply_custom_field_preset(
            "capsule_count",
            FieldResult(value="120 quick release softgels", source="image", confidence=0.9, evidence=""),
            presets={"capsule_count": CustomFieldPreset(output_type="number")},
        )

        self.assertEqual(result.value, "120")

    def test_apply_short_list_preset_normalizes_delimiters_and_deduplicates(self) -> None:
        result = apply_custom_field_preset(
            "free_from",
            FieldResult(
                value="Gluten, Soy; Dairy | Gluten",
                source="image",
                confidence=0.9,
                evidence="",
            ),
            presets={"free_from": CustomFieldPreset(output_type="short_list")},
        )

        self.assertEqual(result.value, "Gluten | Soy | Dairy")

    def test_apply_text_preset_can_merge_obvious_synonyms(self) -> None:
        result = apply_custom_field_preset(
            "flavor_note",
            FieldResult(value="No Flavour", source="image", confidence=0.88, evidence=""),
            presets={"flavor_note": CustomFieldPreset(output_type="text", merge_similar_values=True)},
        )

        self.assertEqual(result.value, "unflavored")

    def test_apply_preset_preserves_blank_value_when_only_abstention_evidence_exists(self) -> None:
        evidence = "No evidence chunk explicitly states a flavor value or flavor label/value pair."
        result = apply_custom_field_preset(
            "flavor_note",
            FieldResult(value="", source="html", confidence=0.2, evidence=evidence),
            presets={"flavor_note": CustomFieldPreset(output_type="text", merge_similar_values=True)},
        )

        self.assertEqual(result.value, "")
        self.assertEqual(result.evidence, evidence)


if __name__ == "__main__":
    unittest.main()
