"""Unit tests for the run decision ledger (ADR-004 phase 1) — no API calls.

Proves the transaction boundary between "the LLM decided" and "the store kept it":
- staged writes are invisible to the base store until commit
- reads during the run still see staged writes (within-run consistency)
- rejecting one decision drops only that alias
- rejection is transitive: a match onto a canonical whose creator was rejected goes too
- a decision no surviving row voted for is never committed
- transitively-rejected rows vanish from the wide CSV, not just from the cache
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from amazon_scraper.canonical_decisions import RunDecisionLedger
from amazon_scraper.canonical_store import CanonicalStore
from amazon_scraper.ledger_consolidation import consolidate_from_ledger

_LEDGER_HEADER = ["asin", "row_order", "name", "amount_text", "amount_mg", "category", "evidence"]


def _store(tmp_path: Path) -> CanonicalStore:
    return CanonicalStore(tmp_path / "store.json")


def _write_ledger(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=_LEDGER_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


@dataclass
class _Decision:
    canonical: str = ""
    decision_id: str = ""
    needs_review: bool = False
    review_reason: str = ""
    suggestion: str = ""
    method: str = "llm_match"


class _StubCanonicalizer:
    def __init__(self, mapping: dict[str, _Decision]) -> None:
        self.mapping = mapping

    def canonicalize(self, raw_name: str, *, context: str = "") -> _Decision:
        return self.mapping.get(raw_name, _Decision(needs_review=True, review_reason="uncertain"))


# -- staging ---------------------------------------------------------------------
def test_staged_writes_do_not_touch_the_base_store(tmp_path: Path) -> None:
    base = _store(tmp_path)
    ledger = RunDecisionLedger(base)

    ledger.stage_new(normalized_key="alpha", raw_name="Alpha", canonical="Alpha",
                     canonical_variant_key="alpha", confidence=0.9)

    assert "Alpha" not in base
    assert base.lookup("alpha") is None
    assert len(base) == 0


def test_reads_see_staged_writes_within_the_run(tmp_path: Path) -> None:
    ledger = RunDecisionLedger(_store(tmp_path))
    ledger.stage_new(normalized_key="alpha", raw_name="Alpha", canonical="Alpha",
                     canonical_variant_key="alpha", confidence=0.9,
                     disambiguation_class="alpha")

    assert ledger.lookup("alpha") == "Alpha"
    assert "Alpha" in ledger
    assert ledger.canonical_names() == ["Alpha"]
    assert ledger.disambiguation_class_of("Alpha") == "alpha"


def test_same_key_reuses_one_decision(tmp_path: Path) -> None:
    ledger = RunDecisionLedger(_store(tmp_path))
    first = ledger.stage_new(normalized_key="alpha", raw_name="Alpha", canonical="Alpha", confidence=0.9)
    second = ledger.stage_match(normalized_key="alpha", raw_name="Alpha", canonical="Alpha", confidence=0.9)

    # one alias, one rejectable identity — not one per row that mentions it
    assert first.decision_id == second.decision_id


def test_immediate_mode_writes_through(tmp_path: Path) -> None:
    base = _store(tmp_path)
    ledger = RunDecisionLedger(base, defer=False)
    ledger.stage_new(normalized_key="alpha", raw_name="Alpha", canonical="Alpha",
                     canonical_variant_key="alpha", confidence=0.9)

    assert base.lookup("alpha") == "Alpha"


# -- commit gating ---------------------------------------------------------------
def test_only_accepted_decisions_commit(tmp_path: Path) -> None:
    base = _store(tmp_path)
    base.add_canonical("Alpha", disambiguation_class="alpha")
    ledger = RunDecisionLedger(base)

    keep = ledger.stage_match(normalized_key="alpha extract", raw_name="Alpha Extract",
                              canonical="Alpha", confidence=0.9)
    drop = ledger.stage_match(normalized_key="beta blend", raw_name="Beta Blend",
                              canonical="Alpha", confidence=0.99)
    ledger.record_outcome(keep.decision_id, accepted=True, asin="A1")
    ledger.record_outcome(drop.decision_id, accepted=False, reason="value_collision", asin="A1")

    audit = ledger.commit()

    assert base.lookup("alpha extract") == "Alpha"
    assert base.lookup("beta blend") is None      # the offending alias only
    assert (audit.committed, audit.rejected) == (1, 1)


def test_decision_without_a_surviving_row_never_commits(tmp_path: Path) -> None:
    base = _store(tmp_path)
    base.add_canonical("Alpha", disambiguation_class="alpha")
    ledger = RunDecisionLedger(base)
    ledger.stage_match(normalized_key="orphan", raw_name="Orphan", canonical="Alpha", confidence=0.99)

    ledger.commit()

    # rejected-unless-proven-accepted: no outcome means no commit
    assert base.lookup("orphan") is None


def test_rejection_is_sticky(tmp_path: Path) -> None:
    ledger = RunDecisionLedger(_store(tmp_path))
    record = ledger.stage_new(normalized_key="alpha", raw_name="Alpha", canonical="Alpha", confidence=0.9)

    ledger.record_outcome(record.decision_id, accepted=False, reason="value_collision")
    ledger.record_outcome(record.decision_id, accepted=True)   # a later row must not revive it

    assert not ledger.resolve().is_accepted(record.decision_id)


def test_rejection_is_transitive(tmp_path: Path) -> None:
    base = _store(tmp_path)
    ledger = RunDecisionLedger(base)

    creator = ledger.stage_new(normalized_key="alpha", raw_name="Alpha", canonical="Alpha",
                               canonical_variant_key="alpha", confidence=0.9)
    dependent = ledger.stage_match(normalized_key="alpha extract", raw_name="Alpha Extract",
                                   canonical="Alpha", confidence=0.95)
    cache_user = ledger.stage_cache_hit("alpha", "Alpha", raw_name="Alpha")

    ledger.record_outcome(creator.decision_id, accepted=False, reason="identity_guard:kind_mismatch")
    ledger.record_outcome(dependent.decision_id, accepted=True)
    ledger.record_outcome(cache_user.decision_id, accepted=True)

    resolution = ledger.resolve()
    assert not resolution.is_accepted(dependent.decision_id)
    assert resolution.reason_for(dependent.decision_id) == f"dependency_rejected:{creator.decision_id}"

    ledger.commit(resolution)
    assert "Alpha" not in base
    assert base.lookup("alpha extract") is None


def test_commit_audit_rows_carry_outcome_and_reason(tmp_path: Path) -> None:
    base = _store(tmp_path)
    base.add_canonical("Alpha", disambiguation_class="alpha")
    ledger = RunDecisionLedger(base)
    record = ledger.stage_match(normalized_key="beta", raw_name="Beta", canonical="Alpha", confidence=0.8)
    ledger.record_outcome(record.decision_id, accepted=False, reason="value_collision", asin="A1")

    row = ledger.commit().rows[0]

    assert row["decision_id"] == record.decision_id
    assert row["normalized_key"] == "beta"
    assert row["canonical"] == "Alpha"
    assert row["outcome"] == "rejected"
    assert row["reason"] == "value_collision"
    assert row["asin"] == "A1"


# -- end to end through consolidation --------------------------------------------
def test_transitively_rejected_rows_leave_the_wide_output(tmp_path: Path) -> None:
    """The hole this phase exists to close.

    Dropping an alias from the proposed cache is not enough: rows that already used it
    must disappear from the rendered CSV too. Phase 3's identity guard is the realistic
    trigger for rejecting a creating decision; here it is recorded directly.
    """
    base = _store(tmp_path)
    ledger = RunDecisionLedger(base)
    creator = ledger.stage_new(normalized_key="mystery blend", raw_name="Mystery Blend",
                               canonical="Choline", canonical_variant_key="choline", confidence=0.99)
    dependent = ledger.stage_match(normalized_key="hydration matrix", raw_name="Hydration Matrix",
                                   canonical="Choline", confidence=0.98)
    ledger.record_outcome(creator.decision_id, accepted=False, reason="identity_guard:kind_mismatch")

    ledger_csv = tmp_path / "x_supplement_facts_rows.csv"
    _write_ledger(ledger_csv, [
        {"asin": "A1", "row_order": "1", "name": "Mystery Blend", "amount_text": "500 mg",
         "amount_mg": "500", "category": "ingredient", "evidence": "panel"},
        {"asin": "A2", "row_order": "1", "name": "Hydration Matrix", "amount_text": "250 mg",
         "amount_mg": "250", "category": "ingredient", "evidence": "panel"},
    ])
    stub = _StubCanonicalizer({
        "Mystery Blend": _Decision(canonical="Choline", decision_id=creator.decision_id, method="llm_new"),
        "Hydration Matrix": _Decision(canonical="Choline", decision_id=dependent.decision_id),
    })

    out, review, stats = consolidate_from_ledger(ledger_csv, canonicalizer=stub, decision_ledger=ledger)
    ledger.commit()

    assert stats["canonical_columns"] == 0                     # nothing rendered
    assert "Choline" not in _read(out)[0]
    assert stats["rows_dropped_by_rejection"] == 2
    rejected = [r for r in _read(review) if r["type"] == "rejected_decision"]
    assert {r["raw_name"] for r in rejected} == {"Mystery Blend", "Hydration Matrix"}
    assert "Choline" not in base                               # and nothing persisted
    assert base.lookup("hydration matrix") is None


def test_a_doomed_row_cannot_evict_a_legitimate_one(tmp_path: Path) -> None:
    """Settlement must precede validation.

    A rejected row that reached a cell first must not make the real row look like a
    value collision — otherwise both are dropped and the cell silently empties, which
    is the data loss this phase exists to prevent.
    """
    base = _store(tmp_path)
    base.add_canonical("Choline", disambiguation_class="choline")
    ledger = RunDecisionLedger(base)
    doomed = ledger.stage_match(normalized_key="hydration matrix", raw_name="Hydration Matrix",
                                canonical="Choline", confidence=0.99)
    genuine = ledger.stage_match(normalized_key="choline bitartrate", raw_name="Choline Bitartrate",
                                 canonical="Choline", confidence=0.95)
    ledger.record_outcome(doomed.decision_id, accepted=False, reason="identity_guard:kind_mismatch")

    ledger_csv = tmp_path / "x_supplement_facts_rows.csv"
    _write_ledger(ledger_csv, [
        {"asin": "A1", "row_order": "1", "name": "Hydration Matrix", "amount_text": "500 mg",
         "amount_mg": "500", "category": "ingredient", "evidence": "panel"},
        {"asin": "A1", "row_order": "2", "name": "Choline Bitartrate", "amount_text": "300 mg",
         "amount_mg": "300", "category": "ingredient", "evidence": "panel"},
    ])
    stub = _StubCanonicalizer({
        "Hydration Matrix": _Decision(canonical="Choline", decision_id=doomed.decision_id),
        "Choline Bitartrate": _Decision(canonical="Choline", decision_id=genuine.decision_id),
    })

    out, review, _ = consolidate_from_ledger(ledger_csv, canonicalizer=stub, decision_ledger=ledger)
    ledger.commit()

    assert _read(out)[0]["Choline"] == "300"                  # the real value survives
    assert [r["type"] for r in _read(review)] == ["rejected_decision"]
    assert base.lookup("choline bitartrate") == "Choline"     # and it still commits
    assert base.lookup("hydration matrix") is None


def test_value_collision_rejects_the_staged_alias(tmp_path: Path) -> None:
    base = _store(tmp_path)
    base.add_canonical("Vitamin D3", disambiguation_class="vitamin_d3")
    ledger = RunDecisionLedger(base)
    good = ledger.stage_cache_hit("vitamin d3", "Vitamin D3", raw_name="Vitamin D3")
    bad = ledger.stage_match(normalized_key="vitamin d", raw_name="Vitamin D",
                             canonical="Vitamin D3", confidence=0.99)

    ledger_csv = tmp_path / "x_supplement_facts_rows.csv"
    _write_ledger(ledger_csv, [
        {"asin": "A1", "row_order": "1", "name": "Vitamin D3", "amount_text": "",
         "amount_mg": "0.1", "category": "nutrient", "evidence": "panel"},
        {"asin": "A1", "row_order": "2", "name": "Vitamin D", "amount_text": "",
         "amount_mg": "0.125", "category": "nutrient", "evidence": "panel"},
    ])
    stub = _StubCanonicalizer({
        "Vitamin D3": _Decision(canonical="Vitamin D3", decision_id=good.decision_id, method="cache"),
        "Vitamin D": _Decision(canonical="Vitamin D3", decision_id=bad.decision_id),
    })

    out, review, _ = consolidate_from_ledger(ledger_csv, canonicalizer=stub, decision_ledger=ledger)
    ledger.commit()

    assert _read(out)[0]["Vitamin D3"] == "0.1"        # the first value survives
    assert [r["type"] for r in _read(review)] == ["value_collision"]
    assert base.lookup("vitamin d") is None            # the collided alias never persists
