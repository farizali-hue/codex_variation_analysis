from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from .models import FieldResult, RunFieldContract
from .retrieval import EmbeddingCache, cosine_similarity
from .value_parsers import normalize_whitespace

LOGGER = logging.getLogger(__name__)

_CANONICALIZATION_SYSTEM_PROMPT = (
    "You determine whether two extracted product field values should be standardized to the same canonical value. "
    "Be conservative. Only return equivalent when the values clearly represent the same user-facing value for the same field. "
    "Return JSON with keys decision, canonical_value, confidence. "
    "decision must be one of equivalent, distinct, uncertain. "
    "If decision is not equivalent, canonical_value must be an empty string."
)
_NORMALIZATION_CLEANUP_PATTERN = re.compile(r"[^a-z0-9+/ ]+")


@dataclass
class CanonicalCluster:
    canonical_value: str
    variant_keys: set[str] = field(default_factory=set)
    embedding_text: str = ""
    embedding: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class MergeDecisionEvent:
    asin: str
    internal_key: str
    mode: str
    action: str
    decision: str
    raw_value: str
    canonical_value: str
    candidate_value: str = ""
    similarity: float = 0.0
    confidence: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "asin": self.asin,
            "internal_key": self.internal_key,
            "mode": self.mode,
            "action": self.action,
            "decision": self.decision,
            "raw_value": self.raw_value,
            "canonical_value": self.canonical_value,
            "candidate_value": self.candidate_value,
            "similarity": self.similarity,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class RunScopedCanonicalizer:
    def __init__(
        self,
        *,
        client,
        embedding_model: str,
        embedding_cache: EmbeddingCache,
        semantic_similarity_threshold: float = 0.88,
        judgment_confidence_threshold: float = 0.75,
        max_candidates: int = 3,
    ) -> None:
        self.client = client
        self.embedding_model = embedding_model
        self.embedding_cache = embedding_cache
        self.semantic_similarity_threshold = semantic_similarity_threshold
        self.judgment_confidence_threshold = judgment_confidence_threshold
        self.max_candidates = max(1, max_candidates)
        self._clusters_by_field: dict[str, list[CanonicalCluster]] = {}
        self._judgments_by_field: dict[str, dict[tuple[str, str], _CanonicalizationJudgment]] = {}
        self._events_by_asin: dict[str, list[MergeDecisionEvent]] = {}

    def reset(self) -> None:
        self._clusters_by_field = {}
        self._judgments_by_field = {}
        self._events_by_asin = {}

    def pop_merge_events(self, *, asin: str | None = None) -> list[MergeDecisionEvent]:
        if asin is not None:
            return list(self._events_by_asin.pop(asin, []))

        events: list[MergeDecisionEvent] = []
        for asin_key in sorted(self._events_by_asin):
            events.extend(self._events_by_asin[asin_key])
        self._events_by_asin = {}
        return events

    def peek_merge_events(self, *, asin: str | None = None) -> list[MergeDecisionEvent]:
        if asin is not None:
            return list(self._events_by_asin.get(asin, []))

        events: list[MergeDecisionEvent] = []
        for asin_key in sorted(self._events_by_asin):
            events.extend(self._events_by_asin[asin_key])
        return events

    def register_field_result(
        self,
        *,
        asin: str,
        field_contract: RunFieldContract,
        result: FieldResult,
        apply_to_result: bool = True,
    ) -> FieldResult:
        if field_contract.output_type != "text" or not field_contract.merge_similar_values:
            return result

        raw_value = normalize_whitespace(result.value)
        normalized_key = _orthographic_normalize(raw_value)
        if not normalized_key:
            return result

        clusters = self._clusters_by_field.setdefault(field_contract.internal_key, [])
        exact_cluster = self._find_exact_cluster(clusters, normalized_key)
        if exact_cluster is not None:
            self._record_event(
                asin=asin,
                internal_key=field_contract.internal_key,
                mode=_merge_mode_label(apply_to_result),
                action="exact_merge",
                raw_value=raw_value,
                canonical_value=exact_cluster.canonical_value,
                candidate_value=exact_cluster.canonical_value,
            )
            LOGGER.info(
                "field_merge_event asin=%s internal_key=%s mode=%s action=exact_merge raw_value=%s canonical_value=%s",
                asin,
                field_contract.internal_key,
                _merge_mode_label(apply_to_result),
                raw_value,
                exact_cluster.canonical_value,
            )
            return _result_for_mode(result, exact_cluster.canonical_value, apply_to_result=apply_to_result)

        if not clusters:
            cluster = self._create_cluster(raw_value, normalized_key)
            clusters.append(cluster)
            self._record_event(
                asin=asin,
                internal_key=field_contract.internal_key,
                mode=_merge_mode_label(apply_to_result),
                action="new_cluster",
                raw_value=raw_value,
                canonical_value=cluster.canonical_value,
                reason="first_value",
            )
            LOGGER.info(
                "field_merge_event asin=%s internal_key=%s mode=%s action=new_cluster raw_value=%s canonical_value=%s reason=first_value",
                asin,
                field_contract.internal_key,
                _merge_mode_label(apply_to_result),
                raw_value,
                cluster.canonical_value,
            )
            return _result_for_mode(result, cluster.canonical_value, apply_to_result=apply_to_result)

        tried_cluster_ids: set[int] = set()
        for cluster in self._rank_lexical_candidate_clusters(raw_value, clusters):
            tried_cluster_ids.add(id(cluster))
            merged = self._evaluate_candidate_cluster(
                asin=asin,
                field_contract=field_contract,
                raw_value=raw_value,
                normalized_key=normalized_key,
                cluster=cluster,
                apply_to_result=apply_to_result,
                similarity=0.0,
                reason="lexical_candidate",
                result=result,
            )
            if merged is not None:
                return merged

        current_embedding = self._get_embedding(normalized_key)
        candidate_clusters = self._rank_candidate_clusters(current_embedding, clusters)
        for cluster, similarity in candidate_clusters:
            if id(cluster) in tried_cluster_ids:
                continue
            if similarity < self.semantic_similarity_threshold:
                continue
            merged = self._evaluate_candidate_cluster(
                asin=asin,
                field_contract=field_contract,
                raw_value=raw_value,
                normalized_key=normalized_key,
                cluster=cluster,
                apply_to_result=apply_to_result,
                similarity=similarity,
                reason="semantic_similarity",
                result=result,
            )
            if merged is not None:
                return merged

        cluster = self._create_cluster(raw_value, normalized_key)
        clusters.append(cluster)
        self._record_event(
            asin=asin,
            internal_key=field_contract.internal_key,
            mode=_merge_mode_label(apply_to_result),
            action="new_cluster",
            raw_value=raw_value,
            canonical_value=cluster.canonical_value,
            reason="no_match",
        )
        LOGGER.info(
            "field_merge_event asin=%s internal_key=%s mode=%s action=new_cluster raw_value=%s canonical_value=%s reason=no_match",
            asin,
            field_contract.internal_key,
            _merge_mode_label(apply_to_result),
            raw_value,
            cluster.canonical_value,
        )
        return _result_for_mode(result, cluster.canonical_value, apply_to_result=apply_to_result)

    def finalize_field_result(
        self,
        *,
        field_contract: RunFieldContract,
        result: FieldResult,
        apply_to_result: bool = True,
    ) -> FieldResult:
        if field_contract.output_type != "text" or not field_contract.merge_similar_values:
            return result

        normalized_key = _orthographic_normalize(result.value)
        if not normalized_key:
            return result
        cluster = self._find_exact_cluster(self._clusters_by_field.get(field_contract.internal_key, []), normalized_key)
        if cluster is None:
            return result
        return _result_for_mode(result, cluster.canonical_value, apply_to_result=apply_to_result)

    def _create_cluster(self, raw_value: str, normalized_key: str) -> CanonicalCluster:
        canonical_value = _canonicalize_text_value(raw_value)
        return CanonicalCluster(
            canonical_value=canonical_value,
            variant_keys={normalized_key},
            embedding_text=canonical_value,
            embedding=self._get_embedding(canonical_value),
        )

    @staticmethod
    def _find_exact_cluster(clusters: Iterable[CanonicalCluster], normalized_key: str) -> CanonicalCluster | None:
        for cluster in clusters:
            if normalized_key in cluster.variant_keys:
                return cluster
        return None

    def _rank_candidate_clusters(
        self,
        current_embedding: list[float],
        clusters: Iterable[CanonicalCluster],
    ) -> list[tuple[CanonicalCluster, float]]:
        ranked: list[tuple[CanonicalCluster, float]] = []
        for cluster in clusters:
            similarity = cosine_similarity(current_embedding, cluster.embedding)
            ranked.append((cluster, similarity))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked[: self.max_candidates]

    def _rank_lexical_candidate_clusters(
        self,
        raw_value: str,
        clusters: Iterable[CanonicalCluster],
    ) -> list[CanonicalCluster]:
        value_signature = _build_lexical_signature(raw_value)
        ranked: list[tuple[CanonicalCluster, int, int]] = []
        for cluster in clusters:
            score = _lexical_candidate_score(
                value_signature,
                _build_lexical_signature(cluster.canonical_value),
            )
            if score is None:
                continue
            relation_score, token_delta = score
            ranked.append((cluster, relation_score, token_delta))
        ranked.sort(key=lambda item: (-item[1], item[2], len(item[0].canonical_value)))
        return [cluster for cluster, _, _ in ranked[: self.max_candidates]]

    def _evaluate_candidate_cluster(
        self,
        *,
        asin: str,
        field_contract: RunFieldContract,
        raw_value: str,
        normalized_key: str,
        cluster: CanonicalCluster,
        apply_to_result: bool,
        similarity: float,
        reason: str,
        result: FieldResult,
    ) -> FieldResult | None:
        judgment = self._judge_equivalence(
            asin=asin,
            field_contract=field_contract,
            new_value=raw_value,
            candidate_value=cluster.canonical_value,
        )
        if judgment.decision != "equivalent" or judgment.confidence < self.judgment_confidence_threshold:
            action = "abstain" if judgment.decision == "uncertain" else "no_merge"
            self._record_event(
                asin=asin,
                internal_key=field_contract.internal_key,
                mode=_merge_mode_label(apply_to_result),
                action=action,
                raw_value=raw_value,
                canonical_value=cluster.canonical_value,
                candidate_value=cluster.canonical_value,
                similarity=similarity,
                confidence=judgment.confidence,
                reason=reason,
            )
            LOGGER.info(
                "field_merge_event asin=%s internal_key=%s mode=%s action=%s raw_value=%s candidate_value=%s similarity=%.4f confidence=%.2f reason=%s",
                asin,
                field_contract.internal_key,
                _merge_mode_label(apply_to_result),
                action,
                raw_value,
                cluster.canonical_value,
                similarity,
                judgment.confidence,
                reason,
            )
            return None

        candidate_value = cluster.canonical_value
        canonical_value = _canonicalize_text_value(judgment.canonical_value) or cluster.canonical_value
        cluster.variant_keys.add(normalized_key)
        if canonical_value != cluster.canonical_value:
            cluster.canonical_value = canonical_value
            cluster.embedding_text = canonical_value
            cluster.embedding = self._get_embedding(canonical_value)
        self._record_event(
            asin=asin,
            internal_key=field_contract.internal_key,
            mode=_merge_mode_label(apply_to_result),
            action="semantic_merge",
            raw_value=raw_value,
            canonical_value=cluster.canonical_value,
            candidate_value=candidate_value,
            similarity=similarity,
            confidence=judgment.confidence,
            reason=reason,
        )
        LOGGER.info(
            "field_merge_event asin=%s internal_key=%s mode=%s action=semantic_merge raw_value=%s canonical_value=%s similarity=%.4f confidence=%.2f reason=%s",
            asin,
            field_contract.internal_key,
            _merge_mode_label(apply_to_result),
            raw_value,
            cluster.canonical_value,
            similarity,
            judgment.confidence,
            reason,
        )
        return _result_for_mode(result, cluster.canonical_value, apply_to_result=apply_to_result)

    def _judge_equivalence(
        self,
        *,
        asin: str,
        field_contract: RunFieldContract,
        new_value: str,
        candidate_value: str,
    ) -> "_CanonicalizationJudgment":
        comparison_key = _build_comparison_key(new_value, candidate_value)
        field_judgments = self._judgments_by_field.setdefault(field_contract.internal_key, {})
        cached = field_judgments.get(comparison_key)
        if cached is not None:
            return cached
        response_text = self.client.create_chat_completion(
            _CANONICALIZATION_SYSTEM_PROMPT,
            (
                f"Field internal key: {field_contract.internal_key}\n"
                f"Requested label: {field_contract.requested_label}\n"
                f"Output type: {field_contract.output_type}\n"
                f"Value A: {new_value}\n"
                f"Value B: {candidate_value}\n"
                "Should these be standardized to the same final value for this field in an enterprise product dataset?"
            ),
            asin=asin,
            field="__run_scoped_canonicalization__",
            retry_attempt=0,
        )
        payload = _parse_json_response(response_text)
        judgment = _CanonicalizationJudgment(
            decision=str(payload.get("decision", "") or "").strip().lower(),
            canonical_value=normalize_whitespace(str(payload.get("canonical_value", "") or "")),
            confidence=_coerce_confidence(payload.get("confidence")),
        )
        field_judgments[comparison_key] = judgment
        return judgment

    def _get_embedding(self, text: str) -> list[float]:
        cached = self.embedding_cache.get(self.embedding_model, text)
        if cached is not None:
            return cached
        embedding = self.client.create_embeddings([text], cache_hits=0)[0]
        self.embedding_cache.set(self.embedding_model, text, embedding)
        self.embedding_cache.save()
        return embedding

    def _record_event(
        self,
        *,
        asin: str,
        internal_key: str,
        mode: str,
        action: str,
        raw_value: str,
        canonical_value: str,
        candidate_value: str = "",
        similarity: float = 0.0,
        confidence: float = 0.0,
        reason: str = "",
    ) -> None:
        event = MergeDecisionEvent(
            asin=asin,
            internal_key=internal_key,
            mode=mode,
            action=action,
            decision=_decision_for_event(action, reason=reason),
            raw_value=raw_value,
            canonical_value=canonical_value,
            candidate_value=candidate_value,
            similarity=similarity,
            confidence=confidence,
            reason=reason,
        )
        self._events_by_asin.setdefault(asin, []).append(event)


@dataclass(frozen=True)
class _CanonicalizationJudgment:
    decision: str
    canonical_value: str
    confidence: float


@dataclass(frozen=True)
class _LexicalSignature:
    tokens: tuple[str, ...]
    singular_tokens: tuple[str, ...]
    joined_singular_text: str
    numeric_tokens: tuple[str, ...]


def _replace_value(result: FieldResult, new_value: str) -> FieldResult:
    return FieldResult(
        value=new_value,
        source=result.source,
        confidence=result.confidence,
        evidence=result.evidence,
    )


def _result_for_mode(result: FieldResult, new_value: str, *, apply_to_result: bool) -> FieldResult:
    if not apply_to_result:
        return result
    return _replace_value(result, new_value)


def _merge_mode_label(apply_to_result: bool) -> str:
    return "apply" if apply_to_result else "shadow"


def _decision_for_event(action: str, *, reason: str) -> str:
    if action in {"semantic_merge", "exact_merge"}:
        return "equivalent"
    if action == "abstain":
        return "uncertain"
    if action == "no_merge":
        return "distinct"
    if action == "new_cluster" and reason == "no_match":
        return "distinct"
    return ""


def _canonicalize_text_value(text: str) -> str:
    return normalize_whitespace(text).lower()


def _orthographic_normalize(text: str) -> str:
    lowered = normalize_whitespace(text).casefold().replace("&", " and ")
    lowered = _NORMALIZATION_CLEANUP_PATTERN.sub(" ", lowered)
    return normalize_whitespace(lowered)


def _build_comparison_key(left: str, right: str) -> tuple[str, str]:
    normalized_pair = sorted((_orthographic_normalize(left), _orthographic_normalize(right)))
    return normalized_pair[0], normalized_pair[1]


def _build_lexical_signature(text: str) -> _LexicalSignature:
    tokens = tuple(token for token in _orthographic_normalize(text).split() if len(token) > 1)
    singular_tokens = tuple(_singularize_token(token) for token in tokens)
    return _LexicalSignature(
        tokens=tokens,
        singular_tokens=singular_tokens,
        joined_singular_text=" ".join(singular_tokens),
        numeric_tokens=tuple(token for token in tokens if token.isdigit()),
    )


def _lexical_candidate_score(
    left: _LexicalSignature,
    right: _LexicalSignature,
) -> tuple[int, int] | None:
    if not left.singular_tokens or not right.singular_tokens:
        return None
    if left.numeric_tokens != right.numeric_tokens and (left.numeric_tokens or right.numeric_tokens):
        return None
    if left.joined_singular_text == right.joined_singular_text:
        return None

    shorter, longer = (left, right) if len(left.singular_tokens) <= len(right.singular_tokens) else (right, left)
    token_delta = len(longer.singular_tokens) - len(shorter.singular_tokens)
    if token_delta < 1 or token_delta > 2:
        return None

    shorter_set = set(shorter.singular_tokens)
    longer_set = set(longer.singular_tokens)
    if not shorter_set or not shorter_set.issubset(longer_set):
        return None

    shorter_text = shorter.joined_singular_text
    longer_text = longer.joined_singular_text
    if longer_text.startswith(f"{shorter_text} ") or longer_text.endswith(f" {shorter_text}"):
        return 3, token_delta
    if shorter_text in longer_text:
        return 2, token_delta
    return 1, token_delta


def _singularize_token(token: str) -> str:
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith(("ches", "shes", "sses", "xes", "zes")) and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _parse_json_response(response_text: str) -> dict:
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    data = json.loads(cleaned)
    return data if isinstance(data, dict) else {}


def _coerce_confidence(raw_value: object) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))
