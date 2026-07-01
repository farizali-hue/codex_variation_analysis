"""The canonicalizer engine ("brain") — EDC: normalize -> cache -> LLM decide.

This orchestrates the per-ingredient decision but stays decoupled from *how* the LLM
is implemented (real OpenAI client in slice 3; a deterministic stub in tests) and from
*how* candidates are chosen (whole-list now; embedding top-K later). Both are injected
via small interfaces (`IngredientLLM`, `CandidateProvider`).

Decision flow for one ingredient string:
  1. normalize -> stable key
  2. store.lookup(key) hit -> done, no LLM ("cache")
  3. miss -> CandidateProvider gives the candidate canonicals -> LLM decides
  4. guardrails: disambiguation-class conflict OR confidence < threshold -> review queue
  5. accepted match/new -> write back to the store (self-populating)

The engine never silently merges across disambiguation classes (e.g. D2 into D3): if the
LLM's class for the incoming item conflicts with the matched canonical's class, the item
goes to review instead of being merged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from .canonical_store import CanonicalStore
from .run_scoped_canonicalization import _orthographic_normalize

LOGGER = logging.getLogger(__name__)

_TRADEMARK = re.compile(r"[™®©]")
_PAREN_AS = re.compile(r"\s*\(as [^)]*\)", re.IGNORECASE)
_PAREN_MK = re.compile(r"\s*\(mk-?\d+\)", re.IGNORECASE)


def default_normalize(name: str) -> str:
    """Generic, category-wide tidy-up used to build the stable store key."""
    text = _TRADEMARK.sub("", name or "")
    text = _PAREN_AS.sub("", text)
    text = _PAREN_MK.sub("", text)
    return _orthographic_normalize(text)


@dataclass
class LLMVerdict:
    """What an `IngredientLLM` returns for one ingredient."""

    decision: str  # "match" | "new" | "uncertain"
    canonical_name: str = ""
    disambiguation_class: str = ""
    confidence: float = 0.0


class IngredientLLM(Protocol):
    def decide(
        self, *, raw_name: str, normalized_name: str, candidates: list[str], context: str
    ) -> LLMVerdict: ...


class CandidateProvider(Protocol):
    def candidates(self, normalized_name: str, store: CanonicalStore) -> list[str]: ...


class WholeListCandidateProvider:
    """v1: hand the LLM the whole master list (small vocabulary). See plan slice 2."""

    def candidates(self, normalized_name: str, store: CanonicalStore) -> list[str]:
        return store.canonical_names()


@dataclass
class CanonicalDecision:
    raw_name: str
    normalized: str
    canonical: str = ""          # the column to place the value in ("" when needs_review)
    is_new: bool = False
    confidence: float = 0.0
    method: str = ""             # cache | llm_match | llm_new | review | empty
    disambiguation_class: str = ""
    needs_review: bool = False
    review_reason: str = ""
    suggestion: str = ""         # best guess shown in the review queue
    candidates_considered: list[str] = field(default_factory=list)


class IngredientCanonicalizer:
    def __init__(
        self,
        *,
        store: CanonicalStore,
        llm: IngredientLLM,
        candidate_provider: CandidateProvider | None = None,
        normalize=default_normalize,
        confidence_threshold: float = 0.75,
    ) -> None:
        self.store = store
        self.llm = llm
        self.candidate_provider = candidate_provider or WholeListCandidateProvider()
        self.normalize = normalize
        self.confidence_threshold = confidence_threshold

    def canonicalize(self, raw_name: str, *, context: str = "") -> CanonicalDecision:
        normalized = self.normalize(raw_name)
        if not normalized:
            return CanonicalDecision(
                raw_name=raw_name, normalized="", method="empty",
                needs_review=True, review_reason="empty_after_normalization",
            )

        hit = self.store.lookup(normalized)
        if hit is not None:
            return CanonicalDecision(
                raw_name=raw_name, normalized=normalized, canonical=hit,
                confidence=1.0, method="cache",
                disambiguation_class=self.store.disambiguation_class_of(hit),
            )

        candidates = self.candidate_provider.candidates(normalized, self.store)
        verdict = self.llm.decide(
            raw_name=raw_name, normalized_name=normalized, candidates=candidates, context=context
        )
        return self._apply_verdict(raw_name, normalized, candidates, verdict, context)

    # -- internals --------------------------------------------------------------
    def _review(
        self, raw_name, normalized, candidates, *, reason, suggestion="", conf=0.0, dclass=""
    ) -> CanonicalDecision:
        LOGGER.info("ingredient_review raw=%s normalized=%s reason=%s suggestion=%s",
                    raw_name, normalized, reason, suggestion)
        return CanonicalDecision(
            raw_name=raw_name, normalized=normalized, canonical="", method="review",
            needs_review=True, review_reason=reason, suggestion=suggestion,
            confidence=conf, disambiguation_class=dclass, candidates_considered=list(candidates),
        )

    def _apply_verdict(self, raw_name, normalized, candidates, verdict: LLMVerdict, context) -> CanonicalDecision:
        decision = (verdict.decision or "").strip().lower()
        incoming_class = (verdict.disambiguation_class or "").strip()

        if decision == "match":
            matched = verdict.canonical_name.strip()
            if matched not in self.store:
                # LLM pointed at a canonical that isn't in the store -> don't trust it
                return self._review(raw_name, normalized, candidates,
                                    reason="matched_unknown_canonical", suggestion=matched,
                                    conf=verdict.confidence, dclass=incoming_class)
            matched_class = self.store.disambiguation_class_of(matched)
            if matched_class and incoming_class and matched_class != incoming_class:
                # hard guard: never merge across disambiguation classes (e.g. D2 into D3)
                return self._review(raw_name, normalized, candidates,
                                    reason="disambiguation_conflict", suggestion=matched,
                                    conf=verdict.confidence, dclass=incoming_class)
            if verdict.confidence < self.confidence_threshold:
                return self._review(raw_name, normalized, candidates,
                                    reason="low_confidence", suggestion=matched,
                                    conf=verdict.confidence, dclass=incoming_class)
            self.store.add_variant(normalized, matched)
            return CanonicalDecision(
                raw_name=raw_name, normalized=normalized, canonical=matched,
                confidence=verdict.confidence, method="llm_match",
                disambiguation_class=matched_class or incoming_class,
                candidates_considered=list(candidates),
            )

        if decision == "new":
            name = verdict.canonical_name.strip()
            if not name:
                return self._review(raw_name, normalized, candidates,
                                    reason="new_without_name", conf=verdict.confidence)
            if verdict.confidence < self.confidence_threshold:
                return self._review(raw_name, normalized, candidates,
                                    reason="low_confidence_new", suggestion=name,
                                    conf=verdict.confidence, dclass=incoming_class)
            self.store.add_canonical(
                name, disambiguation_class=incoming_class, created_by="llm",
                confidence=verdict.confidence, variant_key=self.normalize(name),
            )
            self.store.add_variant(normalized, name)
            return CanonicalDecision(
                raw_name=raw_name, normalized=normalized, canonical=name, is_new=True,
                confidence=verdict.confidence, method="llm_new",
                disambiguation_class=incoming_class, candidates_considered=list(candidates),
            )

        # uncertain (or anything unexpected) -> review, never guess
        return self._review(raw_name, normalized, candidates,
                            reason="uncertain", suggestion=verdict.canonical_name.strip(),
                            conf=verdict.confidence, dclass=incoming_class)


# --- Real LLM adapter (slice 3) -------------------------------------------------
# Imported lazily-friendly: only depends on an injected OpenAI-style client.
from .run_scoped_canonicalization import _coerce_confidence, _parse_json_response  # noqa: E402

_SYSTEM_PROMPT = (
    "You standardize ONE supplement ingredient label into a canonical column name for a "
    "product-comparison dataset. You are given the raw label and the list of canonical names "
    "that already exist.\n\n"
    "RULES:\n"
    "1. Combine different chemical FORMS / salts / sources / botanical qualifiers of the SAME "
    "nutrient into one base name. Strip '(as ...)', Latin binomials, plant parts (fruit/leaf/"
    "root/bark/seed), extraction ratios (e.g. 4:1), trademark/brand wording, 'Organic'/'Vegan'. "
    "Examples: 'Magnesium Glycinate'->'Magnesium'; 'Vitamin D3 (as cholecalciferol)'->'Vitamin D3'; "
    "'Pumpkin Seed Oil (Cucurbita pepo)'->'Pumpkin Seed Oil'.\n"
    "2. NEVER merge genuinely DIFFERENT nutrients. Vitamin D2 (ergocalciferol) is NOT Vitamin D3 "
    "(cholecalciferol). Vitamin K1 (phylloquinone) is NOT Vitamin K2 (menaquinone); MK-4 and MK-7 "
    "are BOTH forms of Vitamin K2. If you are not sure two things are the same nutrient, return "
    "'uncertain' rather than guessing.\n"
    "3. 'disambiguation_class' is a short stable tag for the distinct nutrient identity "
    "(e.g. vitamin_d3, vitamin_d2, vitamin_k1, vitamin_k2, magnesium). Use the SAME tag for all "
    "forms of one nutrient and DIFFERENT tags for different nutrients. It is enforced as a guard.\n\n"
    "Return JSON only: {\"decision\":\"match|new|uncertain\", \"canonical_name\":\"...\", "
    "\"disambiguation_class\":\"...\", \"confidence\":0.0-1.0}. "
    "For 'match', canonical_name MUST be copied EXACTLY from the existing list. For 'new', it is the "
    "clean base name to create. Prefer 'uncertain' over a wrong merge."
)


def _build_user_prompt(raw_name: str, candidates: list[str], context: str) -> str:
    lines = [f"Ingredient label: {raw_name}"]
    if context and context.strip() and context.strip() != raw_name:
        lines.append(f"Evidence (supplement-facts excerpt): {context.strip()[:600]}")
    if candidates:
        lines.append("Existing canonical names (copy one EXACTLY for a match):")
        lines.extend(f"- {name}" for name in candidates)
    else:
        lines.append("Existing canonical names: (none yet)")
    return "\n".join(lines)


class OpenAIIngredientLLM:
    """Real `IngredientLLM` backed by the project's OpenAI client (slice 3)."""

    def __init__(self, client, *, model: str | None = None) -> None:
        self.client = client
        self.model = model

    def decide(self, *, raw_name: str, normalized_name: str, candidates: list[str], context: str) -> LLMVerdict:
        text = self.client.create_chat_completion(
            _SYSTEM_PROMPT,
            _build_user_prompt(raw_name, candidates, context),
            asin="__ingredient_canonicalization__",
            field="ingredient_canonicalization",
            retry_attempt=0,
            model=self.model,
        )
        payload = _parse_json_response(text)
        return LLMVerdict(
            decision=str(payload.get("decision", "") or "").strip().lower(),
            canonical_name=str(payload.get("canonical_name", "") or "").strip(),
            disambiguation_class=str(payload.get("disambiguation_class", "") or "").strip(),
            confidence=_coerce_confidence(payload.get("confidence")),
        )
