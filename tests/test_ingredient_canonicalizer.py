"""Unit tests for the canonicalizer engine (slice 2) with a deterministic stub LLM.

Proves the orchestration + guardrails without any API calls:
- cache hit takes no LLM call
- an accepted LLM match is written back (next lookup is a cache hit)
- a new ingredient is added to the store
- combine-to-base form policy (Magnesium Glycinate -> Magnesium)
- disambiguation guard blocks an over-merge (D2 must not become D3)
- low confidence / uncertain -> review queue, store untouched
"""

from __future__ import annotations

from pathlib import Path

from amazon_scraper.canonical_store import CanonicalStore
from amazon_scraper.ingredient_canonicalizer import (
    IngredientCanonicalizer,
    LLMVerdict,
    default_normalize,
)


class StubLLM:
    """Returns canned verdicts keyed by normalized name; records calls."""

    def __init__(self, verdicts: dict[str, LLMVerdict]) -> None:
        self.verdicts = verdicts
        self.calls: list[str] = []

    def decide(self, *, raw_name, normalized_name, candidates, context) -> LLMVerdict:
        self.calls.append(normalized_name)
        return self.verdicts.get(normalized_name, LLMVerdict(decision="uncertain"))


def _engine(store: CanonicalStore, verdicts: dict[str, LLMVerdict], **kw) -> tuple[IngredientCanonicalizer, StubLLM]:
    llm = StubLLM(verdicts)
    return IngredientCanonicalizer(store=store, llm=llm, **kw), llm


def test_cache_hit_skips_llm(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Vitamin D3", disambiguation_class="vitamin_d3", variant_key="vitamin d3")
    engine, llm = _engine(store, {})

    decision = engine.canonicalize("Vitamin D3")
    assert decision.canonical == "Vitamin D3"
    assert decision.method == "cache"
    assert llm.calls == []  # no LLM consulted


def test_llm_match_is_written_back(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Vitamin D3", disambiguation_class="vitamin_d3")  # no variant seeded
    engine, llm = _engine(store, {
        "vitamin d3": LLMVerdict("match", "Vitamin D3", "vitamin_d3", 0.96),
    })

    first = engine.canonicalize("Vitamin D3 (as cholecalciferol)")  # normalizes to "vitamin d3"
    assert first.canonical == "Vitamin D3"
    assert first.method == "llm_match"
    assert llm.calls == ["vitamin d3"]

    # second time: served from the store, no new LLM call
    second = engine.canonicalize("Vitamin D3 (as Cholecalciferol)")
    assert second.method == "cache"
    assert llm.calls == ["vitamin d3"]


def test_new_ingredient_is_added(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    engine, _ = _engine(store, {
        "sea buckthorn oil": LLMVerdict("new", "Sea Buckthorn Oil", "sea_buckthorn_oil", 0.95),
    })
    decision = engine.canonicalize("Sea Buckthorn Oil")
    assert decision.canonical == "Sea Buckthorn Oil"
    assert decision.is_new and decision.method == "llm_new"
    assert "Sea Buckthorn Oil" in store
    assert store.lookup("sea buckthorn oil") == "Sea Buckthorn Oil"


def test_combine_form_to_base(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Magnesium", disambiguation_class="magnesium")
    engine, _ = _engine(store, {
        "magnesium glycinate": LLMVerdict("match", "Magnesium", "magnesium", 0.9),
    })
    decision = engine.canonicalize("Magnesium Glycinate")
    assert decision.canonical == "Magnesium"  # form folded into the base nutrient
    assert store.lookup("magnesium glycinate") == "Magnesium"


def test_disambiguation_guard_blocks_overmerge(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Vitamin D3", disambiguation_class="vitamin_d3")
    # a buggy/over-eager LLM tries to merge D2 into D3
    engine, _ = _engine(store, {
        "vitamin d2": LLMVerdict("match", "Vitamin D3", "vitamin_d2", 0.9),
    })
    decision = engine.canonicalize("Vitamin D2 (as ergocalciferol)")
    assert decision.needs_review
    assert decision.review_reason == "disambiguation_conflict"
    assert decision.canonical == ""  # NOT merged
    assert store.lookup("vitamin d2") is None  # store untouched


def test_low_confidence_and_uncertain_go_to_review(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Magnesium", disambiguation_class="magnesium")
    engine, _ = _engine(store, {
        "magnesium malate": LLMVerdict("match", "Magnesium", "magnesium", 0.5),  # below threshold
        "vitamin k": LLMVerdict("uncertain", "", "", 0.4),
    })

    low = engine.canonicalize("Magnesium Malate")
    assert low.needs_review and low.review_reason == "low_confidence"
    assert store.lookup("magnesium malate") is None

    vague = engine.canonicalize("Vitamin K")
    assert vague.needs_review and vague.review_reason == "uncertain"


def test_default_normalize_examples() -> None:
    assert default_normalize("Vitamin D3 (as cholecalciferol)") == "vitamin d3"
    assert default_normalize("Vitamin K2 (MK-7)") == "vitamin k2"
    assert default_normalize("Magnesium Glycinate") == "magnesium glycinate"
