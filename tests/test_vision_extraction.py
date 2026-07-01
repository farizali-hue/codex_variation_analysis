from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from amazon_scraper.gallery_images import GalleryImageRecord
from amazon_scraper.vision_extraction import VisionFieldResolver


class _FakeVisionClient:
    def __init__(
        self,
        *,
        classification_images: list[dict[str, object]] | None = None,
        facts_responses: dict[str, dict[str, object]] | None = None,
        facts_detail_responses: dict[str, dict[str, object]] | None = None,
        generic_responses: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.classification_images = classification_images or [
            {
                "slot_index": 0,
                "physical_id": "front",
                "roles": ["packaging", "marketing_text"],
                "packaging_type_enum": "pouch",
                "packaging_type_raw": "stand-up pouch",
                "readability": "medium",
                "visible_text_summary": "Vanilla bean pouch front label",
                "confidence": 0.92,
            },
            {
                "slot_index": 1,
                "physical_id": "facts",
                "roles": ["facts_panel"],
                "packaging_type_enum": "",
                "packaging_type_raw": "",
                "readability": "high",
                "visible_text_summary": "Supplement facts panel ingredients serving size",
                "confidence": 0.97,
            },
        ]
        self.facts_responses = facts_responses or {
            "facts": {
                "ingredients": "Cordyceps, Lion's Mane",
                "serving_size": "30 servings",
                "servings_per_container": "30",
                "evidence": "Supplement Facts panel",
                "confidence": 0.94,
            }
        }
        self.facts_detail_responses = facts_detail_responses or {
            "facts": {
                "panel_detected": True,
                "confidence": 0.94,
                "evidence": "Supplement Facts panel",
                "metadata": [
                    {"label": "Serving Size", "raw_value_text": "30 servings"},
                    {"label": "Servings Per Container", "raw_value_text": "30"},
                ],
                "ingredients": [
                    {"name": "Cordyceps", "raw_amount_text": "", "category": "ingredient"},
                    {"name": "Lion's Mane", "raw_amount_text": "", "category": "ingredient"},
                ],
                "other_ingredients": "",
                "unparsed_fragments": [],
            }
        }
        self.generic_responses = generic_responses or {
            "flavor_note": {
                "value": "Vanilla Bean",
                "evidence": "Vanilla bean pouch front label",
                "source": "image",
                "confidence": 0.88,
            },
            "totally_unknown_field": {
                "value": "mystery",
                "evidence": "unclear badge",
                "source": "image",
                "confidence": 0.25,
            },
        }

    def create_chat_completion(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        asin: str,
        field: str,
        retry_attempt: int = 0,
        model: str | None = None,
        image_inputs: list[dict[str, str]] | None = None,
    ) -> str:
        self.calls.append(
            {
                "field": field,
                "asin": asin,
                "model": model,
                "image_inputs": image_inputs or [],
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        if field == "__image_classification__":
            return json.dumps({"images": self.classification_images})
        if field == "__facts_panel__":
            image_url = str((image_inputs or [{}])[0].get("url", "") or "")
            for url_fragment, payload in self.facts_responses.items():
                if url_fragment in image_url:
                    return json.dumps(payload)
            raise AssertionError(f"Unexpected facts image URL: {image_url}")
        if field == "__facts_panel_detail__":
            image_url = str((image_inputs or [{}])[0].get("url", "") or "")
            for url_fragment, payload in self.facts_detail_responses.items():
                if url_fragment in image_url:
                    return json.dumps(payload)
            raise AssertionError(f"Unexpected detailed facts image URL: {image_url}")
        if field in self.generic_responses:
            return json.dumps(self.generic_responses[field])
        raise AssertionError(f"Unexpected field: {field}")


class VisionFieldResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        root_dir = Path.cwd() / ".test_artifacts"
        root_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = root_dir / f"vision_resolver_{uuid4().hex}"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_resolve_fields_returns_targeted_image_results(self) -> None:
        resolver = VisionFieldResolver(
            client=_FakeVisionClient(),
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )
        images = [
            GalleryImageRecord(
                slot_index=0,
                variant="MAIN",
                large_url="https://example.com/front-large.jpg",
                hi_res_url="https://example.com/front-hires.jpg",
                main_urls=["https://example.com/front-main.jpg"],
                physical_id="front",
            ),
            GalleryImageRecord(
                slot_index=1,
                variant="PT01",
                large_url="https://example.com/facts-large.jpg",
                hi_res_url="https://example.com/facts-hires.jpg",
                main_urls=["https://example.com/facts-main.jpg"],
                physical_id="facts",
            ),
        ]

        results = resolver.resolve_fields(
            asin="B0TEST",
            requested_fields=[
                "packaging_type",
                "ingredients",
                "serving_size",
                "servings_per_container",
            ],
            images=images,
        )

        self.assertEqual(results["packaging_type"].value, "pouch")
        self.assertEqual(results["packaging_type"].source, "https://example.com/front-hires.jpg")
        self.assertEqual(results["ingredients"].value, "Cordyceps, Lion's Mane")
        self.assertEqual(results["serving_size"].value, "30 servings")
        self.assertEqual(results["servings_per_container"].value, "30")
        self.assertEqual(results["ingredients"].source, "https://example.com/facts-hires.jpg")

    def test_resolve_fields_supports_generic_image_text_fields(self) -> None:
        resolver = VisionFieldResolver(
            client=_FakeVisionClient(),
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )
        images = [
            GalleryImageRecord(
                slot_index=0,
                variant="MAIN",
                large_url="https://example.com/front-large.jpg",
                hi_res_url="https://example.com/front-hires.jpg",
                main_urls=["https://example.com/front-main.jpg"],
                physical_id="front",
            )
        ]

        results = resolver.resolve_fields(
            asin="B0TEST",
            requested_fields=["flavor_note", "totally_unknown_field"],
            images=images,
        )

        self.assertEqual(results["flavor_note"].value, "Vanilla Bean")
        self.assertNotIn("totally_unknown_field", results)

    def test_resolve_fields_rejects_fake_facts_panel_and_uses_next_candidate(self) -> None:
        client = _FakeVisionClient(
            classification_images=[
                {
                    "slot_index": 0,
                    "physical_id": "back",
                    "roles": ["facts_panel", "marketing_text"],
                    "packaging_type_enum": "",
                    "packaging_type_raw": "",
                    "readability": "high",
                    "visible_text_summary": "Directions warning free of back label",
                    "confidence": 0.99,
                },
                {
                    "slot_index": 1,
                    "physical_id": "facts",
                    "roles": ["facts_panel"],
                    "packaging_type_enum": "",
                    "packaging_type_raw": "",
                    "readability": "medium",
                    "visible_text_summary": "Supplement facts panel serving size ingredients",
                    "confidence": 0.92,
                },
            ],
            facts_responses={
                "back": {
                    "ingredients": "",
                    "serving_size": "",
                    "servings_per_container": "",
                    "evidence": "Directions and warning label",
                    "confidence": 0.98,
                },
                "facts": {
                    "ingredients": "Coenzyme Q-10, Black Pepper Extract",
                    "serving_size": "2 capsules",
                    "servings_per_container": "60",
                    "evidence": "Supplement Facts panel",
                    "confidence": 0.91,
                },
            },
        )
        resolver = VisionFieldResolver(
            client=client,
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )
        images = [
            GalleryImageRecord(
                slot_index=0,
                variant="PT01",
                large_url="https://example.com/back-large.jpg",
                hi_res_url="https://example.com/back-hires.jpg",
                main_urls=["https://example.com/back-main.jpg"],
                physical_id="back",
            ),
            GalleryImageRecord(
                slot_index=1,
                variant="PT02",
                large_url="https://example.com/actual-facts-large.jpg",
                hi_res_url="https://example.com/actual-facts-hires.jpg",
                main_urls=["https://example.com/actual-facts-main.jpg"],
                physical_id="facts",
            ),
        ]

        results = resolver.resolve_fields(
            asin="B0TEST",
            requested_fields=["ingredients", "serving_size", "servings_per_container"],
            images=images,
        )

        self.assertEqual(results["ingredients"].value, "Coenzyme Q-10, Black Pepper Extract")
        self.assertEqual(results["ingredients"].source, "https://example.com/actual-facts-hires.jpg")
        facts_calls = [call for call in client.calls if call["field"] == "__facts_panel__"]
        self.assertEqual(len(facts_calls), 2)
        self.assertIn("back-hires.jpg", str(facts_calls[0]["image_inputs"][0]["url"]))
        self.assertIn("actual-facts-hires.jpg", str(facts_calls[1]["image_inputs"][0]["url"]))

    def test_resolve_fields_uses_reported_generic_source_candidate(self) -> None:
        client = _FakeVisionClient(
            classification_images=[
                {
                    "slot_index": 0,
                    "physical_id": "front",
                    "roles": ["marketing_text", "packaging"],
                    "packaging_type_enum": "pouch",
                    "packaging_type_raw": "pouch",
                    "readability": "high",
                    "visible_text_summary": "Premium front label",
                    "confidence": 0.99,
                },
                {
                    "slot_index": 1,
                    "physical_id": "side",
                    "roles": ["marketing_text"],
                    "packaging_type_enum": "",
                    "packaging_type_raw": "",
                    "readability": "high",
                    "visible_text_summary": "Vanilla bean label on side panel",
                    "confidence": 0.45,
                },
            ],
            generic_responses={
                "flavor_note": {
                    "value": "Vanilla Bean",
                    "evidence": "Vanilla bean side panel",
                    "source": "image",
                    "source_candidate_index": 1,
                    "confidence": 0.88,
                }
            },
        )
        resolver = VisionFieldResolver(
            client=client,
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )
        images = [
            GalleryImageRecord(
                slot_index=0,
                variant="MAIN",
                large_url="https://example.com/front-large.jpg",
                hi_res_url="https://example.com/front-hires.jpg",
                main_urls=["https://example.com/front-main.jpg"],
                physical_id="front",
            ),
            GalleryImageRecord(
                slot_index=1,
                variant="PT01",
                large_url="https://example.com/side-large.jpg",
                hi_res_url="https://example.com/side-hires.jpg",
                main_urls=["https://example.com/side-main.jpg"],
                physical_id="side",
            ),
        ]

        results = resolver.resolve_fields(
            asin="B0TEST",
            requested_fields=["flavor_note"],
            images=images,
        )

        self.assertEqual(results["flavor_note"].value, "Vanilla Bean")
        self.assertEqual(results["flavor_note"].source, "https://example.com/side-hires.jpg")

    def test_generic_image_prompt_includes_direct_support_abstention_rules(self) -> None:
        client = _FakeVisionClient(
            classification_images=[
                {
                    "slot_index": 0,
                    "physical_id": "front",
                    "roles": ["packaging", "marketing_text"],
                    "packaging_type_enum": "pouch",
                    "packaging_type_raw": "pouch",
                    "readability": "high",
                    "visible_text_summary": "Pumpkin Seed Oil front label",
                    "confidence": 0.99,
                }
            ],
            generic_responses={
                "flavor": {
                    "value": "",
                    "evidence": "",
                    "source_candidate_index": 0,
                    "confidence": 0.0,
                }
            },
        )
        resolver = VisionFieldResolver(
            client=client,
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )
        images = [
            GalleryImageRecord(
                slot_index=0,
                variant="MAIN",
                large_url="https://example.com/front-large.jpg",
                hi_res_url="https://example.com/front-hires.jpg",
                main_urls=["https://example.com/front-main.jpg"],
                physical_id="front",
            )
        ]

        resolver.resolve_fields(
            asin="B0TEST",
            requested_fields=["flavor"],
            images=images,
        )

        flavor_call = next(call for call in client.calls if call["field"] == "flavor")
        self.assertIn("Return a value only when the images directly support the requested field itself.", flavor_call["system_prompt"])
        self.assertIn("Direct support rules:", flavor_call["user_prompt"])
        self.assertIn("Do not infer the field from brand names, product names, ingredient names, dosage text, or general marketing copy.", flavor_call["user_prompt"])
        self.assertIn("does not explicitly answer 'flavor', return an empty value", flavor_call["user_prompt"])

    def test_resolve_job_returns_structured_supplement_facts_detail_when_enabled(self) -> None:
        client = _FakeVisionClient(
            facts_detail_responses={
                "facts": {
                    "panel_detected": True,
                    "confidence": 0.97,
                    "evidence": "Supplement Facts panel with Vitamin C and proprietary blend",
                    "metadata": [
                        {"label": "Serving Size", "raw_value_text": "2 Capsules"},
                        {"label": "Servings Per Container", "raw_value_text": "30"},
                        {"label": "Sodium", "raw_value_text": "15 mg"},
                    ],
                    "ingredients": [
                        {"name": "Vitamin C", "raw_amount_text": "500 mg", "category": "ingredient"},
                        {
                            "name": "Proprietary Herbal Blend",
                            "raw_amount_text": "1000 mg",
                            "category": "proprietary_blend",
                        },
                        {
                            "name": "Ashwagandha",
                            "raw_amount_text": "",
                            "category": "blend_sub_ingredient",
                            "parent_blend_name": "Proprietary Herbal Blend",
                        },
                        {
                            "name": "Rhodiola",
                            "raw_amount_text": "",
                            "category": "blend_sub_ingredient",
                            "parent_blend_name": "Proprietary Herbal Blend",
                        },
                    ],
                    "other_ingredients": "Vegetable cellulose, magnesium stearate",
                    "unparsed_fragments": ["Vitamin D3 1000 IU"],
                }
            }
        )
        resolver = VisionFieldResolver(
            client=client,
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )
        images = [
            GalleryImageRecord(
                slot_index=0,
                variant="PT01",
                large_url="https://example.com/facts-large.jpg",
                hi_res_url="https://example.com/facts-hires.jpg",
                main_urls=["https://example.com/facts-main.jpg"],
                physical_id="facts",
            )
        ]

        result = resolver.resolve_job(
            asin="B0TEST",
            requested_fields=["serving_size", "servings_per_container"],
            images=images,
            extract_supplement_facts_details=True,
        )

        self.assertEqual(result.fields["serving_size"].value, "2 Capsules")
        self.assertEqual(result.fields["servings_per_container"].value, "30")
        self.assertIsNotNone(result.supplement_facts_detail)
        assert result.supplement_facts_detail is not None
        self.assertEqual(result.supplement_facts_detail.other_ingredients, "Vegetable cellulose, magnesium stearate")
        self.assertEqual(result.supplement_facts_detail.unparsed_fragments, ["Vitamin D3 1000 IU"])
        self.assertEqual(
            [entry.label for entry in result.supplement_facts_detail.metadata],
            ["Serving Size", "Servings Per Container"],
        )
        self.assertEqual(result.supplement_facts_detail.metadata[0].final_csv_value, "2 Capsules")
        self.assertEqual(result.supplement_facts_detail.ingredients[0].name, "Vitamin C")
        self.assertEqual(result.supplement_facts_detail.ingredients[0].normalized_amount_mg, 500.0)
        self.assertEqual(result.supplement_facts_detail.ingredients[1].normalized_amount_mg, 1000.0)
        self.assertEqual(result.supplement_facts_detail.ingredients[2].csv_value, "grammage not mentioned")
        detail_calls = [call for call in client.calls if call["field"] == "__facts_panel_detail__"]
        self.assertEqual(len(detail_calls), 1)
        legacy_calls = [call for call in client.calls if call["field"] == "__facts_panel__"]
        self.assertEqual(legacy_calls, [])

    def _facts_image(self) -> GalleryImageRecord:
        return GalleryImageRecord(
            slot_index=0,
            variant="PT01",
            large_url="https://example.com/facts-large.jpg",
            hi_res_url="https://example.com/facts-hires.jpg",
            main_urls=["https://example.com/facts-main.jpg"],
            physical_id="facts",
        )

    def test_detail_mode_abstains_when_panel_not_detected(self) -> None:
        client = _FakeVisionClient(
            facts_detail_responses={
                "facts": {
                    "panel_detected": False,
                    "confidence": 0.95,
                    "metadata": [{"label": "Serving Size", "raw_value_text": "2 Capsules"}],
                    "ingredients": [
                        {"name": "Vitamin C", "raw_amount_text": "500 mg", "category": "ingredient"}
                    ],
                    "other_ingredients": "",
                    "unparsed_fragments": [],
                }
            }
        )
        resolver = VisionFieldResolver(
            client=client,
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )

        result = resolver.resolve_job(
            asin="B0TEST",
            requested_fields=["serving_size"],
            images=[self._facts_image()],
            extract_supplement_facts_details=True,
        )

        # panel_detected=false must abstain even though rows parsed cleanly.
        self.assertIsNone(result.supplement_facts_detail)

    def test_detail_mode_abstains_on_low_confidence_payload(self) -> None:
        client = _FakeVisionClient(
            facts_detail_responses={
                "facts": {
                    "panel_detected": True,
                    "confidence": 0.30,
                    "metadata": [{"label": "Serving Size", "raw_value_text": "2 Capsules"}],
                    "ingredients": [
                        {"name": "Vitamin C", "raw_amount_text": "500 mg", "category": "ingredient"}
                    ],
                    "other_ingredients": "",
                    "unparsed_fragments": [],
                }
            }
        )
        resolver = VisionFieldResolver(
            client=client,
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )

        result = resolver.resolve_job(
            asin="B0TEST",
            requested_fields=["serving_size"],
            images=[self._facts_image()],
            extract_supplement_facts_details=True,
        )

        # Confidence below classification_confidence_threshold (0.55) must abstain,
        # matching the legacy facts path rather than exporting weak panel data.
        self.assertIsNone(result.supplement_facts_detail)

    def test_supplement_facts_detail_parser_enforces_contract_boundary(self) -> None:
        client = _FakeVisionClient(
            facts_detail_responses={
                "facts": {
                    "panel_detected": True,
                    "confidence": 0.98,
                    "evidence": [
                        "Supplement Facts",
                        "Serving Size: 2 Capsules",
                        "Serving Per Container: 100",
                        "Includes 2g Added Sugars 4%",
                    ],
                    "metadata": [
                        {"label": "Product name", "raw_value_text": "Beet Root Capsules"},
                        {"label": "Serving Size", "raw_value_text": "2 Capsules"},
                        {"label": "Serving Per Container", "raw_value_text": "100"},
                        {"label": "% DV", "raw_value_text": "***"},
                        {"label": "Daily Value note", "raw_value_text": "+DV (Daily Value) not established."},
                    ],
                    "ingredients": [
                        {
                            "name": "Includes 2g Added Sugars",
                            "raw_amount_text": "",
                            "daily_value_text": "4%*",
                            "category": "nutrient",
                        },
                        {
                            "name": "Vitamin B12 (as Methylcobalamin)",
                            "raw_amount_text": "25 mcg",
                            "daily_value_text": "1042%",
                            "footnote_markers": ["*"],
                            "category": "nutrient",
                        },
                        {
                            "name": "L-Citrulline",
                            "raw_amount_text": "",
                            "category": "blend_sub_ingredient",
                            "parent_blend_name": "Total Beets Superfood Superblend",
                        },
                    ],
                    "other_ingredients": ["Vegetable Capsule (HPMC)", "Rice Flour"],
                    "unparsed_fragments": [],
                }
            }
        )
        resolver = VisionFieldResolver(
            client=client,
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )
        images = [
            GalleryImageRecord(
                slot_index=0,
                variant="PT01",
                large_url="https://example.com/facts-large.jpg",
                hi_res_url="https://example.com/facts-hires.jpg",
                main_urls=["https://example.com/facts-main.jpg"],
                physical_id="facts",
            )
        ]

        result = resolver.resolve_job(
            asin="B0TEST",
            requested_fields=["serving_size", "servings_per_container"],
            images=images,
            extract_supplement_facts_details=True,
        )

        self.assertEqual(result.fields["serving_size"].value, "2 Capsules")
        self.assertEqual(result.fields["servings_per_container"].value, "100")
        assert result.supplement_facts_detail is not None
        detail = result.supplement_facts_detail
        self.assertEqual(
            [entry.label for entry in detail.metadata],
            ["Serving Size", "Servings Per Container"],
        )
        self.assertEqual(
            [entry.final_csv_value for entry in detail.metadata],
            ["2 Capsules", "100"],
        )
        self.assertEqual(detail.other_ingredients, "Vegetable Capsule (HPMC) | Rice Flour")
        added_sugars = detail.ingredients[0]
        self.assertEqual(added_sugars.name, "Added Sugars")
        self.assertEqual(added_sugars.raw_amount_text, "2 g")
        self.assertEqual(added_sugars.normalized_amount_mg, 2000.0)
        self.assertEqual(added_sugars.daily_value_text, "4%")
        self.assertEqual(added_sugars.footnote_markers, ["*"])
        self.assertEqual(detail.ingredients[1].daily_value_text, "1042%")
        self.assertEqual(detail.ingredients[1].footnote_markers, ["*"])
        self.assertEqual(detail.ingredients[2].name, "L-Citrulline")
        self.assertEqual(detail.ingredients[2].csv_value, "grammage not mentioned")

    def test_supplement_facts_detail_preserves_unitless_and_grouped_numeric_amounts(self) -> None:
        client = _FakeVisionClient(
            facts_detail_responses={
                "facts": {
                    "panel_detected": True,
                    "confidence": 0.96,
                    "evidence": "Nutrition Facts panel with calories and supplement ingredients",
                    "metadata": [],
                    "ingredients": [
                        {
                            "name": "Calories",
                            "raw_amount_text": "10",
                            "category": "nutrient",
                        },
                        {
                            "name": "Creatine Monohydrate 12,000 mgs",
                            "raw_amount_text": "",
                            "category": "ingredient",
                        },
                        {
                            "name": "Bulk Powder",
                            "raw_amount_text": "1 lb",
                            "category": "ingredient",
                        },
                        {
                            "name": "Vitamin D",
                            "raw_amount_text": "1000 IU",
                            "category": "nutrient",
                        },
                    ],
                    "other_ingredients": "",
                    "unparsed_fragments": [],
                }
            }
        )
        resolver = VisionFieldResolver(
            client=client,
            model="gpt-5.4-mini",
            cache_file=self.temp_dir / "vision_cache.json",
        )
        images = [
            GalleryImageRecord(
                slot_index=0,
                variant="PT01",
                large_url="https://example.com/facts-large.jpg",
                hi_res_url="https://example.com/facts-hires.jpg",
                main_urls=["https://example.com/facts-main.jpg"],
                physical_id="facts",
            )
        ]

        result = resolver.resolve_job(
            asin="B0TEST",
            requested_fields=["ingredients"],
            images=images,
            extract_supplement_facts_details=True,
        )

        assert result.supplement_facts_detail is not None
        calories, creatine, bulk_powder, vitamin_d = result.supplement_facts_detail.ingredients
        self.assertEqual(calories.csv_value, "10")
        self.assertIsNone(calories.normalized_amount_mg)
        self.assertEqual(creatine.name, "Creatine Monohydrate")
        self.assertEqual(creatine.raw_amount_text, "12,000 mgs")
        self.assertEqual(creatine.csv_value, "12000")
        self.assertAlmostEqual(bulk_powder.normalized_amount_mg or 0.0, 453592.37)
        self.assertEqual(bulk_powder.csv_value, "453592.37")
        self.assertEqual(vitamin_d.csv_value, "")
        self.assertEqual(result.supplement_facts_detail.unparsed_fragments, ["Vitamin D: 1000 IU"])

if __name__ == "__main__":
    unittest.main()
