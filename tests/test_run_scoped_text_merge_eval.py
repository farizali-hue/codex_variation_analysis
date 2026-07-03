from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

from amazon_scraper.retrieval import EmbeddingCache
from amazon_scraper.run_scoped_text_merge_eval import (
    evaluate_run_scoped_text_merge_cases,
    load_run_scoped_text_merge_eval_cases,
)


class _FakeEvalClient:
    def create_embeddings(self, texts: list[str], cache_hits: int = 0) -> list[list[float]]:
        del cache_hits
        vectors: list[list[float]] = []
        for text in texts:
            normalized = text.strip().lower()
            if normalized in {"no flavour", "unflavored"}:
                vectors.append([1.0, 0.0])
            elif normalized in {"french vanilla", "vanilla"}:
                vectors.append([0.0, 1.0])
            elif normalized in {"original", "plain"}:
                vectors.append([0.5, 0.5])
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
        pair = {value_a.lower(), value_b.lower()}
        if pair == {"no flavour", "unflavored"}:
            return json.dumps(
                {"decision": "equivalent", "canonical_value": "unflavored", "confidence": 0.95}
            )
        if pair == {"quick release softgels", "softgels"}:
            return json.dumps(
                {"decision": "equivalent", "canonical_value": "softgels", "confidence": 0.94}
            )
        if pair == {"original", "plain"}:
            return json.dumps(
                {"decision": "uncertain", "canonical_value": "", "confidence": 0.76}
            )
        return json.dumps({"decision": "distinct", "canonical_value": "", "confidence": 0.9})


class RunScopedTextMergeEvalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_path = Path(".cache") / f"test-run-scoped-text-merge-eval-{uuid4().hex}.json"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_cache_file)

    def _cleanup_cache_file(self) -> None:
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
        except PermissionError:
            return

    def test_load_seed_eval_cases(self) -> None:
        cases = load_run_scoped_text_merge_eval_cases("evals/run_scoped_text_merge_seed.jsonl")

        self.assertGreaterEqual(len(cases), 3)
        self.assertEqual(cases[0].case_id, "flavor_equivalent_unflavored")
        self.assertIn("equivalent", {case.expected_decision for case in cases})
        self.assertIn("distinct", {case.expected_decision for case in cases})
        self.assertIn("uncertain", {case.expected_decision for case in cases})

    def test_evaluate_seed_cases_builds_summary_and_per_case_results(self) -> None:
        cases = load_run_scoped_text_merge_eval_cases("evals/run_scoped_text_merge_seed.jsonl")
        report = evaluate_run_scoped_text_merge_cases(
            cases,
            client=_FakeEvalClient(),
            embedding_model="text-embedding-3-small",
            embedding_cache=EmbeddingCache(self.cache_path),
            semantic_similarity_threshold=0.8,
            judgment_confidence_threshold=0.75,
        )

        self.assertEqual(report.summary.total_cases, len(cases))
        self.assertEqual(report.summary.passed_cases, len(cases))
        self.assertAlmostEqual(report.summary.decision_accuracy, 1.0)
        self.assertAlmostEqual(report.summary.canonical_accuracy_on_equivalent, 1.0)
        self.assertEqual(
            [result.predicted_decision for result in report.results],
            [case.expected_decision for case in cases],
        )


def _extract_labeled_value(prompt: str, label: str) -> str:
    prefix = f"{label}: "
    for line in prompt.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


if __name__ == "__main__":
    unittest.main()
