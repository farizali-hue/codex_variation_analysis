from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

from amazon_scraper.models import FieldResult, RunFieldContract
from amazon_scraper.retrieval import EmbeddingCache
from amazon_scraper.run_scoped_canonicalization import RunScopedCanonicalizer


class _FakeCanonicalizationClient:
    def __init__(self) -> None:
        self.embedding_calls: list[tuple[str, ...]] = []
        self.chat_calls: list[tuple[str, str]] = []

    def create_embeddings(self, texts: list[str], cache_hits: int = 0) -> list[list[float]]:
        self.embedding_calls.append(tuple(texts))
        vectors: list[list[float]] = []
        for text in texts:
            normalized = text.strip().lower()
            if normalized in {"no flavour", "unflavored"}:
                vectors.append([1.0, 0.0])
            elif normalized in {"french vanilla", "vanilla"}:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.0, 0.0])
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
        pair = {value_a.lower(), value_b.lower()}
        if pair == {"no flavour", "unflavored"}:
            return json.dumps(
                {
                    "decision": "equivalent",
                    "canonical_value": "unflavored",
                    "confidence": 0.94,
                }
            )
        if pair == {"quick release softgels", "softgels"}:
            return json.dumps(
                {
                    "decision": "equivalent",
                    "canonical_value": "softgels",
                    "confidence": 0.93,
                }
            )
        return json.dumps(
            {
                "decision": "distinct",
                "canonical_value": "",
                "confidence": 0.92,
            }
        )


class RunScopedCanonicalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_path = Path(".cache") / f"test-run-scoped-canonicalization-{uuid4().hex}.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_cache_file)
        self.cache = EmbeddingCache(self.cache_path)
        self.client = _FakeCanonicalizationClient()
        self.canonicalizer = RunScopedCanonicalizer(
            client=self.client,
            embedding_model="text-embedding-3-small",
            embedding_cache=self.cache,
            semantic_similarity_threshold=0.8,
            judgment_confidence_threshold=0.75,
        )
        self.field_contract = RunFieldContract(
            requested_label="Flavor",
            internal_key="flavor",
            output_type="text",
            merge_similar_values=True,
            matched_by="generated",
        )

    def _cleanup_cache_file(self) -> None:
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
        except PermissionError:
            return

    def test_semantic_merge_updates_earlier_rows_on_finalize(self) -> None:
        first = self.canonicalizer.register_field_result(
            asin="ASIN1",
            field_contract=self.field_contract,
            result=FieldResult(value="No Flavour", source="image", confidence=0.9, evidence="No Flavour"),
        )
        second = self.canonicalizer.register_field_result(
            asin="ASIN2",
            field_contract=self.field_contract,
            result=FieldResult(value="Unflavored", source="html", confidence=0.9, evidence="Unflavored"),
        )

        finalized_first = self.canonicalizer.finalize_field_result(
            field_contract=self.field_contract,
            result=first,
        )

        self.assertEqual(first.value, "no flavour")
        self.assertEqual(second.value, "unflavored")
        self.assertEqual(finalized_first.value, "unflavored")
        self.assertEqual(self.client.chat_calls, [("Unflavored", "no flavour")])

    def test_distinct_values_form_separate_clusters(self) -> None:
        first = self.canonicalizer.register_field_result(
            asin="ASIN1",
            field_contract=self.field_contract,
            result=FieldResult(value="French Vanilla", source="image", confidence=0.9, evidence="French Vanilla"),
        )
        second = self.canonicalizer.register_field_result(
            asin="ASIN2",
            field_contract=self.field_contract,
            result=FieldResult(value="Vanilla", source="html", confidence=0.9, evidence="Vanilla"),
        )

        self.assertEqual(first.value, "french vanilla")
        self.assertEqual(second.value, "vanilla")
        self.assertEqual(self.client.chat_calls, [("Vanilla", "french vanilla")])

    def test_lexical_candidate_merges_modifier_suffix_pair(self) -> None:
        first = self.canonicalizer.register_field_result(
            asin="ASIN1",
            field_contract=self.field_contract,
            result=FieldResult(value="Softgels", source="image", confidence=0.9, evidence="Softgels"),
        )
        second = self.canonicalizer.register_field_result(
            asin="ASIN2",
            field_contract=self.field_contract,
            result=FieldResult(
                value="Quick Release Softgels",
                source="html",
                confidence=0.9,
                evidence="Quick Release Softgels",
            ),
        )

        events = self.canonicalizer.pop_merge_events(asin="ASIN2")

        self.assertEqual(first.value, "softgels")
        self.assertEqual(second.value, "softgels")
        self.assertEqual(self.client.chat_calls, [("Quick Release Softgels", "softgels")])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "semantic_merge")
        self.assertEqual(events[0].reason, "lexical_candidate")

    def test_lexical_candidate_merge_updates_earlier_rows_when_canonical_changes(self) -> None:
        first = self.canonicalizer.register_field_result(
            asin="ASIN1",
            field_contract=self.field_contract,
            result=FieldResult(
                value="Quick Release Softgels",
                source="image",
                confidence=0.9,
                evidence="Quick Release Softgels",
            ),
        )
        second = self.canonicalizer.register_field_result(
            asin="ASIN2",
            field_contract=self.field_contract,
            result=FieldResult(value="Softgels", source="html", confidence=0.9, evidence="Softgels"),
        )

        finalized_first = self.canonicalizer.finalize_field_result(
            field_contract=self.field_contract,
            result=first,
        )

        self.assertEqual(first.value, "quick release softgels")
        self.assertEqual(second.value, "softgels")
        self.assertEqual(finalized_first.value, "softgels")
        self.assertEqual(self.client.chat_calls, [("Softgels", "quick release softgels")])

    def test_shadow_mode_records_merge_without_rewriting_results(self) -> None:
        first = self.canonicalizer.register_field_result(
            asin="ASIN1",
            field_contract=self.field_contract,
            result=FieldResult(value="No Flavour", source="image", confidence=0.9, evidence="No Flavour"),
            apply_to_result=False,
        )
        second = self.canonicalizer.register_field_result(
            asin="ASIN2",
            field_contract=self.field_contract,
            result=FieldResult(value="Unflavored", source="html", confidence=0.9, evidence="Unflavored"),
            apply_to_result=False,
        )

        finalized_first = self.canonicalizer.finalize_field_result(
            field_contract=self.field_contract,
            result=first,
            apply_to_result=False,
        )

        self.assertEqual(first.value, "No Flavour")
        self.assertEqual(second.value, "Unflavored")
        self.assertEqual(finalized_first.value, "No Flavour")
        self.assertEqual(self.client.chat_calls, [("Unflavored", "no flavour")])

    def test_pop_merge_events_returns_structured_events_for_asin(self) -> None:
        self.canonicalizer.register_field_result(
            asin="ASIN1",
            field_contract=self.field_contract,
            result=FieldResult(value="No Flavour", source="image", confidence=0.9, evidence="No Flavour"),
            apply_to_result=False,
        )
        self.canonicalizer.register_field_result(
            asin="ASIN2",
            field_contract=self.field_contract,
            result=FieldResult(value="Unflavored", source="html", confidence=0.9, evidence="Unflavored"),
            apply_to_result=False,
        )

        events = self.canonicalizer.pop_merge_events(asin="ASIN2")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].asin, "ASIN2")
        self.assertEqual(events[0].internal_key, "flavor")
        self.assertEqual(events[0].mode, "shadow")
        self.assertEqual(events[0].action, "semantic_merge")
        self.assertEqual(events[0].decision, "equivalent")
        self.assertEqual(events[0].canonical_value, "unflavored")
        self.assertEqual(self.canonicalizer.pop_merge_events(asin="ASIN2"), [])

    def test_blank_value_does_not_register_abstention_evidence_as_merge_candidate(self) -> None:
        evidence = "No evidence chunk explicitly states a flavor value or flavor label/value pair."
        result = self.canonicalizer.register_field_result(
            asin="ASIN1",
            field_contract=self.field_contract,
            result=FieldResult(value="", source="html", confidence=0.2, evidence=evidence),
        )

        finalized = self.canonicalizer.finalize_field_result(
            field_contract=self.field_contract,
            result=result,
        )

        self.assertEqual(result.value, "")
        self.assertEqual(finalized.value, "")
        self.assertEqual(self.client.embedding_calls, [])
        self.assertEqual(self.client.chat_calls, [])
        self.assertEqual(self.canonicalizer.pop_merge_events(asin="ASIN1"), [])


class _MalformedJudgmentClient(_FakeCanonicalizationClient):
    """Embeddings behave normally, but every judge call returns non-JSON text."""

    def create_chat_completion(self, system_prompt, user_prompt, **kwargs) -> str:  # type: ignore[override]
        value_a = _extract_labeled_value(user_prompt, "Value A")
        value_b = _extract_labeled_value(user_prompt, "Value B")
        self.chat_calls.append((value_a, value_b))
        return "I'm sorry, I can't produce JSON right now."


class MalformedJudgmentResponseTests(unittest.TestCase):
    """A malformed LLM judge response must abstain, not crash the whole job."""

    def setUp(self) -> None:
        self.cache_path = Path(".cache") / f"test-run-scoped-canonicalization-{uuid4().hex}.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_cache_file)
        self.client = _MalformedJudgmentClient()
        self.canonicalizer = RunScopedCanonicalizer(
            client=self.client,
            embedding_model="text-embedding-3-small",
            embedding_cache=EmbeddingCache(self.cache_path),
            semantic_similarity_threshold=0.8,
            judgment_confidence_threshold=0.75,
        )
        self.field_contract = RunFieldContract(
            requested_label="Flavor",
            internal_key="flavor",
            output_type="text",
            merge_similar_values=True,
            matched_by="generated",
        )

    def _cleanup_cache_file(self) -> None:
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
        except PermissionError:
            return

    def test_malformed_judge_response_abstains_instead_of_raising(self) -> None:
        self.canonicalizer.register_field_result(
            asin="ASIN1",
            field_contract=self.field_contract,
            result=FieldResult(value="Vanilla", source="image", confidence=0.9, evidence="Vanilla"),
        )
        # "Vanilla Bean" is a lexical candidate against "Vanilla", so the judge
        # is consulted; its garbage reply must abstain (uncertain), keep the
        # value unmerged, and not raise out of register_field_result.
        second = self.canonicalizer.register_field_result(
            asin="ASIN2",
            field_contract=self.field_contract,
            result=FieldResult(value="Vanilla Bean", source="html", confidence=0.9, evidence="Vanilla Bean"),
        )

        self.assertEqual(second.value, "vanilla bean")
        self.assertGreaterEqual(len(self.client.chat_calls), 1)
        events = self.canonicalizer.pop_merge_events(asin="ASIN2")
        actions = [event.action for event in events]
        self.assertIn("abstain", actions)
        self.assertEqual(actions[-1], "new_cluster")


def _extract_labeled_value(prompt: str, label: str) -> str:
    prefix = f"{label}: "
    for line in prompt.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


if __name__ == "__main__":
    unittest.main()
