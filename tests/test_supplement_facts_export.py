from __future__ import annotations

import csv
import shutil
import unittest
from datetime import datetime, timezone
from pathlib import Path

from amazon_scraper.csv_export import write_results_csv
from amazon_scraper.models import (
    FieldResult,
    ScrapeResult,
    SupplementFactsDetail,
    SupplementFactsIngredient,
    SupplementFactsMetadataEntry,
)


class SupplementFactsExportTests(unittest.TestCase):
    def test_detail_mode_flattens_metadata_and_run_scoped_ingredient_columns(self) -> None:
        temp_dir = Path.cwd() / ".test_artifacts" / "supplement_facts_export"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            output_path = temp_dir / "detail_results.csv"
            results = [
                ScrapeResult(
                    asin="ASIN_A",
                    product_type="supplement",
                    status="OK",
                    url="https://example.com/a",
                    scraped_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                    title="Supplement A",
                    error=None,
                    supplement_facts_detail=SupplementFactsDetail(
                        metadata=[
                            SupplementFactsMetadataEntry(
                                label="Serving Size",
                                raw_metadata_text="2 Capsules",
                                final_csv_value="2 Capsules",
                            ),
                            SupplementFactsMetadataEntry(
                                label="Servings Per Container",
                                raw_metadata_text="30",
                                final_csv_value="30",
                            ),
                        ],
                        ingredients=[
                            SupplementFactsIngredient(
                                name="Sodium",
                                raw_amount_text="15 mg",
                                normalized_amount_mg=15.0,
                                csv_value="15",
                                category="nutrient",
                                daily_value_text="63%",
                                footnote_markers=["*"],
                            ),
                            SupplementFactsIngredient(
                                name="Vitamin C",
                                raw_amount_text="500 mg",
                                normalized_amount_mg=500.0,
                                csv_value="500",
                                category="ingredient",
                            ),
                            SupplementFactsIngredient(
                                name="Proprietary Herbal Blend",
                                raw_amount_text="1000 mg",
                                normalized_amount_mg=1000.0,
                                csv_value="1000",
                                category="proprietary_blend",
                            ),
                            SupplementFactsIngredient(
                                name="Ashwagandha",
                                raw_amount_text="",
                                normalized_amount_mg=None,
                                csv_value="grammage not mentioned",
                                category="blend_sub_ingredient",
                                parent_blend_name="Proprietary Herbal Blend",
                            ),
                        ],
                        other_ingredients="Vegetable cellulose, magnesium stearate",
                        unparsed_fragments=["Vitamin D3 1000 IU"],
                    ),
                ),
                ScrapeResult(
                    asin="ASIN_B",
                    product_type="supplement",
                    status="OK",
                    url="https://example.com/b",
                    scraped_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                    title="Supplement B",
                    error=None,
                    supplement_facts_detail=SupplementFactsDetail(
                        metadata=[
                            SupplementFactsMetadataEntry(
                                label="Serving Size",
                                raw_metadata_text="1 Scoop",
                                final_csv_value="1 Scoop",
                            ),
                            SupplementFactsMetadataEntry(
                                label="Product name",
                                raw_metadata_text="Supplement B",
                                final_csv_value="Supplement B",
                            ),
                            SupplementFactsMetadataEntry(
                                label="% DV",
                                raw_metadata_text="***",
                                final_csv_value="***",
                            ),
                        ],
                        ingredients=[
                            SupplementFactsIngredient(
                                name="Potassium",
                                raw_amount_text="40 mg",
                                normalized_amount_mg=40.0,
                                csv_value="40",
                                category="nutrient",
                            ),
                            SupplementFactsIngredient(
                                name="vitamin c",
                                raw_amount_text="250 mg",
                                normalized_amount_mg=250.0,
                                csv_value="250",
                                category="ingredient",
                            ),
                            SupplementFactsIngredient(
                                name="Vitamin C (as Ascorbic Acid)",
                                raw_amount_text="1000 mg",
                                normalized_amount_mg=1000.0,
                                csv_value="1000",
                                category="ingredient",
                            ),
                        ],
                        other_ingredients="Natural flavors",
                        unparsed_fragments=[],
                    ),
                ),
            ]

            written_path = write_results_csv(
                results=results,
                output_file=output_path,
                extract_supplement_facts_details=True,
            )

            self.assertEqual(written_path, output_path)
            with written_path.open("r", encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
                fieldnames = list(reader.fieldnames or [])
            sidecar_path = output_path.with_name(f"{output_path.stem}_supplement_facts_rows{output_path.suffix}")
            with sidecar_path.open("r", encoding="utf-8", newline="") as file:
                sidecar_reader = csv.DictReader(file)
                sidecar_rows = list(sidecar_reader)
                sidecar_fieldnames = list(sidecar_reader.fieldnames or [])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertEqual(
            fieldnames[:9],
            [
                "asin",
                "product_type",
                "status",
                "url",
                "scraped_at",
                "title",
                "error",
                "Other ingredients",
                "unparsed_ingredients",
            ],
        )
        self.assertEqual(
            fieldnames[9:11],
            ["Serving Size", "Servings Per Container"],
        )
        self.assertEqual(
            fieldnames[11:17],
            [
                "Sodium",
                "Vitamin C",
                "Proprietary Herbal Blend",
                "Ashwagandha",
                "Potassium",
                "Vitamin C (as Ascorbic Acid)",
            ],
        )
        self.assertNotIn("Product name", fieldnames)
        self.assertNotIn("% DV", fieldnames)

        self.assertEqual(rows[0]["Serving Size"], "2 Capsules")
        self.assertEqual(rows[0]["Servings Per Container"], "30")
        self.assertEqual(rows[0]["Sodium"], "15")
        self.assertEqual(rows[0]["Vitamin C"], "500")
        self.assertEqual(rows[0]["Proprietary Herbal Blend"], "1000")
        self.assertEqual(rows[0]["Ashwagandha"], "grammage not mentioned")
        self.assertEqual(rows[0]["Other ingredients"], "Vegetable cellulose, magnesium stearate")
        self.assertEqual(rows[0]["unparsed_ingredients"], "Vitamin D3 1000 IU")

        self.assertEqual(rows[1]["Serving Size"], "1 Scoop")
        self.assertEqual(rows[1]["Potassium"], "40")
        self.assertEqual(rows[1]["Vitamin C"], "250")
        self.assertEqual(rows[1]["Vitamin C (as Ascorbic Acid)"], "1000")
        self.assertEqual(rows[1]["Other ingredients"], "Natural flavors")
        self.assertEqual(
            sidecar_fieldnames,
            [
                "asin",
                "row_order",
                "name",
                "amount_text",
                "amount_mg",
                "daily_value_text",
                "footnote_markers",
                "notes",
                "category",
                "parent_blend_name",
                "source",
                "confidence",
                "evidence",
            ],
        )
        self.assertEqual(sidecar_rows[0]["asin"], "ASIN_A")
        self.assertEqual(sidecar_rows[0]["name"], "Sodium")
        self.assertEqual(sidecar_rows[0]["amount_text"], "15 mg")
        self.assertEqual(sidecar_rows[0]["amount_mg"], "15")
        self.assertEqual(sidecar_rows[0]["daily_value_text"], "63%")
        self.assertEqual(sidecar_rows[0]["footnote_markers"], "*")
        self.assertEqual(sidecar_rows[0]["category"], "nutrient")

    def test_detail_mode_canonicalizes_approved_metadata_columns_at_export_boundary(self) -> None:
        temp_dir = Path.cwd() / ".test_artifacts" / "supplement_facts_export"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            output_path = temp_dir / "detail_results.csv"
            results = [
                ScrapeResult(
                    asin="ASIN_A",
                    product_type="supplement",
                    status="OK",
                    url="https://example.com/a",
                    scraped_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                    title="Supplement A",
                    error=None,
                    supplement_facts_detail=SupplementFactsDetail(
                        metadata=[
                            SupplementFactsMetadataEntry(
                                label="Product name",
                                raw_metadata_text="Beet Root Capsules",
                                final_csv_value="Beet Root Capsules",
                            ),
                            SupplementFactsMetadataEntry(
                                label="Serving Size",
                                raw_metadata_text="2 Capsules",
                                final_csv_value="2 Capsules",
                            ),
                            SupplementFactsMetadataEntry(
                                label="Serving Per Container",
                                raw_metadata_text="100",
                                final_csv_value="100",
                            ),
                            SupplementFactsMetadataEntry(
                                label="% DV",
                                raw_metadata_text="***",
                                final_csv_value="***",
                            ),
                        ],
                        ingredients=[],
                        other_ingredients="",
                        unparsed_fragments=[],
                    ),
                )
            ]

            written_path = write_results_csv(
                results=results,
                output_file=output_path,
                extract_supplement_facts_details=True,
            )

            with written_path.open("r", encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
                fieldnames = list(reader.fieldnames or [])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertIn("Serving Size", fieldnames)
        self.assertIn("Servings Per Container", fieldnames)
        self.assertNotIn("Serving Per Container", fieldnames)
        self.assertNotIn("Product name", fieldnames)
        self.assertNotIn("% DV", fieldnames)
        self.assertEqual(rows[0]["Serving Size"], "2 Capsules")
        self.assertEqual(rows[0]["Servings Per Container"], "100")

    def test_detail_mode_preserves_fallback_fields_when_panel_missing_or_partial(self) -> None:
        temp_dir = Path.cwd() / ".test_artifacts" / "supplement_facts_export"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            output_path = temp_dir / "detail_results.csv"
            results = [
                # Full detail present: structured values must win over fallbacks.
                ScrapeResult(
                    asin="ASIN_A",
                    product_type="supplement",
                    status="OK",
                    url="https://example.com/a",
                    scraped_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                    title="Supplement A",
                    error=None,
                    fields={
                        "serving_size": FieldResult(value="FALLBACK_SHOULD_NOT_WIN"),
                        "ingredients": FieldResult(value="FALLBACK_INGREDIENTS_SHOULD_NOT_APPEAR"),
                    },
                    supplement_facts_detail=SupplementFactsDetail(
                        metadata=[
                            SupplementFactsMetadataEntry(
                                label="Serving Size",
                                raw_metadata_text="2 Capsules",
                                final_csv_value="2 Capsules",
                            ),
                        ],
                        ingredients=[
                            SupplementFactsIngredient(
                                name="Sodium",
                                raw_amount_text="15 mg",
                                normalized_amount_mg=15.0,
                                csv_value="15",
                                category="nutrient",
                            ),
                        ],
                    ),
                ),
                # Partial detail: has ingredients + Servings Per Container, but no
                # Serving Size metadata; the serving_size fallback must fill it.
                ScrapeResult(
                    asin="ASIN_B",
                    product_type="supplement",
                    status="OK",
                    url="https://example.com/b",
                    scraped_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                    title="Supplement B",
                    error=None,
                    fields={
                        "serving_size": FieldResult(value="1 Scoop"),
                        "servings_per_container": FieldResult(value="FALLBACK_SHOULD_NOT_WIN"),
                    },
                    supplement_facts_detail=SupplementFactsDetail(
                        metadata=[
                            SupplementFactsMetadataEntry(
                                label="Servings Per Container",
                                raw_metadata_text="60",
                                final_csv_value="60",
                            ),
                        ],
                        ingredients=[
                            SupplementFactsIngredient(
                                name="Potassium",
                                raw_amount_text="40 mg",
                                normalized_amount_mg=40.0,
                                csv_value="40",
                                category="nutrient",
                            ),
                        ],
                    ),
                ),
                # No detail at all (panel failed): every handled fallback must survive.
                ScrapeResult(
                    asin="ASIN_C",
                    product_type="supplement",
                    status="OK",
                    url="https://example.com/c",
                    scraped_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                    title="Supplement C",
                    error=None,
                    fields={
                        "serving_size": FieldResult(value="3 Tablets"),
                        "servings_per_container": FieldResult(value="20"),
                        "ingredients": FieldResult(value="Turmeric, Black Pepper"),
                    },
                    supplement_facts_detail=None,
                ),
            ]

            written_path = write_results_csv(
                results=results,
                output_file=output_path,
                extract_supplement_facts_details=True,
            )

            with written_path.open("r", encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
                fieldnames = list(reader.fieldnames or [])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self.assertIn("Serving Size", fieldnames)
        self.assertIn("Servings Per Container", fieldnames)
        self.assertIn("ingredients", fieldnames)

        rows_by_asin = {row["asin"]: row for row in rows}

        # ASIN_A: structured values win; ingredients fallback not emitted (represented).
        self.assertEqual(rows_by_asin["ASIN_A"]["Serving Size"], "2 Capsules")
        self.assertEqual(rows_by_asin["ASIN_A"]["ingredients"], "")

        # ASIN_B: serving_size fallback fills the missing metadata; structured
        # servings-per-container wins over its fallback; structured ingredients present.
        self.assertEqual(rows_by_asin["ASIN_B"]["Serving Size"], "1 Scoop")
        self.assertEqual(rows_by_asin["ASIN_B"]["Servings Per Container"], "60")
        self.assertEqual(rows_by_asin["ASIN_B"]["ingredients"], "")

        # ASIN_C: no panel, so the HTML/image fallbacks are preserved, not dropped.
        self.assertEqual(rows_by_asin["ASIN_C"]["Serving Size"], "3 Tablets")
        self.assertEqual(rows_by_asin["ASIN_C"]["Servings Per Container"], "20")
        self.assertEqual(rows_by_asin["ASIN_C"]["ingredients"], "Turmeric, Black Pepper")

    def test_detail_mode_creates_fallback_columns_when_no_panel_in_batch(self) -> None:
        temp_dir = Path.cwd() / ".test_artifacts" / "supplement_facts_export"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            output_path = temp_dir / "detail_results.csv"
            results = [
                ScrapeResult(
                    asin="ASIN_ONLY",
                    product_type="supplement",
                    status="OK",
                    url="https://example.com/only",
                    scraped_at=datetime(2026, 4, 21, tzinfo=timezone.utc),
                    title="Supplement Only",
                    error=None,
                    fields={
                        "serving_size": FieldResult(value="2 Softgels"),
                        "servings_per_container": FieldResult(value="45"),
                        "ingredients": FieldResult(value="Fish Oil, Vitamin E"),
                    },
                    supplement_facts_detail=None,
                )
            ]

            written_path = write_results_csv(
                results=results,
                output_file=output_path,
                extract_supplement_facts_details=True,
            )

            with written_path.open("r", encoding="utf-8", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
                fieldnames = list(reader.fieldnames or [])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        # No result carried a structured panel, yet the handled fields still get
        # columns created from the fallback values (otherwise silently dropped).
        self.assertIn("Serving Size", fieldnames)
        self.assertIn("Servings Per Container", fieldnames)
        self.assertIn("ingredients", fieldnames)
        self.assertEqual(rows[0]["Serving Size"], "2 Softgels")
        self.assertEqual(rows[0]["Servings Per Container"], "45")
        self.assertEqual(rows[0]["ingredients"], "Fish Oil, Vitamin E")


if __name__ == "__main__":
    unittest.main()
