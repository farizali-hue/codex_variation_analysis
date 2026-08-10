"""Unit tests for ledger -> canonical-columns pivot (slice 3, stub canonicalizer)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from amazon_scraper.ledger_consolidation import consolidate_from_ledger

_LEDGER_HEADER = ["asin", "row_order", "name", "amount_text", "amount_mg", "category", "evidence"]


@dataclass
class _Decision:
    canonical: str = ""
    needs_review: bool = False
    review_reason: str = ""
    suggestion: str = ""
    method: str = "llm_match"


class StubCanonicalizer:
    """Maps raw ingredient name -> canned decision."""

    def __init__(self, mapping: dict[str, _Decision]) -> None:
        self.mapping = mapping

    def canonicalize(self, raw_name: str, *, context: str = "") -> _Decision:
        return self.mapping.get(raw_name, _Decision(needs_review=True, review_reason="uncertain"))


def _write_ledger(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=_LEDGER_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def test_pivot_collapses_variants_to_canonical_columns(tmp_path: Path) -> None:
    ledger = tmp_path / "x_supplement_facts_rows.csv"
    _write_ledger(ledger, [
        {"asin": "A1", "row_order": "1", "name": "Pumpkin Seed Oil (Cucurbita pepo)",
         "amount_text": "3,000 mg", "amount_mg": "3000", "category": "ingredient", "evidence": "..."},
        {"asin": "A1", "row_order": "2", "name": "Saw Palmetto Extract (Serenoa repens)",
         "amount_text": "75 mg", "amount_mg": "75", "category": "ingredient", "evidence": "..."},
        # different surface form of the SAME ingredient in another product
        {"asin": "A2", "row_order": "1", "name": "Pumpkin Seed Oil",
         "amount_text": "3,000 mg", "amount_mg": "3000", "category": "ingredient", "evidence": "..."},
    ])
    stub = StubCanonicalizer({
        "Pumpkin Seed Oil (Cucurbita pepo)": _Decision(canonical="Pumpkin Seed Oil"),
        "Pumpkin Seed Oil": _Decision(canonical="Pumpkin Seed Oil", method="cache"),
        "Saw Palmetto Extract (Serenoa repens)": _Decision(canonical="Saw Palmetto"),
    })

    out, review, stats = consolidate_from_ledger(ledger, canonicalizer=stub)
    rows = {r["asin"]: r for r in _read(out)}

    assert stats["canonical_columns"] == 2
    assert rows["A1"]["Pumpkin Seed Oil"] == "3000"
    assert rows["A1"]["Saw Palmetto"] == "75"
    assert rows["A2"]["Pumpkin Seed Oil"] == "3000"   # collapsed despite different wording
    assert _read(review) == []


def test_unresolved_goes_to_review_not_output(tmp_path: Path) -> None:
    ledger = tmp_path / "x_supplement_facts_rows.csv"
    _write_ledger(ledger, [
        {"asin": "A1", "row_order": "1", "name": "Mystery Botanical X",
         "amount_text": "50 mg", "amount_mg": "50", "category": "ingredient", "evidence": "panel text"},
    ])
    stub = StubCanonicalizer({})  # everything unknown -> review

    out, review, stats = consolidate_from_ledger(ledger, canonicalizer=stub)
    assert stats["canonical_columns"] == 0
    review_rows = _read(review)
    assert len(review_rows) == 1
    assert review_rows[0]["raw_name"] == "Mystery Botanical X"
    assert review_rows[0]["value"] == "50"


def test_value_collision_is_flagged(tmp_path: Path) -> None:
    ledger = tmp_path / "x_supplement_facts_rows.csv"
    _write_ledger(ledger, [
        {"asin": "A1", "row_order": "1", "name": "Vitamin D",
         "amount_text": "", "amount_mg": "0.125", "category": "nutrient", "evidence": "..."},
        {"asin": "A1", "row_order": "2", "name": "Vitamin D3",
         "amount_text": "", "amount_mg": "0.1", "category": "nutrient", "evidence": "..."},
    ])
    stub = StubCanonicalizer({
        "Vitamin D": _Decision(canonical="Vitamin D3"),
        "Vitamin D3": _Decision(canonical="Vitamin D3"),
    })
    out, review, stats = consolidate_from_ledger(ledger, canonicalizer=stub)
    collisions = [r for r in _read(review) if r["type"] == "value_collision"]
    assert len(collisions) == 1
    assert collisions[0]["canonical_column"] == "Vitamin D3"


def test_blend_members_show_presence_instead_of_a_blank_cell(tmp_path: Path) -> None:
    """A blank cell cannot distinguish "no amount stated" from "not in this product"."""
    ledger = tmp_path / "x_supplement_facts_rows.csv"
    header = [*_LEDGER_HEADER, "parent_blend_name"]
    with ledger.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=header)
        writer.writeheader()
        writer.writerows([
            {"asin": "A1", "row_order": "1", "name": "Food Blend", "amount_text": "330 mg",
             "amount_mg": "330", "category": "proprietary_blend", "evidence": "panel",
             "parent_blend_name": ""},
            {"asin": "A1", "row_order": "2", "name": "kale", "amount_text": "", "amount_mg": "",
             "category": "blend_sub_ingredient", "evidence": "panel", "parent_blend_name": "Food Blend"},
        ])
    stub = StubCanonicalizer({
        "Food Blend": _Decision(canonical="Food Blend"),
        "kale": _Decision(canonical="Kale"),
    })

    out, review, _ = consolidate_from_ledger(ledger, canonicalizer=stub)
    row = _read(out)[0]

    assert row["Food Blend"] == "330"
    assert row["Kale"] == "amount not mentioned (part of Food Blend)"
    assert _read(review) == []


def test_a_stated_amount_beats_a_blend_marker_without_colliding(tmp_path: Path) -> None:
    ledger = tmp_path / "x_supplement_facts_rows.csv"
    header = [*_LEDGER_HEADER, "parent_blend_name"]
    with ledger.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=header)
        writer.writeheader()
        writer.writerows([
            # blend member first, then the same nutrient on its own line with an amount
            {"asin": "A1", "row_order": "1", "name": "zinc", "amount_text": "", "amount_mg": "",
             "category": "blend_sub_ingredient", "evidence": "panel", "parent_blend_name": "Mineral Blend"},
            {"asin": "A1", "row_order": "2", "name": "Zinc", "amount_text": "25 mg", "amount_mg": "25",
             "category": "nutrient", "evidence": "panel", "parent_blend_name": ""},
        ])
    stub = StubCanonicalizer({
        "zinc": _Decision(canonical="Zinc"),
        "Zinc": _Decision(canonical="Zinc"),
    })

    out, review, _ = consolidate_from_ledger(ledger, canonicalizer=stub)

    assert _read(out)[0]["Zinc"] == "25"        # the real number wins
    assert _read(review) == []                  # and it is not a collision
