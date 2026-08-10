"""Run-scoped unit of work for canonical-store writes (ADR-004, phase 1).

The canonicalizer used to mutate the persistent store the moment the LLM returned a
verdict, so a decision that consolidation later rejected (a value collision, or a row
that went to review) was already in memory and got flushed to disk unconditionally at
end of run. Nothing between "decided" and "persisted" could retract it.

This module puts a transaction boundary between those two points:

  * every canonicalization stages an immutable `DecisionRecord` and gets a `decision_id`
  * consolidation records an accepted/rejected outcome per decision
  * only transitively-accepted decisions are applied to the base store at `commit()`

Reads during the run see base + staged writes, so a canonical created early in a run is
still matchable later in the same run. Nothing staged reaches the base store — let alone
disk — until commit.

Two policies:
  * `defer=True`  (the runner) -- writes are buffered and gated on the outcome
  * `defer=False` (legacy/tests) -- writes apply immediately, preserving the old
    write-through behavior for callers that just want an engine and a store

A decision is committed only if ALL of these hold:
  1. consolidation explicitly recorded an accepted outcome for it
  2. it is commit-eligible (shadow-flagged decisions are not)
  3. every decision it depends on is itself committed

Rule 1 is deliberately "rejected unless proven accepted": an alias whose only supporting
row was dropped never enters the permanent store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .canonical_store import CanonicalStore

LOGGER = logging.getLogger(__name__)

_AUDIT_FIELDS = [
    "decision_id", "normalized_key", "raw_name", "canonical", "method", "confidence",
    "guard_rule", "guard_verdict", "asin", "outcome", "reason",
]


@dataclass(frozen=True)
class DecisionRecord:
    """One canonicalization decision, immutable once staged."""

    decision_id: str
    normalized_key: str
    raw_name: str
    canonical: str
    method: str                       # cache | llm_match | llm_new | review
    confidence: float = 0.0
    creates_canonical: bool = False
    canonical_variant_key: str = ""   # the canonical's own key, for llm_new
    disambiguation_class: str = ""
    guard_rule: str = ""              # "" | the identity rule that fired (phase 3)
    guard_verdict: str = "allow"      # allow | review | adjudicate
    commit_eligible: bool = True      # False: shadow-flagged, quarantined cache hit
    depends_on: tuple[str, ...] = ()  # decisions that created what this one relies on

    @property
    def writes_store(self) -> bool:
        """Cache hits and reviews resolve rows but add nothing to the vocabulary."""
        return self.method in ("llm_match", "llm_new")


@dataclass(frozen=True)
class DecisionOutcome:
    accepted: bool
    reason: str = ""
    asin: str = ""


@dataclass(frozen=True)
class Resolution:
    accepted: frozenset[str]
    rejected: dict[str, str]          # decision_id -> reason

    def is_accepted(self, decision_id: str) -> bool:
        return decision_id in self.accepted

    def reason_for(self, decision_id: str) -> str:
        return self.rejected.get(decision_id, "")


@dataclass
class CommitAudit:
    committed: int = 0
    rejected: int = 0
    rows: list[dict[str, str]] = field(default_factory=list)

    @staticmethod
    def fieldnames() -> list[str]:
        return list(_AUDIT_FIELDS)


class RunDecisionLedger:
    """Write-buffer + decision log over a `CanonicalStore`.

    Exposes the read surface the canonicalizer needs (`lookup`, `canonical_names`,
    `disambiguation_class_of`, `__contains__`), so it is a drop-in for the engine's
    `store=` argument.
    """

    def __init__(self, base: CanonicalStore, *, defer: bool = True) -> None:
        self._base = base
        self._defer = defer
        self._records: dict[str, DecisionRecord] = {}
        self._outcomes: dict[str, DecisionOutcome] = {}
        self._by_key: dict[str, str] = {}          # normalized_key -> decision_id
        self._staged_alias: dict[str, str] = {}    # key -> canonical (deferred only)
        self._staged_canonical: dict[str, str] = {}  # canonical -> disambiguation class
        self._alias_owner: dict[str, str] = {}     # key -> decision that staged it
        self._canonical_owner: dict[str, str] = {}  # canonical -> decision that created it
        self._counter = 0

    @property
    def base(self) -> CanonicalStore:
        return self._base

    @property
    def deferred(self) -> bool:
        return self._defer

    # -- reads (base + staged) ---------------------------------------------------
    def lookup(self, variant_key: str) -> str | None:
        staged = self._staged_alias.get(variant_key)
        if staged is not None:
            return staged
        return self._base.lookup(variant_key)

    def canonical_names(self) -> list[str]:
        names = self._base.canonical_names()
        known = set(names)
        return [*names, *(n for n in self._staged_canonical if n not in known)]

    def disambiguation_class_of(self, canonical_name: str) -> str:
        existing = self._base.disambiguation_class_of(canonical_name)
        if existing:
            return existing
        return self._staged_canonical.get(canonical_name, "")

    def __contains__(self, canonical_name: object) -> bool:
        return canonical_name in self._base or canonical_name in self._staged_canonical

    def __len__(self) -> int:
        return len(self._base) + len(self._staged_canonical)

    # -- staging -----------------------------------------------------------------
    def stage_cache_hit(
        self, normalized_key: str, canonical: str, *, raw_name: str = "",
        guard_rule: str = "", guard_verdict: str = "allow", commit_eligible: bool = True,
    ) -> DecisionRecord:
        """Record a resolution that adds nothing new but can still be rejected.

        If the alias came from a write staged earlier in this run, the cache hit
        depends on it: reject the staging decision and this row goes too.
        """
        owner = self._alias_owner.get(normalized_key)
        return self._stage(
            normalized_key=normalized_key, raw_name=raw_name, canonical=canonical,
            method="cache", confidence=1.0,
            guard_rule=guard_rule, guard_verdict=guard_verdict, commit_eligible=commit_eligible,
            depends_on=(owner,) if owner else (),
        )

    def stage_match(
        self, *, normalized_key: str, raw_name: str, canonical: str,
        confidence: float, disambiguation_class: str = "",
        guard_rule: str = "", guard_verdict: str = "allow", commit_eligible: bool = True,
    ) -> DecisionRecord:
        owner = self._canonical_owner.get(canonical)
        record = self._stage(
            normalized_key=normalized_key, raw_name=raw_name, canonical=canonical,
            method="llm_match", confidence=confidence,
            disambiguation_class=disambiguation_class,
            guard_rule=guard_rule, guard_verdict=guard_verdict, commit_eligible=commit_eligible,
            depends_on=(owner,) if owner else (),
        )
        self._register_alias(record)
        return record

    def stage_new(
        self, *, normalized_key: str, raw_name: str, canonical: str,
        canonical_variant_key: str = "", confidence: float, disambiguation_class: str = "",
        guard_rule: str = "", guard_verdict: str = "allow", commit_eligible: bool = True,
    ) -> DecisionRecord:
        record = self._stage(
            normalized_key=normalized_key, raw_name=raw_name, canonical=canonical,
            method="llm_new", confidence=confidence, creates_canonical=True,
            canonical_variant_key=canonical_variant_key,
            disambiguation_class=disambiguation_class,
            guard_rule=guard_rule, guard_verdict=guard_verdict, commit_eligible=commit_eligible,
        )
        self._register_canonical(record)
        self._register_alias(record)
        return record

    def stage_review(self, *, normalized_key: str, raw_name: str, reason: str, suggestion: str = "") -> DecisionRecord:
        """A decision that resolves nothing; logged so the audit shows why."""
        return self._stage(
            normalized_key=normalized_key, raw_name=raw_name, canonical=suggestion,
            method="review", guard_rule=reason, commit_eligible=False,
        )

    def _stage(self, **kwargs) -> DecisionRecord:
        key = kwargs.get("normalized_key", "")
        method = kwargs.get("method", "")
        # One record per normalized key per run, so rejecting an alias is coherent
        # across every row that used it. Reviews are not deduped: the same string can
        # fail for different reasons and each should appear in the audit.
        if key and method != "review":
            existing_id = self._by_key.get(key)
            if existing_id is not None:
                return self._records[existing_id]
        self._counter += 1
        record = DecisionRecord(decision_id=f"d{self._counter:05d}", **kwargs)
        self._records[record.decision_id] = record
        if key and method != "review":
            self._by_key[key] = record.decision_id
        if not self._defer and record.writes_store:
            self._apply(record)
        return record

    def _register_alias(self, record: DecisionRecord) -> None:
        if not self._defer:
            return
        self._staged_alias[record.normalized_key] = record.canonical
        self._alias_owner[record.normalized_key] = record.decision_id

    def _register_canonical(self, record: DecisionRecord) -> None:
        if not self._defer:
            return
        # First creator owns the canonical: a later decision naming the same canonical
        # depends on the original, it does not take over the dependency edge.
        self._staged_canonical.setdefault(record.canonical, record.disambiguation_class)
        self._canonical_owner.setdefault(record.canonical, record.decision_id)
        if record.canonical_variant_key:
            self._staged_alias.setdefault(record.canonical_variant_key, record.canonical)
            self._alias_owner.setdefault(record.canonical_variant_key, record.decision_id)

    # -- outcomes ----------------------------------------------------------------
    def record_outcome(self, decision_id: str, *, accepted: bool, reason: str = "", asin: str = "") -> None:
        """Record consolidation's verdict. A rejection is sticky — it never flips back."""
        if not decision_id or decision_id not in self._records:
            return
        previous = self._outcomes.get(decision_id)
        if previous is not None and not previous.accepted:
            return
        self._outcomes[decision_id] = DecisionOutcome(accepted=accepted, reason=reason, asin=asin)

    def record_for(self, decision_id: str) -> DecisionRecord | None:
        return self._records.get(decision_id)

    def records(self) -> list[DecisionRecord]:
        return list(self._records.values())

    def resolve(self) -> Resolution:
        """Compute the transitively-accepted set. Absent outcome == rejected."""
        accepted: set[str] = set()
        rejected: dict[str, str] = {}
        for decision_id, record in self._records.items():
            outcome = self._outcomes.get(decision_id)
            if outcome is None:
                rejected[decision_id] = "no_surviving_row"
            elif not outcome.accepted:
                rejected[decision_id] = outcome.reason or "rejected"
            elif not record.commit_eligible:
                rejected[decision_id] = f"not_commit_eligible:{record.guard_rule or record.guard_verdict}"
            else:
                accepted.add(decision_id)

        # Transitive closure: dropping a canonical drops everything that leans on it.
        changed = True
        while changed:
            changed = False
            for decision_id in sorted(accepted):
                unmet = [dep for dep in self._records[decision_id].depends_on if dep not in accepted]
                if unmet:
                    accepted.discard(decision_id)
                    rejected[decision_id] = f"dependency_rejected:{unmet[0]}"
                    changed = True
        return Resolution(accepted=frozenset(accepted), rejected=rejected)

    # -- commit ------------------------------------------------------------------
    def commit(self, resolution: Resolution | None = None) -> CommitAudit:
        """Apply accepted writes to the base store and return the audit."""
        resolution = resolution if resolution is not None else self.resolve()
        audit = CommitAudit()
        for decision_id, record in self._records.items():
            accepted = resolution.is_accepted(decision_id)
            outcome = self._outcomes.get(decision_id)
            audit.rows.append({
                "decision_id": decision_id,
                "normalized_key": record.normalized_key,
                "raw_name": record.raw_name,
                "canonical": record.canonical,
                "method": record.method,
                "confidence": f"{record.confidence:.2f}",
                "guard_rule": record.guard_rule,
                "guard_verdict": record.guard_verdict,
                "asin": outcome.asin if outcome else "",
                "outcome": "committed" if accepted else "rejected",
                "reason": "" if accepted else resolution.reason_for(decision_id),
            })
            if accepted:
                audit.committed += 1
            else:
                audit.rejected += 1
            if not accepted or not record.writes_store or not self._defer:
                continue
            self._apply(record)

        if not self._defer and audit.rejected:
            # Immediate mode has no transaction boundary; say so rather than implying
            # the rejections were prevented from reaching the store.
            LOGGER.warning(
                "canonical ledger in immediate mode: %s rejected decision(s) were already "
                "written to the store and cannot be retracted", audit.rejected,
            )
        return audit

    def _apply(self, record: DecisionRecord) -> None:
        if record.method == "llm_new":
            self._base.add_canonical(
                record.canonical, disambiguation_class=record.disambiguation_class,
                created_by="llm", confidence=record.confidence,
                variant_key=record.canonical_variant_key or None,
            )
            self._base.add_variant(record.normalized_key, record.canonical)
        elif record.method == "llm_match":
            if record.canonical not in self._base:
                LOGGER.warning(
                    "canonical ledger skipping match onto missing canonical %r (decision=%s)",
                    record.canonical, record.decision_id,
                )
                return
            self._base.add_variant(record.normalized_key, record.canonical)
