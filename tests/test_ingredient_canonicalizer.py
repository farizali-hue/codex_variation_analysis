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

from amazon_scraper.canonical_decisions import RunDecisionLedger
from amazon_scraper.canonical_store import CanonicalStore
from amazon_scraper.ingredient_canonicalizer import (
    IngredientCanonicalizer,
    LLMVerdict,
    OpenAIIngredientLLM,
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


class _MalformedResponseClient:
    """OpenAI-client stand-in whose chat reply is not parseable JSON."""

    def create_chat_completion(self, system_prompt, user_prompt, **kwargs) -> str:
        return "Sorry, I cannot answer in JSON."


class _RecordingClient:
    """Captures the kwargs the adapter sends, so model routing can be asserted."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    def create_chat_completion(self, system_prompt, user_prompt, **kwargs) -> str:
        self.kwargs = kwargs
        return '{"decision":"uncertain","canonical_name":"","disambiguation_class":"","confidence":0.1}'


def test_adapter_sends_the_configured_canonicalization_model() -> None:
    # Ingredient matching may run on a stronger model than the batch chat model.
    client = _RecordingClient()
    llm = OpenAIIngredientLLM(client, model="gpt-5.4")

    llm.decide(raw_name="Choline", normalized_name="choline", candidates=[], context="")

    assert client.kwargs["model"] == "gpt-5.4"
    assert client.kwargs["field"] == "ingredient_canonicalization"


def test_openai_llm_adapter_returns_uncertain_on_malformed_json() -> None:
    # A garbage LLM reply must become an "uncertain" verdict (-> review queue),
    # not an exception that aborts the whole consolidation run mid-ledger.
    llm = OpenAIIngredientLLM(_MalformedResponseClient())

    verdict = llm.decide(
        raw_name="Vitamin D3",
        normalized_name="vitamin d3",
        candidates=[],
        context="",
    )

    assert verdict.decision == "uncertain"
    assert verdict.confidence == 0.0


# -- identity guard wiring (ADR-004 phase 3) -------------------------------------
def _guarded(store, verdicts, mode, adjudicator=None):
    llm = StubLLM(verdicts)
    return IngredientCanonicalizer(
        store=store, llm=llm, identity_guard_mode=mode, adjudicator=adjudicator,
    ), llm


def test_guard_blocks_a_merge_the_llm_approved_at_high_confidence(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Choline", disambiguation_class="choline")
    verdicts = {"electrolyte hydration matrix": LLMVerdict("match", "Choline", "choline", 0.99)}

    engine, _ = _guarded(store, verdicts, "apply")
    decision = engine.canonicalize("Electrolyte Hydration Matrix")

    assert decision.needs_review
    assert decision.review_reason == "identity_guard:kind_mismatch"
    assert store.lookup("electrolyte hydration matrix") is None


def test_guard_off_lets_the_same_merge_through(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Choline", disambiguation_class="choline")
    verdicts = {"electrolyte hydration matrix": LLMVerdict("match", "Choline", "choline", 0.99)}

    engine, _ = _guarded(store, verdicts, "off")
    assert engine.canonicalize("Electrolyte Hydration Matrix").canonical == "Choline"


def test_shadow_keeps_routing_but_refuses_to_persist(tmp_path: Path) -> None:
    base = CanonicalStore(tmp_path / "s.json")
    base.add_canonical("Choline", disambiguation_class="choline")
    ledger = RunDecisionLedger(base)
    verdicts = {"electrolyte hydration matrix": LLMVerdict("match", "Choline", "choline", 0.99)}
    engine, _ = _guarded(ledger, verdicts, "shadow")

    decision = engine.canonicalize("Electrolyte Hydration Matrix")
    ledger.record_outcome(decision.decision_id, accepted=True)
    ledger.commit()

    assert decision.canonical == "Choline"          # routing unchanged
    assert not decision.needs_review
    assert base.lookup("electrolyte hydration matrix") is None   # but never persisted


def test_guard_runs_on_the_cache_path(tmp_path: Path) -> None:
    # a legacy bad alias must not resolve for free forever
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Whey Powder", disambiguation_class="whey")
    store.add_variant("soy protein isolate", "Whey Powder")

    engine, llm = _guarded(store, {}, "apply")
    decision = engine.canonicalize("Soy Protein Isolate")

    assert decision.needs_review
    assert decision.review_reason == "identity_guard:source_qualifier_mismatch"
    assert llm.calls == []          # still no LLM call on the cache path


def test_adjudicator_can_clear_an_ambiguous_pair(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Vitamin B1", disambiguation_class="vitamin_b1")
    verdicts = {"thiamin b1": LLMVerdict("match", "Vitamin B1", "vitamin_b1", 0.99)}

    engine, _ = _guarded(store, verdicts, "apply", adjudicator=lambda **kw: True)
    assert engine.canonicalize("Thiamin (B1)").canonical == "Vitamin B1"

    engine2, _ = _guarded(store, verdicts, "apply", adjudicator=lambda **kw: False)
    assert engine2.canonicalize("Thiamin (B1)").needs_review


def test_without_an_adjudicator_ambiguous_pairs_go_to_review(tmp_path: Path) -> None:
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Folate", disambiguation_class="folate")
    verdicts = {"folic acid": LLMVerdict("match", "Folate", "folate", 0.98)}

    engine, _ = _guarded(store, verdicts, "apply")
    assert engine.canonicalize("Folic acid").needs_review


def test_new_is_guarded_against_its_proposed_name_not_the_nearest(tmp_path: Path) -> None:
    # A blend must still be able to create its OWN column next to a nutrient.
    store = CanonicalStore(tmp_path / "s.json")
    store.add_canonical("Choline", disambiguation_class="choline")
    verdicts = {"hydration matrix": LLMVerdict("new", "Hydration Matrix", "hydration_matrix", 0.9)}

    engine, _ = _guarded(store, verdicts, "apply")
    decision = engine.canonicalize("Hydration Matrix")

    assert decision.canonical == "Hydration Matrix"
    assert decision.is_new
