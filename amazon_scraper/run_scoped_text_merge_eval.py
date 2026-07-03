from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import FieldResult, RunFieldContract
from .retrieval import EmbeddingCache
from .run_scoped_canonicalization import MergeDecisionEvent, RunScopedCanonicalizer

_DECISIONS = {"equivalent", "distinct", "uncertain"}


@dataclass(frozen=True)
class RunScopedTextMergeEvalCase:
    case_id: str
    field_internal_key: str
    requested_label: str
    value_a: str
    value_b: str
    expected_decision: str
    expected_canonical_value: str = ""


@dataclass(frozen=True)
class RunScopedTextMergeEvalResult:
    case_id: str
    predicted_decision: str
    predicted_canonical_value: str
    expected_decision: str
    expected_canonical_value: str
    action: str
    passed: bool
    similarity: float
    confidence: float


@dataclass(frozen=True)
class RunScopedTextMergeEvalSummary:
    total_cases: int
    passed_cases: int
    decision_accuracy: float
    canonical_accuracy_on_equivalent: float | None
    equivalent_precision: float | None
    distinct_precision: float | None
    uncertain_precision: float | None


@dataclass(frozen=True)
class RunScopedTextMergeEvalReport:
    results: list[RunScopedTextMergeEvalResult]
    summary: RunScopedTextMergeEvalSummary


def load_run_scoped_text_merge_eval_cases(path: str | Path) -> list[RunScopedTextMergeEvalCase]:
    cases: list[RunScopedTextMergeEvalCase] = []
    eval_path = Path(path)
    for line in eval_path.read_text(encoding="utf-8").splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        payload = json.loads(normalized)
        expected_decision = str(payload.get("expected_decision", "") or "").strip().lower()
        if expected_decision not in _DECISIONS:
            raise ValueError(f"Invalid expected_decision {expected_decision!r} in {eval_path}")
        cases.append(
            RunScopedTextMergeEvalCase(
                case_id=str(payload.get("case_id", "") or "").strip(),
                field_internal_key=str(payload.get("field_internal_key", "") or "").strip(),
                requested_label=str(payload.get("requested_label", "") or "").strip(),
                value_a=str(payload.get("value_a", "") or "").strip(),
                value_b=str(payload.get("value_b", "") or "").strip(),
                expected_decision=expected_decision,
                expected_canonical_value=str(payload.get("expected_canonical_value", "") or "").strip().lower(),
            )
        )
    return cases


def evaluate_run_scoped_text_merge_cases(
    cases: Iterable[RunScopedTextMergeEvalCase],
    *,
    client,
    embedding_model: str,
    embedding_cache: EmbeddingCache,
    semantic_similarity_threshold: float = 0.88,
    judgment_confidence_threshold: float = 0.75,
) -> RunScopedTextMergeEvalReport:
    canonicalizer = RunScopedCanonicalizer(
        client=client,
        embedding_model=embedding_model,
        embedding_cache=embedding_cache,
        semantic_similarity_threshold=semantic_similarity_threshold,
        judgment_confidence_threshold=judgment_confidence_threshold,
    )
    results: list[RunScopedTextMergeEvalResult] = []

    for case in cases:
        canonicalizer.reset()
        field_contract = RunFieldContract(
            requested_label=case.requested_label,
            internal_key=case.field_internal_key,
            output_type="text",
            merge_similar_values=True,
            matched_by="eval",
        )
        first_asin = f"{case.case_id}:A"
        second_asin = f"{case.case_id}:B"
        canonicalizer.register_field_result(
            asin=first_asin,
            field_contract=field_contract,
            result=FieldResult(value=case.value_a, source="eval", confidence=1.0, evidence=case.value_a),
            apply_to_result=False,
        )
        canonicalizer.register_field_result(
            asin=second_asin,
            field_contract=field_contract,
            result=FieldResult(value=case.value_b, source="eval", confidence=1.0, evidence=case.value_b),
            apply_to_result=False,
        )
        events = canonicalizer.pop_merge_events(asin=second_asin)
        chosen_event = _select_eval_event(events)
        predicted_decision = chosen_event.decision if chosen_event is not None else ""
        predicted_canonical_value = (
            chosen_event.canonical_value.lower()
            if chosen_event is not None and predicted_decision == "equivalent"
            else ""
        )
        passed = (
            predicted_decision == case.expected_decision
            and (
                case.expected_decision != "equivalent"
                or predicted_canonical_value == case.expected_canonical_value
            )
        )
        results.append(
            RunScopedTextMergeEvalResult(
                case_id=case.case_id,
                predicted_decision=predicted_decision,
                predicted_canonical_value=predicted_canonical_value,
                expected_decision=case.expected_decision,
                expected_canonical_value=case.expected_canonical_value,
                action=chosen_event.action if chosen_event is not None else "",
                passed=passed,
                similarity=chosen_event.similarity if chosen_event is not None else 0.0,
                confidence=chosen_event.confidence if chosen_event is not None else 0.0,
            )
        )

    return RunScopedTextMergeEvalReport(results=results, summary=_build_summary(results))


def _select_eval_event(events: list[MergeDecisionEvent]) -> MergeDecisionEvent | None:
    if not events:
        return None
    for event in events:
        if event.action in {"semantic_merge", "exact_merge", "abstain", "no_merge"}:
            return event
    return events[-1]


def _build_summary(results: list[RunScopedTextMergeEvalResult]) -> RunScopedTextMergeEvalSummary:
    total_cases = len(results)
    passed_cases = sum(1 for result in results if result.passed)
    correct_decisions = sum(
        1 for result in results if result.predicted_decision == result.expected_decision
    )
    equivalent_cases = [result for result in results if result.expected_decision == "equivalent"]
    correct_equivalent_canonicals = sum(
        1
        for result in equivalent_cases
        if result.predicted_decision == "equivalent"
        and result.predicted_canonical_value == result.expected_canonical_value
    )
    return RunScopedTextMergeEvalSummary(
        total_cases=total_cases,
        passed_cases=passed_cases,
        decision_accuracy=(correct_decisions / total_cases) if total_cases else 0.0,
        canonical_accuracy_on_equivalent=(
            correct_equivalent_canonicals / len(equivalent_cases)
            if equivalent_cases
            else None
        ),
        equivalent_precision=_precision_for_decision(results, "equivalent"),
        distinct_precision=_precision_for_decision(results, "distinct"),
        uncertain_precision=_precision_for_decision(results, "uncertain"),
    )


def _precision_for_decision(
    results: list[RunScopedTextMergeEvalResult],
    decision: str,
) -> float | None:
    predicted = [result for result in results if result.predicted_decision == decision]
    if not predicted:
        return None
    correct = sum(1 for result in predicted if result.expected_decision == decision)
    return correct / len(predicted)
