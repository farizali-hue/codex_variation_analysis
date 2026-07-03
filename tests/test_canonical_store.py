"""Unit tests for the persistent canonical store (slice 1, no LLM)."""

from __future__ import annotations

from pathlib import Path

from amazon_scraper.canonical_store import CanonicalStore


def test_empty_store(tmp_path: Path) -> None:
    store = CanonicalStore.load(tmp_path / "store.json")
    assert len(store) == 0
    assert store.lookup("vitamin d3") is None
    assert store.canonical_names() == []


def test_add_canonical_and_variant_lookup(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "store.json")
    store.add_canonical("Vitamin D3", disambiguation_class="vitamin_d", variant_key="vitamin d3")
    store.add_variant("vitamin d", "Vitamin D3")

    assert store.lookup("vitamin d3") == "Vitamin D3"
    assert store.lookup("vitamin d") == "Vitamin D3"
    assert store.lookup("magnesium") is None
    assert "Vitamin D3" in store
    assert store.disambiguation_class_of("Vitamin D3") == "vitamin_d"
    assert store.canonical_names() == ["Vitamin D3"]


def test_add_variant_requires_existing_canonical(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "store.json")
    try:
        store.add_variant("vitamin d", "Vitamin D3")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected KeyError for unknown canonical")


def test_add_canonical_is_idempotent(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "store.json")
    store.add_canonical("Magnesium", variant_key="magnesium")
    store.add_canonical("Magnesium", variant_key="magnesium glycinate")
    assert store.canonical_names() == ["Magnesium"]
    assert store.lookup("magnesium glycinate") == "Magnesium"


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "store.json"
    store = CanonicalStore(path)
    store.add_canonical("Vitamin K2", disambiguation_class="vitamin_k", variant_key="vitamin k2")
    store.add_variant("vitamin k2 mk-7", "Vitamin K2")
    store.add_canonical("Magnesium", variant_key="magnesium")
    store.save()

    reloaded = CanonicalStore.load(path)
    assert reloaded.lookup("vitamin k2 mk-7") == "Vitamin K2"
    assert reloaded.lookup("magnesium") == "Magnesium"
    assert reloaded.disambiguation_class_of("Vitamin K2") == "vitamin_k"
    assert set(reloaded.canonical_names()) == {"Vitamin K2", "Magnesium"}


def test_repointing_a_variant_moves_it(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "store.json")
    store.add_canonical("Vitamin K1", variant_key="vitamin k")
    store.add_canonical("Vitamin K2")
    # correct an earlier wrong mapping
    store.add_variant("vitamin k", "Vitamin K2")
    assert store.lookup("vitamin k") == "Vitamin K2"
    # the old entry no longer claims that alias
    k1 = next(e for e in store.entries() if e.canonical_name == "Vitamin K1")
    assert "vitamin k" not in k1.aliases


def test_load_distrusts_unparseable_confidence(tmp_path: Path) -> None:
    # Engine-written entries always carry a numeric confidence; a missing or
    # garbage value means the file was hand-edited or corrupted. Distrust it
    # (0.0) instead of granting full trust (1.0) — AGENTS.md rule 3.
    path = tmp_path / "store.json"
    path.write_text(
        '{"version": 1, "canonicals": ['
        '{"canonical_name": "Zinc", "aliases": ["zinc"], "confidence": "garbage"},'
        '{"canonical_name": "Iron", "aliases": ["iron"]}'
        "]}",
        encoding="utf-8",
    )

    store = CanonicalStore.load(path)

    confidences = {entry.canonical_name: entry.confidence for entry in store.entries()}
    assert confidences == {"Zinc": 0.0, "Iron": 0.0}
