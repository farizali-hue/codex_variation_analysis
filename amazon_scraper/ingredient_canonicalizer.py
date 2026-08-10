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
  5. accepted match/new -> staged in the run's decision ledger (self-populating)

Writes go through a `RunDecisionLedger` rather than straight to the store, so a decision
consolidation later rejects never reaches the permanent vocabulary (ADR-004). Pass a
deferred ledger as `store=` to get that gating; pass a plain `CanonicalStore` and the
ledger runs in immediate mode, which is the historical write-through behavior.

The engine never silently merges across disambiguation classes (e.g. D2 into D3): if the
LLM's class for the incoming item conflicts with the matched canonical's class, the item
goes to review instead of being merged.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from .canonical_decisions import RunDecisionLedger
from .canonical_store import CanonicalStore
from .ingredient_identity import GuardVerdict, check_merge, classify
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
    decision_id: str = ""        # handle into the run's decision ledger ("" = untracked)


class IngredientCanonicalizer:
    def __init__(
        self,
        *,
        store: CanonicalStore | RunDecisionLedger,
        llm: IngredientLLM,
        candidate_provider: CandidateProvider | None = None,
        normalize=default_normalize,
        confidence_threshold: float = 0.75,
        identity_guard_mode: str = "off",
        adjudicator=None,
    ) -> None:
        # Reads and writes both go through the ledger; a plain store gets wrapped in an
        # immediate-mode one so there is a single write path, not two.
        self.store = store
        self.ledger = store if isinstance(store, RunDecisionLedger) else RunDecisionLedger(store, defer=False)
        self.llm = llm
        self.candidate_provider = candidate_provider or WholeListCandidateProvider()
        self.normalize = normalize
        self.confidence_threshold = confidence_threshold
        self.identity_guard_mode = identity_guard_mode
        # Optional second opinion for `adjudicate` verdicts. Without one, an
        # unverifiable pair goes to review rather than through.
        self.adjudicator = adjudicator

    def canonicalize(self, raw_name: str, *, context: str = "") -> CanonicalDecision:
        normalized = self.normalize(raw_name)
        if not normalized:
            return CanonicalDecision(
                raw_name=raw_name, normalized="", method="empty",
                needs_review=True, review_reason="empty_after_normalization",
            )

        hit = self.store.lookup(normalized)
        if hit is not None:
            # Guard cache hits too. A legacy bad alias would otherwise resolve for
            # free forever, never facing the checks that would have caught it.
            guard = self._guard(raw_name, normalized, hit)
            if guard.blocks and self.identity_guard_mode == "apply":
                return self._review(raw_name, normalized, [], reason=f"identity_guard:{guard.rule}",
                                    suggestion=hit, conf=1.0)
            record = self.ledger.stage_cache_hit(
                normalized, hit, raw_name=raw_name,
                guard_rule=guard.rule, guard_verdict=guard.verdict,
                commit_eligible=not guard.blocks,
            )
            return CanonicalDecision(
                raw_name=raw_name, normalized=normalized, canonical=hit,
                confidence=1.0, method="cache",
                disambiguation_class=self.store.disambiguation_class_of(hit),
                decision_id=record.decision_id,
            )

        candidates = self.candidate_provider.candidates(normalized, self.store)
        verdict = self.llm.decide(
            raw_name=raw_name, normalized_name=normalized, candidates=candidates, context=context
        )
        return self._apply_verdict(raw_name, normalized, candidates, verdict, context)

    # -- identity guards ---------------------------------------------------------
    def _guard(self, raw_name: str, normalized: str, candidate: str) -> GuardVerdict:
        """Structural check on a proposed merge; `allow` when the guards are off."""
        if self.identity_guard_mode == "off" or not candidate:
            return GuardVerdict("allow")
        verdict = check_merge(
            classify(raw_name, normalized),
            classify(candidate, self.normalize(candidate)),
        )
        if verdict.verdict != "adjudicate":
            return verdict
        if self.adjudicator is None:
            return GuardVerdict("review", verdict.rule, f"{verdict.detail}; no adjudicator")
        try:
            same = self.adjudicator(raw_name=raw_name, candidate=candidate, rule=verdict.rule)
        except Exception:
            LOGGER.warning("identity adjudication failed raw=%s candidate=%s", raw_name, candidate,
                           exc_info=True)
            return GuardVerdict("review", verdict.rule, f"{verdict.detail}; adjudication failed")
        if same:
            return GuardVerdict("allow", verdict.rule, f"{verdict.detail}; adjudicated same")
        return GuardVerdict("review", verdict.rule, f"{verdict.detail}; adjudicated different")

    # -- internals --------------------------------------------------------------
    def _review(
        self, raw_name, normalized, candidates, *, reason, suggestion="", conf=0.0, dclass=""
    ) -> CanonicalDecision:
        LOGGER.info("ingredient_review raw=%s normalized=%s reason=%s suggestion=%s",
                    raw_name, normalized, reason, suggestion)
        record = self.ledger.stage_review(
            normalized_key=normalized, raw_name=raw_name, reason=reason, suggestion=suggestion,
        )
        return CanonicalDecision(
            raw_name=raw_name, normalized=normalized, canonical="", method="review",
            needs_review=True, review_reason=reason, suggestion=suggestion,
            confidence=conf, disambiguation_class=dclass, candidates_considered=list(candidates),
            decision_id=record.decision_id,
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
            guard = self._guard(raw_name, normalized, matched)
            if guard.blocks and self.identity_guard_mode == "apply":
                return self._review(raw_name, normalized, candidates,
                                    reason=f"identity_guard:{guard.rule}", suggestion=matched,
                                    conf=verdict.confidence, dclass=incoming_class)
            record = self.ledger.stage_match(
                normalized_key=normalized, raw_name=raw_name, canonical=matched,
                confidence=verdict.confidence,
                disambiguation_class=matched_class or incoming_class,
                guard_rule=guard.rule, guard_verdict=guard.verdict,
                commit_eligible=not guard.blocks,
            )
            return CanonicalDecision(
                raw_name=raw_name, normalized=normalized, canonical=matched,
                confidence=verdict.confidence, method="llm_match",
                disambiguation_class=matched_class or incoming_class,
                candidates_considered=list(candidates),
                decision_id=record.decision_id,
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
            # Guard `new` against the name it PROPOSES, not the nearest existing
            # canonical: a blend being incompatible with the nearest nutrient is a
            # reason to create its own column, not to refuse creation.
            guard = self._guard(raw_name, normalized, name)
            if guard.blocks and self.identity_guard_mode == "apply":
                return self._review(raw_name, normalized, candidates,
                                    reason=f"identity_guard:{guard.rule}", suggestion=name,
                                    conf=verdict.confidence, dclass=incoming_class)
            record = self.ledger.stage_new(
                normalized_key=normalized, raw_name=raw_name, canonical=name,
                canonical_variant_key=self.normalize(name),
                confidence=verdict.confidence, disambiguation_class=incoming_class,
                guard_rule=guard.rule, guard_verdict=guard.verdict,
                commit_eligible=not guard.blocks,
            )
            return CanonicalDecision(
                raw_name=raw_name, normalized=normalized, canonical=name, is_new=True,
                confidence=verdict.confidence, method="llm_new",
                disambiguation_class=incoming_class, candidates_considered=list(candidates),
                decision_id=record.decision_id,
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
    "COMBINE (same substance, different wording) -> 'match':\n"
    "- Salts, esters and chelates of the same element or molecule: 'Magnesium Glycinate' -> "
    "'Magnesium'; 'Zinc Picolinate' -> 'Zinc'.\n"
    "- '(as ...)' clauses, Latin binomials, plant parts (fruit/leaf/root/bark/seed), extraction "
    "ratios (4:1), trademark/brand wording, 'Organic'/'Vegan': 'Vitamin D3 (as cholecalciferol)' "
    "-> 'Vitamin D3'; 'Pumpkin Seed Oil (Cucurbita pepo)' -> 'Pumpkin Seed Oil'.\n"
    "- Spelling variants, typos and abbreviations of the same substance: 'gamma aminobutyric "
    "acid' -> 'GABA'; 'niacin' -> 'Vitamin B3'.\n"
    "- An unprefixed name and a stereochemical prefix of the SAME molecule: 'Taurine' and "
    "'L-Taurine' are the same.\n\n"
    "NEVER MERGE (different identity) -> return 'new' or 'uncertain', never 'match':\n"
    "1. MIXTURE vs SINGLE SUBSTANCE. A proprietary blend, matrix, complex, formula or 'blend' is "
    "a MIXTURE and is never a form of any single nutrient it contains. 'Electrolyte Hydration "
    "Matrix' is NOT Choline. A mixture gets its OWN canonical name.\n"
    "2. DIFFERENT NUTRIENTS. Vitamin D2 (ergocalciferol) is NOT Vitamin D3 (cholecalciferol). "
    "Vitamin K1 (phylloquinone) is NOT Vitamin K2 (menaquinone); MK-4 and MK-7 are BOTH forms of "
    "Vitamin K2.\n"
    "3. STEREOCHEMISTRY AND CLOSE MOLECULES. 'DL-Methionine' is NOT 'L-Methionine'. "
    "'DL-Phenylalanine' is NOT 'L-Phenylalanine'. 'L-Cystine' is NOT 'L-Cysteine' — these are "
    "different molecules, not different spellings.\n"
    "4. DIFFERENT BIOLOGICAL SOURCE. Whey protein is NOT soy protein. Different plant species are "
    "different ingredients even when plant part and extraction ratio match: rosemary (Rosmarinus "
    "officinalis) is NOT saw palmetto (Serenoa repens).\n"
    "5. MULTI-INGREDIENT STRINGS. A label listing several distinct ingredients is not any one of "
    "them -> 'uncertain'.\n"
    "6. FRAGMENTS with no identity of their own ('4:1 Extract', 'Extract Blend', 'Electrolytes') "
    "-> 'uncertain'.\n\n"
    "SOURCE COMPOUNDS: a source compound may share the base nutrient's NAME ('Magnesium Oxide' "
    "-> 'Magnesium') because that is a naming decision. Do NOT treat it as a statement about the "
    "amount: a source-compound row may report compound mass rather than elemental mass. Naming is "
    "your job; the amount is not.\n\n"
    "'new' IS A CORRECT ANSWER, NOT A FAILURE. Creating a new canonical name for a genuine "
    "ingredient with no match is the right outcome. Never pick the closest-looking name from the "
    "list merely because it is there. When you cannot tell, return 'uncertain' — that routes the "
    "item to a human, which is the desired result for a doubtful case.\n\n"
    "'disambiguation_class' is a short stable tag for the distinct identity (e.g. vitamin_d3, "
    "vitamin_d2, vitamin_k2, magnesium, l_methionine, dl_methionine). Use the SAME tag for all "
    "forms of one substance and DIFFERENT tags for different substances. For a mixture, tag the "
    "mixture itself, never a member nutrient. It is enforced as a guard.\n\n"
    "'confidence' is your probability that the label is the SAME substance as the canonical you "
    "chose. Use a value below 0.75 whenever you are unsure; do not report 0.95+ unless the "
    "identity is beyond doubt.\n\n"
    "Return JSON only: {\"decision\":\"match|new|uncertain\", \"canonical_name\":\"...\", "
    "\"disambiguation_class\":\"...\", \"confidence\":0.0-1.0}. "
    "For 'match', canonical_name MUST be copied EXACTLY from the existing list. For 'new', it is "
    "the clean base name to create."
)


_ADJUDICATION_SYSTEM_PROMPT = (
    "You are the SKEPTIC in a two-stage ingredient review. Another model already claimed "
    "these two supplement labels name the SAME substance. Your job is to argue the opposite.\n\n"
    "Give the strongest specific reason they are DISTINCT substances — different molecule, "
    "different stereochemistry, different salt where the base differs, synthetic vs natural "
    "form, a mixture versus one of its members, a remainder versus a total, or different "
    "biological source. Answer \"same\" ONLY if you cannot construct such a reason.\n\n"
    "Being unsure means answering \"different\": a wrong merge silently corrupts a shared "
    "dataset, while a wrong split only creates one extra column that a human can merge later.\n\n"
    "Return JSON only: {\"same\": true|false, \"reason\": \"...\"}"
)


class OpenAIIdentityAdjudicator:
    """Second opinion for `adjudicate` verdicts, framed to argue against merging."""

    def __init__(self, client, *, model: str | None = None) -> None:
        self.client = client
        self.model = model

    def __call__(self, *, raw_name: str, candidate: str, rule: str) -> bool:
        text = self.client.create_chat_completion(
            _ADJUDICATION_SYSTEM_PROMPT,
            f"Label A (incoming): {raw_name}\nLabel B (existing column): {candidate}\n"
            f"Structural concern raised: {rule}",
            asin="__ingredient_identity_adjudication__",
            field="ingredient_identity_adjudication",
            retry_attempt=0,
            model=self.model,
        )
        try:
            payload = _parse_json_response(text)
        except json.JSONDecodeError:
            LOGGER.warning("identity adjudication returned non-JSON; treating as different raw=%s",
                           raw_name)
            return False
        same = payload.get("same")
        if isinstance(same, str):
            same = same.strip().lower() in {"true", "yes", "same"}
        LOGGER.info("identity_adjudication raw=%s candidate=%s rule=%s same=%s reason=%s",
                    raw_name, candidate, rule, bool(same), str(payload.get("reason", ""))[:160])
        return bool(same)


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
        try:
            payload = _parse_json_response(text)
        except json.JSONDecodeError:
            # An unparseable verdict must become "uncertain" (-> review queue),
            # not an exception that aborts the consolidation run mid-ledger.
            LOGGER.warning(
                "ingredient_canonicalization malformed LLM response; treating as uncertain raw=%s",
                raw_name,
            )
            return LLMVerdict(decision="uncertain")
        return LLMVerdict(
            decision=str(payload.get("decision", "") or "").strip().lower(),
            canonical_name=str(payload.get("canonical_name", "") or "").strip(),
            disambiguation_class=str(payload.get("disambiguation_class", "") or "").strip(),
            confidence=_coerce_confidence(payload.get("confidence")),
        )
