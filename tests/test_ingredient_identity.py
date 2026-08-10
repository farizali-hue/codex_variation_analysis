"""Identity guard tests — no API calls.

The must-block cases come from the corrupted live store and from this repo's own
runs. The must-allow cases come from merges those same runs made correctly: any
rule that vetoes one of them is too strict and would cost real data.
"""

from __future__ import annotations

import pytest

from amazon_scraper.ingredient_canonicalizer import default_normalize
from amazon_scraper.ingredient_identity import check_merge, classify


def _verdict(raw_incoming: str, raw_candidate: str):
    return check_merge(
        classify(raw_incoming, default_normalize(raw_incoming)),
        classify(raw_candidate, default_normalize(raw_candidate)),
    )


# -- must block (or at least question) -------------------------------------------
@pytest.mark.parametrize("incoming,candidate,rule", [
    # blends swallowed by single nutrients -- the live store's worst corruption
    ("Electrolyte Hydration Matrix", "Choline", "kind_mismatch"),
    ("Full Spectrum EAA BCAA Matrix", "Choline", "kind_mismatch"),
    ("Vivonu DHT Blocker Proprietary Blend", "Pumpkin Seed Oil", "kind_mismatch"),
    ("Food Blend", "L-Theanine", "kind_mismatch"),
    # the inverse: a single enzyme is not the complex
    ("Amylase", "Enzyme Complex", "kind_mismatch"),
    # different biological source
    ("Soy Protein Isolate", "Whey Powder", "source_qualifier_mismatch"),
    # stereochemistry
    ("DL-Methionine", "L-Methionine", "chirality_conflict"),
    ("DL-Phenylalanine", "L-Phenylalanine", "chirality_conflict"),
    # different species
    ("Organic Rosemary (Rosmarinus officinalis) leaf 30:1 extract", "Saw Palmetto (Serenoa repens)",
     "binomial_mismatch"),
    # fragments with no identity of their own
    ("4:1 Extract", "Saw Palmetto Extract", "no_identity"),
    ("Electrolytes", "Magnesium", "no_identity"),
    # numbered identities
    ("Vitamin D2", "Vitamin D3", "identity_marker_conflict"),
    ("Vitamin K1", "Vitamin K2", "identity_marker_conflict"),
    # found in this repo's own 27-product run, at 0.93 confidence
    ("Other Omega-3 fatty acids", "Omega-3 Fatty Acids", "residual_qualifier_mismatch"),
])
def test_incompatible_merges_are_blocked(incoming, candidate, rule) -> None:
    verdict = _verdict(incoming, candidate)
    assert verdict.blocks, f"{incoming!r} -> {candidate!r} was allowed"
    assert verdict.rule == rule


@pytest.mark.parametrize("incoming,candidate", [
    ("Folic acid", "Folate"),          # 0.98 in the real run; different molecules
    ("L-Cystine", "L-Cysteine"),       # one letter apart, not the same compound
    ("Vitamin D", "Vitamin D3"),       # the "(as ...)" clause is stripped from the key
])
def test_ambiguous_pairs_are_sent_for_adjudication(incoming, candidate) -> None:
    verdict = _verdict(incoming, candidate)
    assert verdict.verdict == "adjudicate", f"{incoming!r} -> {candidate!r}: {verdict}"


def test_a_long_concatenated_ingredient_list_is_blocked() -> None:
    listing = ("beta glucans lycopene nettle root pygeum bark saw palmetto berry "
               "pumpkin seed zinc selenium copper boron vitamin e")
    assert _verdict(listing, "Beta-glucans").blocks


# -- must allow: real merges from the 9- and 27-product runs ----------------------
@pytest.mark.parametrize("incoming,candidate", [
    ("Thiamin (Vitamin B1)", "Vitamin B1"),
    ("Thiamin (vitamin B1 as thiamin mononitrate)", "Vitamin B1"),
    ("Riboflavin (Vitamin B-2)", "Vitamin B2"),
    ("Niacin (vitamin B3 as inositol niacinate)", "Vitamin B3"),
    ("Vitamin B-12 (as Methylcobalamin)", "Vitamin B12"),
    ("Vitamin C (Ascorbic acid)", "Vitamin C"),
    ("Vitamin D3 (as fermented cholecalciferol)", "Vitamin D3"),
    ("Choline (as Choline L(+) Bitartrate) (VitaCholine)", "Choline"),
    ("Iron (as Ferrous Bisglycinate Chelate) (Ferrochel)", "Iron"),
    ("Zinc (as Zinc Bisglycinate Chelate) (Albion)", "Zinc"),
    ("Magnesium Glycinate", "Magnesium"),
    ("Organic Broccoli (floret, stem)", "Broccoli"),
    ("Organic Red Cabbage (leaf)", "Cabbage"),
    ("Total Omega-3s", "Omega-3 Fatty Acids"),       # a total IS the whole
    ("Total Omega-3 Fatty Acids", "Omega-3 Fatty Acids"),
    ("Omega-3 Fatty Acids (from Fish Oil Concentrate)", "Omega-3 Fatty Acids"),
    ("Taurine", "L-Taurine"),                        # one side unprefixed is not a conflict
    ("Peppermint (Mentha haplocalyx) leaf 4:1 extract", "Peppermint"),
    ("Pumpkin Seed Oil (Cucurbita pepo)", "Pumpkin Seed Oil"),
])
def test_valid_merges_are_not_vetoed(incoming, candidate) -> None:
    verdict = _verdict(incoming, candidate)
    assert verdict.verdict != "review", f"{incoming!r} -> {candidate!r} vetoed by {verdict.rule}"


# -- classification ---------------------------------------------------------------
def test_mixture_words_make_a_mixture() -> None:
    for name in ["Proprietary Blend", "Hydration Matrix", "Enzyme Complex", "Immune Formula"]:
        assert classify(name, default_normalize(name)).kind == "mixture"


def test_a_plain_nutrient_is_single() -> None:
    assert classify("Magnesium", "magnesium").kind == "single"


def test_chirality_is_read_from_the_leading_token() -> None:
    # _build_lexical_signature drops 1-char tokens, so this must not use it
    assert classify("L-Methionine", default_normalize("L-Methionine")).chirality == "l"
    assert classify("DL-Methionine", default_normalize("DL-Methionine")).chirality == "dl"


def test_identity_markers_are_separated_from_the_stem() -> None:
    identity = classify("Vitamin D3", "vitamin d3")
    assert identity.identity_markers == ("d3",)
    assert identity.stem_tokens == ("vitamin",)
