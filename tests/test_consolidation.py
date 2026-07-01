"""Guard tests for the Silver-layer consolidation transform.

Uses a small synthetic raw CSV that exercises the behaviors that matter:
- many variant ingredient columns collapse into one canonical column
- parse-artifact / blend-member columns are dropped
- sentinel values are blanked
- meta columns are renamed and serving fields are derived
- an unknown ingredient is kept AND flagged for review (never silently merged)
"""

from __future__ import annotations

import csv
from pathlib import Path

from amazon_scraper.consolidation import consolidate_csv

# variant columns for the same ingredients, plus a drop artifact, a sentinel,
# and one ingredient that is NOT in the dictionary.
_RAW_HEADER = [
    "asin", "product_type", "status", "title", "packaging_type",
    "Serving Size", "Servings Per Container",
    "Pumpkin Seed Oil (Cucurbita pepo)", "Pumpkin Seed Oil",
    "Saw Palmetto Extract (Serenoa repens) (fruit)", "Saw Palmetto Extract",
    "Vitamin C (as ascorbic acid)", "Typical Fatty Acid Profile",
    "Linoleic Acid (Omega-6)", "Ashwagandha Root Extract (Made-up Variant)",
]


def _write_raw(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=_RAW_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def test_consolidation_collapses_and_cleans(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    _write_raw(raw, [
        {  # ingredient values arrive under the botanical variant column
            "asin": "A1", "product_type": "x", "status": "OK", "title": "t1",
            "packaging_type": "jar", "Serving Size": "3 Softgels", "Servings Per Container": "100",
            "Pumpkin Seed Oil (Cucurbita pepo)": "3000", "Pumpkin Seed Oil": "",
            "Saw Palmetto Extract (Serenoa repens) (fruit)": "75", "Saw Palmetto Extract": "",
            "Vitamin C (as ascorbic acid)": "500", "Typical Fatty Acid Profile": "junk",
            "Linoleic Acid (Omega-6)": "1200", "Ashwagandha Root Extract (Made-up Variant)": "",
        },
        {  # same ingredients arrive under the plain variant column + a sentinel
            "asin": "A2", "product_type": "x", "status": "OK", "title": "t2",
            "packaging_type": "bottle", "Serving Size": "2 Softgels", "Servings Per Container": "50",
            "Pumpkin Seed Oil (Cucurbita pepo)": "", "Pumpkin Seed Oil": "grammage not mentioned",
            "Saw Palmetto Extract (Serenoa repens) (fruit)": "", "Saw Palmetto Extract": "80",
            "Vitamin C (as ascorbic acid)": "", "Typical Fatty Acid Profile": "junk",
            "Linoleic Acid (Omega-6)": "", "Ashwagandha Root Extract (Made-up Variant)": "999",
        },
    ])

    out = consolidate_csv(raw)
    rows = {r["asin"]: r for r in _read(out)}
    review = _read(out.with_name(f"{out.stem}_needs_review.csv"))

    a1, a2 = rows["A1"], rows["A2"]

    # variants collapse into one canonical column, value coalesced
    assert a1["Pumpkin Seed Oil"] == "3000"
    assert a2["Pumpkin Seed Oil"] == ""           # sentinel was blanked
    assert a1["Saw Palmetto"] == "75"
    assert a2["Saw Palmetto"] == "80"
    assert a1["Vitamin C"] == "500"
    assert a1["Omega-6 (Linoleic Acid)"] == "1200"

    # parse artifact dropped, operational columns dropped
    assert "Typical Fatty Acid Profile" not in a1
    assert "status" not in a1 and "product_type" not in a1

    # meta rename + derived fields (3 softgels x 100 servings = 300 capsules)
    assert a1["serving_size"] == "3"
    assert a1["item_form"] == "Softgel"
    assert a1["number_of_capsules_per_container"] == "300"

    # unknown ingredient is kept as its own column AND flagged (never silent)
    assert a2["Ashwagandha Root Extract (Made-up Variant)"] == "999"
    unknowns = [r for r in review if r["type"] == "unknown_ingredient"]
    assert any("Ashwagandha" in r["canonical_column"] for r in unknowns)


def test_leading_int_preserves_thousands_in_servings_per_container(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    _write_raw(raw, [
        {
            "asin": "A1", "product_type": "x", "status": "OK", "title": "t1",
            "Serving Size": "3 Softgels", "Servings Per Container": "1,000",
        },
    ])

    out = consolidate_csv(raw)
    rows = {r["asin"]: r for r in _read(out)}

    # 3 softgels x 1,000 servings = 3000 capsules; the comma must not truncate
    # servings-per-container to 1 (which would yield "3").
    assert rows["A1"]["number_of_capsules_per_container"] == "3000"


def test_value_collision_is_flagged(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    _write_raw(raw, [{
        "asin": "C1", "product_type": "x", "status": "OK", "title": "t",
        "packaging_type": "jar", "Serving Size": "3 Softgels", "Servings Per Container": "100",
        # two columns that map to "Pumpkin Seed Oil" with DIFFERENT values
        "Pumpkin Seed Oil (Cucurbita pepo)": "3000", "Pumpkin Seed Oil": "2000",
        "Saw Palmetto Extract (Serenoa repens) (fruit)": "", "Saw Palmetto Extract": "",
        "Vitamin C (as ascorbic acid)": "", "Typical Fatty Acid Profile": "",
        "Linoleic Acid (Omega-6)": "", "Ashwagandha Root Extract (Made-up Variant)": "",
    }])
    out = consolidate_csv(raw)
    review = _read(out.with_name(f"{out.stem}_needs_review.csv"))
    collisions = [r for r in review if r["type"] == "value_collision"]
    assert any(r["canonical_column"] == "Pumpkin Seed Oil" for r in collisions)
