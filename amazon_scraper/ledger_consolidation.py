"""Consolidate the rich long-form ledger into canonical ingredient columns.

Reads `*_supplement_facts_rows.csv` (the lossless ledger, which keeps amount_text,
category, parent_blend, provenance — see csv_export._build_supplement_facts_rows_export),
canonicalizes each ingredient name via an injected canonicalizer, and pivots into a wide
table of canonical (base-nutrient) columns. Items the canonicalizer can't confidently place
go to a `*_needs_review.csv` instead of being guessed.

The canonicalizer is injected (real engine in the runner; a stub in tests), so this module
has no LLM dependency and is fully unit-testable.

Consolidation runs in four passes (ADR-004) so that validation outcomes are decided before
anything is rendered or persisted:

  A. resolve  -- canonicalize every row into a `_ResolvedRow`; reviews drop out here
  B. settle   -- drop rows whose decision did not survive, including transitively (a row
                 using a canonical whose creating decision was rejected goes too)
  C. validate -- detect value collisions per (asin, canonical) cell among the survivors,
                 feeding each round of rejections back through B until stable
  D. render   -- write the wide CSV from the surviving rows only

This module never mutates canonicalizer state; it only reports outcomes to the ledger.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LOGGER = logging.getLogger(__name__)

_DEFAULT_VALUE_FIELDS = ("amount_mg", "amount_text")
_REVIEW_FIELDS = ["asin", "type", "raw_name", "canonical_column", "reason", "value", "evidence", "decision_id"]

# A blend member carries no amount of its own -- the blend states one total for the
# group. Leaving the cell blank makes "in this product, amount undisclosed"
# indistinguishable from "not in this product at all", which loses the presence signal
# the comparison table exists to show. These rows get a marker instead.
_BLEND_MEMBER_MARKER = "amount not mentioned (part of blend)"
_BLEND_MEMBER_MARKER_NAMED = "amount not mentioned (part of {blend})"
_MARKER_PREFIX = "amount not mentioned"
_BLEND_MEMBER_CATEGORIES = {"blend_sub_ingredient"}


class Canonicalizer(Protocol):
    def canonicalize(self, raw_name: str, *, context: str = "") -> object: ...


@dataclass
class _ResolvedRow:
    """One ledger row that reached a canonical column, pending validation."""

    index: int
    asin: str
    raw_name: str
    canonical: str
    value: str
    evidence: str
    decision_id: str


def consolidate_from_ledger(
    rows_csv: Path | str,
    *,
    canonicalizer: Canonicalizer,
    output_csv: Path | str | None = None,
    review_csv: Path | str | None = None,
    value_fields: tuple[str, ...] = _DEFAULT_VALUE_FIELDS,
    decision_ledger=None,
) -> tuple[Path, Path, dict[str, int]]:
    """Return (output_path, review_path, stats).

    `decision_ledger` is the run's `RunDecisionLedger` when the caller wants staged
    writes gated on validation. When it is None the pivot behaves exactly as before —
    which is what the stub-canonicalizer tests exercise.
    """
    input_path = Path(rows_csv)
    output_path = Path(output_csv) if output_csv else _default_output_path(input_path)
    review_path = Path(review_csv) if review_csv else output_path.with_name(f"{output_path.stem}_needs_review.csv")

    with input_path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))

    asin_order: list[str] = []
    seen_asins: set[str] = set()
    review_rows: list[dict[str, str]] = []
    resolved: list[_ResolvedRow] = []
    llm_decisions = 0

    # -- pass A: resolve ---------------------------------------------------------
    for index, row in enumerate(rows):
        asin = (row.get("asin", "") or "").strip()
        name = (row.get("name", "") or "").strip()
        if not name:
            continue
        if asin not in seen_asins:
            seen_asins.add(asin)
            asin_order.append(asin)

        value = _first_value(row, value_fields) or _blend_member_marker(row)
        evidence = (row.get("evidence", "") or "").strip() or name
        decision = canonicalizer.canonicalize(name, context=evidence)
        if getattr(decision, "method", "") not in ("cache", ""):
            llm_decisions += 1
        decision_id = getattr(decision, "decision_id", "") or ""

        if getattr(decision, "needs_review", False):
            reason = getattr(decision, "review_reason", "") or ""
            review_rows.append({
                "asin": asin, "type": "unresolved_ingredient", "raw_name": name,
                "canonical_column": getattr(decision, "suggestion", "") or "",
                "reason": reason, "value": value, "evidence": evidence,
                "decision_id": decision_id,
            })
            _record(decision_ledger, decision_id, accepted=False, reason=reason or "needs_review", asin=asin)
            continue

        canonical = getattr(decision, "canonical", "") or ""
        if not canonical:
            continue
        resolved.append(_ResolvedRow(
            index=index, asin=asin, raw_name=name, canonical=canonical,
            value=value, evidence=evidence, decision_id=decision_id,
        ))

    # -- passes B/C: settle, then validate what is left, to a fixed point ---------
    # Order matters. Validating before settling would let a doomed row occupy a cell
    # and evict a legitimate row as a "collision", losing both. So rows whose decision
    # already failed are removed first, and each round of collision rejections is fed
    # back through resolution because a rejected decision can cascade via depends_on.
    survivors = list(resolved)
    dropped_by_rejection = 0
    for row_ref in survivors:
        _record(decision_ledger, row_ref.decision_id, accepted=True, asin=row_ref.asin)

    while True:
        if decision_ledger is not None:
            resolution = decision_ledger.resolve()
            settled: list[_ResolvedRow] = []
            for row_ref in survivors:
                if row_ref.decision_id and not resolution.is_accepted(row_ref.decision_id):
                    review_rows.append({
                        "asin": row_ref.asin, "type": "rejected_decision", "raw_name": row_ref.raw_name,
                        "canonical_column": row_ref.canonical,
                        "reason": resolution.reason_for(row_ref.decision_id) or "rejected",
                        "value": row_ref.value, "evidence": row_ref.evidence,
                        "decision_id": row_ref.decision_id,
                    })
                    dropped_by_rejection += 1
                    continue
                settled.append(row_ref)
            survivors = settled

        collisions = _detect_collisions(survivors)
        if not collisions:
            break
        collided: set[int] = set()
        for row_ref, existing in collisions:
            review_rows.append({
                "asin": row_ref.asin, "type": "value_collision", "raw_name": row_ref.raw_name,
                "canonical_column": row_ref.canonical, "reason": f"{existing} != {row_ref.value}",
                "value": row_ref.value, "evidence": row_ref.evidence, "decision_id": row_ref.decision_id,
            })
            collided.add(row_ref.index)
            _record(decision_ledger, row_ref.decision_id, accepted=False,
                    reason="value_collision", asin=row_ref.asin)
        survivors = [row_ref for row_ref in survivors if row_ref.index not in collided]
        if decision_ledger is None:
            break  # nothing to cascade through; one validation round is the whole job

    # -- pass D: render ----------------------------------------------------------
    columns, wide, _ = _place(survivors)

    _write_wide(output_path, asin_order, columns, wide)
    _write_review(review_path, review_rows)

    stats = {
        "asins": len(asin_order), "canonical_columns": len(columns),
        "review_items": len(review_rows), "llm_or_new_decisions": llm_decisions,
        "rows_dropped_by_rejection": dropped_by_rejection,
    }
    LOGGER.info("ledger consolidation done %s -> %s (%s)", input_path, output_path, stats)
    return output_path, review_path, stats


def _record(ledger, decision_id: str, *, accepted: bool, reason: str = "", asin: str = "") -> None:
    if ledger is None or not decision_id:
        return
    ledger.record_outcome(decision_id, accepted=accepted, reason=reason, asin=asin)


def _place(rows: list[_ResolvedRow]) -> tuple[list[str], dict[str, dict[str, str]], list[tuple[_ResolvedRow, str]]]:
    """Pivot rows into cells, first non-empty value per cell winning."""
    columns: list[str] = []
    wide: dict[str, dict[str, str]] = {}
    collisions: list[tuple[_ResolvedRow, str]] = []
    for row_ref in rows:
        cells = wide.setdefault(row_ref.asin, {})
        if row_ref.canonical not in columns:
            columns.append(row_ref.canonical)
        existing = cells.get(row_ref.canonical, "")
        if existing and row_ref.value and existing != row_ref.value:
            # A stated amount always beats a "part of blend" marker, and never
            # collides with one: the same nutrient can appear both inside a blend and
            # on its own line, and that is agreement, not a contradiction.
            if _is_marker(existing) and not _is_marker(row_ref.value):
                cells[row_ref.canonical] = row_ref.value
                continue
            if _is_marker(row_ref.value) and not _is_marker(existing):
                continue
            collisions.append((row_ref, existing))
            continue
        if row_ref.value and not existing:
            cells[row_ref.canonical] = row_ref.value
        else:
            cells.setdefault(row_ref.canonical, existing)
    return columns, wide, collisions


def _detect_collisions(rows: list[_ResolvedRow]) -> list[tuple[_ResolvedRow, str]]:
    return _place(rows)[2]


def _default_output_path(input_path: Path) -> Path:
    stem = input_path.stem.replace("_supplement_facts_rows", "")
    return input_path.with_name(f"{stem}_canonical.csv")


def _blend_member_marker(row: dict[str, str]) -> str:
    """Marker for a blend member that states no amount of its own, else ""."""
    category = (row.get("category", "") or "").strip().lower()
    parent = (row.get("parent_blend_name", "") or "").strip()
    if category not in _BLEND_MEMBER_CATEGORIES and not parent:
        return ""
    if parent:
        return _BLEND_MEMBER_MARKER_NAMED.format(blend=parent)
    return _BLEND_MEMBER_MARKER


def _is_marker(value: str) -> bool:
    return value.startswith(_MARKER_PREFIX)


def _first_value(row: dict[str, str], value_fields: tuple[str, ...]) -> str:
    for field_name in value_fields:
        value = (row.get(field_name, "") or "").strip()
        if value:
            return value
    return ""


def _write_wide(path: Path, asin_order: list[str], columns: list[str], wide: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["asin", *columns]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for asin in asin_order:
            record = {"asin": asin}
            cells = wide.get(asin, {})
            for column in columns:
                record[column] = cells.get(column, "")
            writer.writerow(record)


def _write_review(path: Path, review_rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=_REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(review_rows)
