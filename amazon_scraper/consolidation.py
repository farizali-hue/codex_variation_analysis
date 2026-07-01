"""Silver-layer consolidation: turn a raw detail-mode scrape CSV into the
canonical, human-style "Consolidated" sheet.

The scraper emits one column per *literal* ingredient string (Bronze layer),
so the same ingredient shows up under many botanical / form-descriptor / word-
order variants. This module folds those variant columns into a small set of
canonical columns using a version-controlled dictionary
(``canonical_ingredients.json``), drops parse artifacts, normalizes a few
names, and derives the cheap columns (serving size count, capsules/container).

Anything it cannot confidently map is kept as its own column AND written to a
``*_needs_review.csv`` so a human can extend the dictionary — it never silently
mis-merges.

Entry point: :func:`consolidate_csv`.
"""

from __future__ import annotations

import csv
import difflib
import json
import logging
import re
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).with_name("canonical_ingredients.json")

# Raw meta columns that become the parsed/derived serving fields.
_RAW_SERVING_SIZE = "Serving Size"
_RAW_SERVINGS_PER_CONTAINER = "Servings Per Container"


def load_config(config_path: Path | str | None = None) -> dict:
    path = Path(config_path) if config_path else _CONFIG_PATH
    with path.open(encoding="utf-8") as file:
        return json.load(file)


class _Consolidator:
    def __init__(self, config: dict) -> None:
        self.cfg = config
        self.identity = set(config["identity_columns"])
        self.rename = dict(config["rename_columns"])
        self.nutrients = set(config["nutrient_passthrough"])
        self.drop = set(config["drop_columns"])
        self.sentinels = {s.strip().lower() for s in config["sentinel_values"]}
        self.canonical_map = dict(config["canonical_map"])
        self.output_order = list(config["output_order"])
        self.external = set(config["external_columns"])
        self.rules = [(re.compile(r["pattern"], re.IGNORECASE), r["replace"]) for r in config["normalization_rules"]]
        self.canonical_ingredients = set(self.canonical_map.values())

    # -- column name resolution -------------------------------------------------
    def _normalize_name(self, name: str) -> str:
        text = name
        for pattern, replace in self.rules:
            text = pattern.sub(replace, text)
        return text.strip()

    def resolve_column(self, name: str) -> tuple[str, str | None]:
        """Return (action, target). action in {drop, keep, merge, unknown}."""
        if name in self.drop:
            return ("drop", None)
        if name in self.identity:
            return ("keep", name)
        if name in self.rename:
            return ("keep", self.rename[name])
        if name in self.nutrients:
            return ("keep", name)
        if name in self.canonical_map:
            return ("merge", self.canonical_map[name])
        normalized = self._normalize_name(name)
        for canonical in self.canonical_ingredients:
            if normalized.lower() == canonical.lower():
                return ("merge", canonical)
        return ("unknown", name)

    def _suggest(self, name: str) -> str:
        candidates = difflib.get_close_matches(self._normalize_name(name), sorted(self.canonical_ingredients), n=1, cutoff=0.6)
        return candidates[0] if candidates else ""

    # -- value helpers ----------------------------------------------------------
    def _clean(self, value: str | None) -> str:
        if value is None:
            return ""
        text = value.strip()
        if text.lower() in self.sentinels:
            return ""
        return text

    @staticmethod
    def _leading_int(value: str) -> str:
        # Accept grouped thousands so "1,000" -> "1000" instead of truncating to "1".
        match = re.match(r"\s*(\d{1,3}(?:,\d{3})+|\d+)", value or "")
        return match.group(1).replace(",", "") if match else ""

    @staticmethod
    def _item_form(serving_size_raw: str) -> str:
        # "3 Softgels" -> "Softgel", "1 Veggie Softgel" -> "Softgel"
        words = re.sub(r"^\s*\d+\s*", "", serving_size_raw or "").split()
        if not words:
            return ""
        last = words[-1]
        return last[:-1] if last.lower().endswith("s") and len(last) > 3 else last

    def consolidate(self, rows: list[dict[str, str]], source_columns: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        # Map each canonical output column -> list of source columns feeding it.
        sources_by_target: dict[str, list[str]] = {}
        unknown_columns: list[str] = []
        for name in source_columns:
            action, target = self.resolve_column(name)
            if action in {"keep", "merge"} and target is not None:
                sources_by_target.setdefault(target, []).append(name)
            elif action == "unknown":
                unknown_columns.append(name)
                sources_by_target.setdefault(name, []).append(name)

        out_rows: list[dict[str, str]] = []
        review_rows: list[dict[str, str]] = []
        for row in rows:
            asin = row.get("asin", "")
            out: dict[str, str] = {col: "" for col in self.output_order}
            # extend output with any unknown columns not already in the schema
            for unknown in unknown_columns:
                out.setdefault(unknown, "")

            for target, names in sources_by_target.items():
                chosen = ""
                seen_values: list[tuple[str, str]] = []
                for name in names:
                    cleaned = self._clean(row.get(name))
                    if cleaned:
                        seen_values.append((name, cleaned))
                        if not chosen:
                            chosen = cleaned
                out[target] = chosen
                # collision: two different non-empty source values into one column
                distinct = {v for _, v in seen_values}
                if len(distinct) > 1:
                    review_rows.append({
                        "asin": asin, "type": "value_collision", "canonical_column": target,
                        "detail": " | ".join(f"{n}={v}" for n, v in seen_values), "suggestion": "",
                    })

            # derived columns
            serving_raw = row.get(_RAW_SERVING_SIZE, "")
            serving_count = self._leading_int(serving_raw)
            out["serving_size"] = serving_count or out.get("serving_size", "")
            if not out.get("item_form"):
                out["item_form"] = self._item_form(serving_raw)
            spc = self._leading_int(out.get("servings_per_container", ""))
            if serving_count and spc:
                out["number_of_capsules_per_container"] = str(int(serving_count) * int(spc))

            out_rows.append(out)

        # one review entry per unknown column (with a fuzzy suggestion)
        for unknown in unknown_columns:
            review_rows.append({
                "asin": "", "type": "unknown_ingredient", "canonical_column": unknown,
                "detail": "no dictionary entry; kept as its own column",
                "suggestion": self._suggest(unknown),
            })
        return out_rows, review_rows


def consolidate_csv(
    input_csv: Path | str,
    output_csv: Path | str | None = None,
    *,
    config_path: Path | str | None = None,
    review_csv: Path | str | None = None,
) -> Path:
    """Read a raw detail-mode scrape CSV and write the canonical consolidated CSV.

    Also writes a ``*_needs_review.csv`` listing unknown ingredients and value
    collisions. Returns the consolidated output path.
    """
    input_path = Path(input_csv)
    output_path = Path(output_csv) if output_csv else input_path.with_name(f"{input_path.stem}_consolidated.csv")
    review_path = Path(review_csv) if review_csv else output_path.with_name(f"{output_path.stem}_needs_review.csv")

    config = load_config(config_path)
    consolidator = _Consolidator(config)

    with input_path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        source_columns = list(reader.fieldnames or [])
        rows = list(reader)

    out_rows, review_rows = consolidator.consolidate(rows, source_columns)

    extra = [c for c in (out_rows[0].keys() if out_rows else []) if c not in consolidator.output_order]
    fieldnames = consolidator.output_order + extra
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    with review_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["asin", "type", "canonical_column", "detail", "suggestion"])
        writer.writeheader()
        writer.writerows(review_rows)

    LOGGER.info(
        "Consolidation complete rows=%d input_cols=%d output_cols=%d review_items=%d output=%s",
        len(out_rows), len(source_columns), len(fieldnames), len(review_rows), output_path,
    )
    return output_path


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) < 2:
        print("usage: python -m amazon_scraper.consolidation <input.csv> [output.csv]")
        raise SystemExit(2)
    result = consolidate_csv(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"wrote {result}")
