from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

from amazon_scraper.custom_field_presets import CustomFieldPreset
from amazon_scraper.models import FieldResult, RunFieldContract, ScrapeJob, ScrapeResult
from amazon_scraper.openai_client import OpenAIConfig
from amazon_scraper.retrieval import EmbeddingCache
from amazon_scraper.run_scoped_canonicalization import RunScopedCanonicalizer
from amazon_scraper.scraper import AmazonScraper, DriverSessionLostError, ScrapeCancelledError


class _FakeDriver:
    session_id = "fake-session"

    def quit(self) -> None:
        return None


class _FakeVisionResolver:
    def __init__(self, results: dict[str, FieldResult], detail=None) -> None:
        self.results = results
        self.detail = detail
        self.calls: list[dict[str, object]] = []

    def resolve_fields(self, *, asin: str, requested_fields, images, field_contracts=None):
        self.calls.append(
            {
                "asin": asin,
                "requested_fields": list(requested_fields),
                "image_count": len(images),
                "field_contract_keys": sorted((field_contracts or {}).keys()),
            }
        )
        return dict(self.results)

    def resolve_job(
        self,
        *,
        asin: str,
        requested_fields,
        images,
        field_contracts=None,
        extract_supplement_facts_details: bool = False,
    ):
        self.calls.append(
            {
                "asin": asin,
                "requested_fields": list(requested_fields),
                "image_count": len(images),
                "field_contract_keys": sorted((field_contracts or {}).keys()),
                "extract_supplement_facts_details": extract_supplement_facts_details,
            }
        )
        return SimpleNamespace(
            fields=dict(self.results),
            supplement_facts_detail=self.detail,
        )


class _FailingVisionResolver:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def resolve_fields(self, *, asin: str, requested_fields, images, field_contracts=None):
        raise self.error

    def resolve_job(
        self,
        *,
        asin: str,
        requested_fields,
        images,
        field_contracts=None,
        extract_supplement_facts_details: bool = False,
    ):
        del asin, requested_fields, images, field_contracts, extract_supplement_facts_details
        raise self.error


class _FakeMergeClient:
    def __init__(self) -> None:
        self.chat_calls: list[tuple[str, str]] = []

    def create_embeddings(self, texts: list[str], cache_hits: int = 0) -> list[list[float]]:
        del cache_hits
        vectors: list[list[float]] = []
        for text in texts:
            normalized = text.strip().lower()
            if normalized in {"no flavour", "unflavored"}:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors

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
        del system_prompt, asin, field, retry_attempt, model, image_inputs
        value_a = _extract_labeled_value(user_prompt, "Value A")
        value_b = _extract_labeled_value(user_prompt, "Value B")
        self.chat_calls.append((value_a, value_b))
        if "Value A: Unflavored" in user_prompt and "Value B: no flavour" in user_prompt:
            return json.dumps({"decision": "equivalent", "canonical_value": "unflavored", "confidence": 0.95})
        return json.dumps({"decision": "distinct", "canonical_value": "", "confidence": 0.9})


class _PassthroughCanonicalizer:
    def reset(self) -> None:
        return None

    def register_field_result(
        self,
        *,
        asin: str,
        field_contract: RunFieldContract,
        result: FieldResult,
        apply_to_result: bool = True,
    ) -> FieldResult:
        del asin, field_contract, apply_to_result
        return result

    def finalize_field_result(
        self,
        *,
        field_contract: RunFieldContract,
        result: FieldResult,
        apply_to_result: bool = True,
    ) -> FieldResult:
        del field_contract, apply_to_result
        return result

    def pop_merge_events(self, *, asin: str | None = None) -> list[object]:
        del asin
        return []


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        return


def _extract_labeled_value(prompt: str, label: str) -> str:
    prefix = f"{label}: "
    for line in prompt.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


class ScraperImageFirstTests(unittest.TestCase):
    def _build_scraper(self) -> AmazonScraper:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
        )
        scraper._prepare_session = lambda driver: None
        return scraper

    def _build_html(self) -> str:
        return r"""
        <html>
            <body>
                <span id="productTitle">Test Product</span>
                <div id="item_details">
                    <table>
                        <tr><th>Serving Size</th><td>25 Servings</td></tr>
                        <tr><th>Ingredients</th><td>Pumpkin Seed Oil, Sunflower Lecithin</td></tr>
                        <tr><th>Container Type</th><td>Pouch</td></tr>
                    </table>
                </div>
                <script type="text/javascript">
                P.when('jQuery').register('ImageBlockBTF', function(jQuery){
                    var obj = jQuery.parseJSON('{\"landingAsinColor\":\"initial\",\"colorImages\":{\"initial\":[{\"large\":\"https://example.com/front-large.jpg\",\"thumb\":\"https://example.com/front-thumb.jpg\",\"hiRes\":\"https://example.com/front-hires.jpg\",\"variant\":\"MAIN\",\"main\":{\"https://example.com/front-main.jpg\":[450,450]},\"physicalIdForMedia\":\"front\"}]}}');
                });
                </script>
            </body>
        </html>
        """

    def test_image_results_override_html_and_html_fills_missing_fields(self) -> None:
        scraper = self._build_scraper()
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver(
            {
                "serving_size": FieldResult(
                    value="30 servings",
                    source="https://example.com/front-hires.jpg",
                    confidence=0.93,
                    evidence="Serving Size 30 Servings",
                )
            }
        )

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                raise AssertionError("Retriever should not be called for direct HTML fallback fields in this test")

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                raise AssertionError("Extractor should not be called for direct HTML fallback fields in this test")

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0TESTING1",
                product_type="supplement",
                requested_fields=["serving_size", "ingredients"],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(result.fields["serving_size"].value, "30")
        self.assertEqual(result.fields["ingredients"].value, "Pumpkin Seed Oil | Sunflower Lecithin")
        self.assertEqual(scraper.vision_resolver.calls[0]["image_count"], 1)

    def test_image_results_skip_html_retrieval_when_generic_field_is_resolved(self) -> None:
        scraper = self._build_scraper()
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver(
            {
                "flavor_note": FieldResult(
                    value="Vanilla Bean",
                    source="https://example.com/front-hires.jpg",
                    confidence=0.88,
                    evidence="Vanilla bean front label",
                )
            }
        )

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                raise AssertionError("Retriever should not be called when image-first already resolved the field")

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                raise AssertionError("Extractor should not be called when image-first already resolved the field")

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0TESTING2",
                product_type="supplement",
                requested_fields=["flavor_note"],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(result.fields["flavor_note"].value, "Vanilla Bean")

    def test_detail_flag_is_forwarded_and_structured_panel_is_attached_to_result(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            extract_supplement_facts_details=True,
        )
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        detail = SimpleNamespace(
            metadata=[],
            ingredients=[],
            other_ingredients="Vegetable cellulose",
            unparsed_fragments=["Vitamin D3 1000 IU"],
        )
        scraper.vision_resolver = _FakeVisionResolver(
            {
                "serving_size": FieldResult(
                    value="2 Capsules",
                    source="https://example.com/front-hires.jpg",
                    confidence=0.98,
                    evidence="Serving Size 2 Capsules",
                )
            },
            detail=detail,
        )

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                raise AssertionError("Retriever should not be called when the detail parse already resolved the field")

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                raise AssertionError("Extractor should not be called when the detail parse already resolved the field")

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0TESTINGD",
                product_type="supplement",
                requested_fields=["serving_size"],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(result.fields["serving_size"].value, "2")
        self.assertIs(result.supplement_facts_detail, detail)
        self.assertTrue(bool(scraper.vision_resolver.calls[0]["extract_supplement_facts_details"]))

    def test_custom_field_rules_are_applied_to_image_first_results(self) -> None:
        scraper = self._build_scraper()
        scraper.custom_field_presets = {
            "flavor_note": CustomFieldPreset(output_type="text", merge_similar_values=True)
        }
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver(
            {
                "flavor_note": FieldResult(
                    value="No Flavour",
                    source="https://example.com/front-hires.jpg",
                    confidence=0.92,
                    evidence="No Flavour",
                )
            }
        )
        scraper.run_scoped_canonicalizer = _PassthroughCanonicalizer()

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                raise AssertionError("Retriever should not be called when image-first already resolved the field")

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                raise AssertionError("Extractor should not be called when image-first already resolved the field")

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0TESTING4",
                product_type="supplement",
                requested_fields=["flavor_note"],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(result.fields["flavor_note"].value, "unflavored")

    def test_vision_failure_falls_back_to_html_extraction(self) -> None:
        scraper = self._build_scraper()
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FailingVisionResolver(RuntimeError("invalid_image_url"))

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                return chunks

        scraper.retriever = _FakeRetriever()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0TESTING3",
                product_type="supplement",
                requested_fields=["serving_size", "ingredients"],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.fields["serving_size"].value, "25")
        self.assertEqual(result.fields["ingredients"].value, "Pumpkin Seed Oil | Sunflower Lecithin")

    def test_run_scoped_contracts_reach_vision_and_html_extraction_when_enabled(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            run_scoped_schema_enabled=True,
        )
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver({})

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                return chunks

        extractor_calls: list[dict[str, object]] = []

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                extractor_calls.append(kwargs)
                return FieldResult(
                    value="Vanilla",
                    source="html",
                    confidence=0.88,
                    evidence="Flavor: Vanilla",
                )

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0CONTRACT1",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=False,
                        matched_by="generated",
                    )
                ],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(scraper.vision_resolver.calls[0]["requested_fields"], ["flavor"])
        self.assertEqual(scraper.vision_resolver.calls[0]["field_contract_keys"], ["flavor"])
        self.assertEqual(extractor_calls[0]["field_contract"].internal_key, "flavor")
        self.assertEqual(result.fields["flavor"].value, "Vanilla")

    def test_run_scoped_contracts_bypass_legacy_preset_normalization_when_enabled(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            run_scoped_schema_enabled=True,
        )
        scraper.custom_field_presets = {
            "flavor_note": CustomFieldPreset(output_type="text", merge_similar_values=True)
        }
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver(
            {
                "flavor_note": FieldResult(
                    value="No Flavour",
                    source="https://example.com/front-hires.jpg",
                    confidence=0.92,
                    evidence="No Flavour",
                )
            }
        )
        scraper.run_scoped_canonicalizer = _PassthroughCanonicalizer()

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                raise AssertionError("Retriever should not be called when image-first already resolved the field")

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                raise AssertionError("Extractor should not be called when image-first already resolved the field")

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0CONTRACT2",
                product_type="supplement",
                requested_fields=["flavor_note"],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor note",
                        internal_key="flavor_note",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                ],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(result.fields["flavor_note"].value, "No Flavour")

    def test_run_scoped_text_contract_keeps_blank_value_when_extractor_abstains(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            run_scoped_schema_enabled=True,
            run_scoped_text_merge_mode="apply",
        )
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver({})

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                return chunks

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                del kwargs
                return FieldResult(
                    value="",
                    source="html",
                    confidence=0.2,
                    evidence="No evidence chunk explicitly states a flavor value or flavor label/value pair.",
                )

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        cache_path = Path(".cache") / f"test-run-blank-contract-{uuid4().hex}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: _safe_unlink(cache_path))
        merge_client = _FakeMergeClient()
        scraper.run_scoped_canonicalizer = RunScopedCanonicalizer(
            client=merge_client,
            embedding_model="text-embedding-3-small",
            embedding_cache=EmbeddingCache(cache_path),
            semantic_similarity_threshold=0.8,
            judgment_confidence_threshold=0.75,
        )

        result = scraper.run_job(
            ScrapeJob(
                asin="B0BLANK001",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                ],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(result.fields["flavor"].value, "")
        self.assertEqual(
            result.fields["flavor"].evidence,
            "No evidence chunk explicitly states a flavor value or flavor label/value pair.",
        )
        self.assertEqual(merge_client.chat_calls, [])

    def test_run_scoped_number_contract_normalizes_extracted_value(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            run_scoped_schema_enabled=True,
        )
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver({})

        class _FakeRetriever:
            def __init__(self) -> None:
                self.query_texts: list[str] = []

            def rank_chunks(self, query_text, chunks):
                self.query_texts.append(query_text)
                return chunks

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                return FieldResult(
                    value="120 quick release softgels",
                    source="html",
                    confidence=0.9,
                    evidence="Capsule count 120 quick release softgels",
                )

        fake_retriever = _FakeRetriever()
        scraper.retriever = fake_retriever
        scraper.extractor = _FakeExtractor()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0NUMBER01",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Capsule counts",
                        internal_key="capsule_counts",
                        output_type="number",
                        matched_by="generated",
                    )
                ],
            ),
            driver=_FakeDriver(),
        )

        self.assertIn("Capsule counts", fake_retriever.query_texts[0])
        self.assertIn("only one numeric value", fake_retriever.query_texts[0])
        self.assertEqual(result.fields["capsule_counts"].value, "120")

    def test_run_scoped_short_list_contract_normalizes_image_first_value(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            run_scoped_schema_enabled=True,
        )
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver(
            {
                "free_from": FieldResult(
                    value="Gluten, Soy; Dairy | Gluten",
                    source="https://example.com/front-hires.jpg",
                    confidence=0.92,
                    evidence="Gluten, Soy; Dairy | Gluten",
                )
            }
        )

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                raise AssertionError("Retriever should not be called when image-first already resolved the field")

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                raise AssertionError("Extractor should not be called when image-first already resolved the field")

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        result = scraper.run_job(
            ScrapeJob(
                asin="B0LIST001",
                product_type="supplement",
                requested_fields=["free_from"],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Free from",
                        internal_key="free_from",
                        output_type="short_list",
                        matched_by="generated",
                    )
                ],
            ),
            driver=_FakeDriver(),
        )

        self.assertEqual(result.fields["free_from"].value, "Gluten | Soy | Dairy")

    def test_run_batch_finalizes_text_merges_across_jobs(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            run_scoped_schema_enabled=True,
            run_scoped_text_merge_mode="apply",
        )
        scraper._build_driver = lambda: _FakeDriver()
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver({})

        cache_path = Path(".cache") / f"test-run-batch-merge-{uuid4().hex}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: _safe_unlink(cache_path))
        scraper.run_scoped_canonicalizer = RunScopedCanonicalizer(
            client=_FakeMergeClient(),
            embedding_model="text-embedding-3-small",
            embedding_cache=EmbeddingCache(cache_path),
            semantic_similarity_threshold=0.8,
            judgment_confidence_threshold=0.75,
        )

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                return chunks

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                asin = kwargs["asin"]
                if asin == "B0MERGE01":
                    return FieldResult(
                        value="No Flavour",
                        source="html",
                        confidence=0.92,
                        evidence="Flavor: No Flavour",
                    )
                return FieldResult(
                    value="Unflavored",
                    source="html",
                    confidence=0.91,
                    evidence="Flavor: Unflavored",
                )

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        jobs = [
            ScrapeJob(
                asin="B0MERGE01",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                ],
            ),
            ScrapeJob(
                asin="B0MERGE02",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                ],
            ),
        ]

        results = scraper.run_batch(jobs, delay_seconds=0.0)

        self.assertEqual(results[0].fields["flavor"].value, "unflavored")
        self.assertEqual(results[1].fields["flavor"].value, "unflavored")

    def test_run_batch_shadow_merge_evaluates_without_rewriting_results(self) -> None:
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            run_scoped_schema_enabled=True,
            run_scoped_text_merge_mode="shadow",
        )
        scraper._build_driver = lambda: _FakeDriver()
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver({})

        cache_path = Path(".cache") / f"test-run-batch-shadow-merge-{uuid4().hex}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: _safe_unlink(cache_path))
        merge_client = _FakeMergeClient()
        scraper.run_scoped_canonicalizer = RunScopedCanonicalizer(
            client=merge_client,
            embedding_model="text-embedding-3-small",
            embedding_cache=EmbeddingCache(cache_path),
            semantic_similarity_threshold=0.8,
            judgment_confidence_threshold=0.75,
        )

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                return chunks

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                asin = kwargs["asin"]
                if asin == "B0MERGE01":
                    return FieldResult(
                        value="No Flavour",
                        source="html",
                        confidence=0.92,
                        evidence="Flavor: No Flavour",
                    )
                return FieldResult(
                    value="Unflavored",
                    source="html",
                    confidence=0.91,
                    evidence="Flavor: Unflavored",
                )

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        jobs = [
            ScrapeJob(
                asin="B0MERGE01",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                ],
            ),
            ScrapeJob(
                asin="B0MERGE02",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                ],
            ),
        ]

        results = scraper.run_batch(jobs, delay_seconds=0.0)

        self.assertEqual(results[0].fields["flavor"].value, "No Flavour")
        self.assertEqual(results[1].fields["flavor"].value, "Unflavored")
        self.assertEqual(merge_client.chat_calls, [("Unflavored", "no flavour")])

    def test_shadow_mode_writes_structured_merge_events_to_debug_metadata(self) -> None:
        temp_dir = Path.cwd() / ".test_artifacts" / f"shadow-merge-debug-{uuid4().hex}"
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(temp_dir, ignore_errors=True))

        scraper = AmazonScraper(
            debug_html_dir=temp_dir,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            run_scoped_schema_enabled=True,
            run_scoped_text_merge_mode="shadow",
        )
        scraper._build_driver = lambda: _FakeDriver()
        scraper._prepare_session = lambda driver: None
        scraper._fetch_page_source = lambda driver, url: self._build_html()
        scraper.vision_resolver = _FakeVisionResolver({})

        cache_path = Path(".cache") / f"test-shadow-merge-debug-{uuid4().hex}.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: _safe_unlink(cache_path))
        scraper.run_scoped_canonicalizer = RunScopedCanonicalizer(
            client=_FakeMergeClient(),
            embedding_model="text-embedding-3-small",
            embedding_cache=EmbeddingCache(cache_path),
            semantic_similarity_threshold=0.8,
            judgment_confidence_threshold=0.75,
        )

        class _FakeRetriever:
            def rank_chunks(self, query_text, chunks):
                return chunks

        class _FakeExtractor:
            def extract_field(self, **kwargs):
                asin = kwargs["asin"]
                if asin == "B0MERGE01":
                    return FieldResult(
                        value="No Flavour",
                        source="html",
                        confidence=0.92,
                        evidence="Flavor: No Flavour",
                    )
                return FieldResult(
                    value="Unflavored",
                    source="html",
                    confidence=0.91,
                    evidence="Flavor: Unflavored",
                )

        scraper.retriever = _FakeRetriever()
        scraper.extractor = _FakeExtractor()

        jobs = [
            ScrapeJob(
                asin="B0MERGE01",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                ],
            ),
            ScrapeJob(
                asin="B0MERGE02",
                product_type="supplement",
                requested_fields=[],
                field_contracts=[
                    RunFieldContract(
                        requested_label="Flavor",
                        internal_key="flavor",
                        output_type="text",
                        merge_similar_values=True,
                        matched_by="generated",
                    )
                ],
            ),
        ]

        scraper.run_batch(jobs, delay_seconds=0.0)

        metadata = json.loads((temp_dir / "B0MERGE02.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["run_scoped_text_merge_mode"], "shadow")
        self.assertEqual(len(metadata["run_scoped_merge_events"]), 1)
        self.assertEqual(metadata["run_scoped_merge_events"][0]["mode"], "shadow")
        self.assertEqual(metadata["run_scoped_merge_events"][0]["decision"], "equivalent")
        self.assertEqual(metadata["run_scoped_merge_events"][0]["canonical_value"], "unflavored")

    def test_run_batch_aborts_remaining_jobs_when_driver_session_is_lost(self) -> None:
        scraper = self._build_scraper()
        scraper._build_driver = lambda: _FakeDriver()
        scraper._prepare_session = lambda driver: None

        calls: list[str] = []

        def _fake_run_job(job, driver=None):
            calls.append(job.asin)
            if job.asin == "B0SECOND":
                raise DriverSessionLostError("driver lost")
            return ScrapeResult(
                asin=job.asin,
                product_type=job.product_type,
                status="OK",
                url=f"https://example.com/{job.asin}",
                scraped_at=datetime.now(tz=timezone.utc),
                title="Test",
                fields={},
                error=None,
            )

        scraper.run_job = _fake_run_job

        with self.assertRaises(DriverSessionLostError) as context:
            scraper.run_batch(
                [
                    ScrapeJob(asin="B0FIRST", requested_fields=["packaging_type"]),
                    ScrapeJob(asin="B0SECOND", requested_fields=["packaging_type"]),
                    ScrapeJob(asin="B0THIRD", requested_fields=["packaging_type"]),
                ]
            )

        self.assertEqual(calls, ["B0FIRST", "B0SECOND"])
        self.assertEqual([result.asin for result in context.exception.partial_results], ["B0FIRST"])

    def test_run_batch_honors_cancel_event_between_jobs(self) -> None:
        cancel_event = threading.Event()
        scraper = AmazonScraper(
            debug_html_dir=None,
            openai_config=OpenAIConfig(
                api_key="dummy",
                embedding_model="text-embedding-3-small",
                chat_model="gpt-5.4-mini",
            ),
            cancel_event=cancel_event,
        )
        scraper._build_driver = lambda: _FakeDriver()
        scraper._prepare_session = lambda driver: None

        calls: list[str] = []

        def _fake_run_job(job, driver=None):
            calls.append(job.asin)
            cancel_event.set()
            return ScrapeResult(
                asin=job.asin,
                product_type=job.product_type,
                status="OK",
                url=f"https://example.com/{job.asin}",
                scraped_at=datetime.now(tz=timezone.utc),
                title="Test",
                fields={},
                error=None,
            )

        scraper.run_job = _fake_run_job

        with self.assertRaises(ScrapeCancelledError) as context:
            scraper.run_batch(
                [
                    ScrapeJob(asin="B0FIRST", requested_fields=["packaging_type"]),
                    ScrapeJob(asin="B0SECOND", requested_fields=["packaging_type"]),
                ],
                delay_seconds=0.0,
            )

        self.assertEqual(calls, ["B0FIRST"])
        self.assertEqual([result.asin for result in context.exception.partial_results], ["B0FIRST"])


if __name__ == "__main__":
    unittest.main()
