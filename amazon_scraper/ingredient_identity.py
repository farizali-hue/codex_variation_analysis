"""Deterministic identity guards for ingredient canonicalization (ADR-004, phase 3).

The LLM merges wrong things at high confidence — `soy protein isolate -> Whey Powder`
and `electrolyte hydration matrix -> Choline` both scored 0.98+, and this repo's own
runs produced `Folic acid -> Folate` (0.98) and `Other Omega-3 fatty acids ->
Omega-3 Fatty Acids` (0.93). Self-reported confidence carries no signal for this
failure mode, so a second, independent check is needed.

These are **category-general identity axes**, not a per-ingredient alias table
(AGENTS.md rule #5). Each rule asks one structural question — is this a mixture or a
single substance? do the two sides name different species? different stereochemistry?
— using closed token vocabularies that do not grow per product.

No LLM, no network, no I/O. Two entry points:

    classify(raw_name, normalized) -> IngredientIdentity
    check_merge(incoming, candidate) -> GuardVerdict

`check_merge` never returns "these are the same". It returns `allow` (found no reason
to object), `review` (a structural conflict a human must settle), or `adjudicate`
(surface forms too far apart to verify deterministically — ask a second, adversarial
LLM, and default to review if none is available). The bias is deliberate: a wrong
canonical merge is worse than an extra column (AGENTS.md rule #3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .run_scoped_canonicalization import _orthographic_normalize, _singularize_token

# -- closed, category-general axis vocabularies ----------------------------------
# A mixture is never a form of any single nutrient it contains.
_MIXTURE_TOKENS = frozenset({
    "blend", "matrix", "complex", "formula", "formulation", "proprietary",
    "mixture", "mix", "combination", "prep", "concentrate blend",
})
# Words that name a preparation, not an identity of their own.
_GENERIC_ONLY_TOKENS = frozenset({
    "extract", "powder", "oil", "juice", "concentrate", "isolate", "ratio",
    "standardized", "organic", "vegan", "natural", "raw", "pure", "fresh",
    "root", "leaf", "fruit", "seed", "bark", "stem", "flower", "floret", "stalk",
    "whole", "dried", "freeze", "part", "parts", "electrolyte", "electrolytes",
})
# NB "vitamin"/"mineral" are deliberately NOT generic-only: "Vitamin D" is an
# identity, just an incompletely specified one. Treating it as identity-free sent it
# to the wrong rule entirely.
# Biological / botanical source. Different source = different ingredient.
_SOURCE_TOKENS = frozenset({
    "whey", "soy", "casein", "pea", "rice", "hemp", "egg", "collagen", "gelatin",
    "algae", "algal", "fish", "krill", "flax", "chia", "bovine", "porcine",
    "marine", "plant", "dairy", "milk", "yeast",
})
# Residual/partition qualifiers: "Other Omega-3s" is the leftover after DHA and EPA,
# not the total. Folding it into the total double-counts. "Total" is NOT here --
# a total is the same quantity as the base name.
_RESIDUAL_TOKENS = frozenset({"other", "others", "remaining", "residual", "misc"})
# Stereochemistry prefixes. dl- is a racemic mix of d- and l-; not interchangeable.
_CHIRALITY_TOKENS = frozenset({"d", "l", "dl", "ld"})
# Salt / ester / chelate forms -- a naming relation, handled by the prompt, and NOT
# an identity conflict. Listed so the guards do not mistake them for source words.
_FORM_TOKENS = frozenset({
    "citrate", "oxide", "glycinate", "bisglycinate", "picolinate", "gluconate",
    "sulfate", "sulphate", "carbonate", "chloride", "malate", "chelate",
    "ascorbate", "bitartrate", "tartrate", "acetate", "palmitate", "phosphate",
    "hydrochloride", "mononitrate", "selenomethionine", "aspartate", "fumarate",
})

_BINOMIAL = re.compile(r"\b([a-z]{4,})\s+(officinalis|repens|pepo|dioica|sativa|vulgaris|"
                       r"indica|nigra|alba|major|minor|chinensis|americana|canadensis|"
                       r"haplocalyx|rangiferina|multiflorum|serrata|paniculata)\b")
# Identity markers: the "D3" in Vitamin D3, the "12" in Vitamin B-12, the "K2" in
# Vitamin K2. Short by construction so real words ("oil", "root") never qualify.
_IDENTITY_MARKER = re.compile(r"^(?:[a-z]{1,2}\d{0,2}|\d{1,2})$")
_MARKER_PARTS = re.compile(r"^([a-z]*)(\d*)$")
_RATIO = re.compile(r"^\d+$")

_MAX_SINGLE_TOKENS = 8          # beyond this a label is describing a list, not one thing
_NEAR_MISS_MIN, _NEAR_MISS_MAX = 1, 3


@dataclass(frozen=True)
class IngredientIdentity:
    raw_name: str
    normalized: str
    tokens: tuple[str, ...] = ()
    stem_tokens: tuple[str, ...] = ()      # tokens with identity markers removed
    identity_markers: tuple[str, ...] = ()  # d3, b12, k2, 7 ...
    kind: str = "unknown"                   # single | mixture | unknown
    chirality: str = ""                     # d | l | dl
    source_qualifiers: frozenset = field(default_factory=frozenset)
    residual_qualifiers: frozenset = field(default_factory=frozenset)
    binomials: frozenset = field(default_factory=frozenset)
    is_multi_ingredient: bool = False
    has_identity: bool = True               # False for "4:1 extract"-style fragments


@dataclass(frozen=True)
class GuardVerdict:
    verdict: str            # allow | review | adjudicate
    rule: str = ""
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.verdict != "allow"


def classify(raw_name: str, normalized: str = "") -> IngredientIdentity:
    """Describe one ingredient label along the identity axes.

    `raw_name` is used as well as the normalized key because normalization strips
    "(as ...)" clauses, and those sometimes carry the only identity marker present
    (e.g. "Vitamin D (as D2 Ergocalciferol)" normalizes to "vitamin d").
    """
    text = normalized.strip() if normalized.strip() else _orthographic_normalize(raw_name)
    raw_text = _orthographic_normalize(raw_name)
    tokens = tuple(t for t in text.split() if t)
    singular = tuple(_singularize_token(t) for t in tokens)
    token_set = set(tokens) | set(singular)

    markers, stem = _split_markers(tokens)

    chirality = ""
    if tokens and tokens[0] in _CHIRALITY_TOKENS:
        chirality = tokens[0]

    is_mixture = bool(token_set & _MIXTURE_TOKENS)
    multi = _looks_multi_ingredient(raw_text, tokens)

    meaningful = [
        t for t in singular
        if t not in _GENERIC_ONLY_TOKENS and not _RATIO.match(t) and t not in _CHIRALITY_TOKENS
    ]
    return IngredientIdentity(
        raw_name=raw_name,
        normalized=text,
        tokens=tokens,
        stem_tokens=stem,
        identity_markers=markers,
        kind="mixture" if is_mixture else ("single" if meaningful else "unknown"),
        chirality=chirality,
        source_qualifiers=frozenset(token_set & _SOURCE_TOKENS),
        residual_qualifiers=frozenset(token_set & _RESIDUAL_TOKENS),
        binomials=frozenset(m.group(2) for m in _BINOMIAL.finditer(raw_text)),
        is_multi_ingredient=multi,
        has_identity=bool(meaningful),
    )


def check_merge(incoming: IngredientIdentity, candidate: IngredientIdentity) -> GuardVerdict:
    """Decide whether `incoming` may be folded into `candidate`.

    Rules are ordered by how load-bearing they are; the first conflict wins so the
    review reason names the strongest objection.
    """
    for rule in (
        _rule_no_identity,
        _rule_kind_mismatch,
        _rule_multi_ingredient,
        _rule_residual_qualifier,
        _rule_chirality,
        _rule_source_qualifier,
        _rule_binomial,
        _rule_identity_marker,
        _rule_surface_distance,
    ):
        verdict = rule(incoming, candidate)
        if verdict is not None:
            return verdict
    return GuardVerdict("allow")


# -- rules -----------------------------------------------------------------------
def _rule_no_identity(incoming: IngredientIdentity, candidate: IngredientIdentity):
    if not incoming.has_identity:
        return GuardVerdict("review", "no_identity",
                            f"{incoming.normalized!r} reduces to generic/ratio words only")
    return None


def _rule_kind_mismatch(incoming: IngredientIdentity, candidate: IngredientIdentity):
    if "unknown" in (incoming.kind, candidate.kind):
        return None
    if incoming.kind != candidate.kind:
        return GuardVerdict("review", "kind_mismatch",
                            f"{incoming.kind} vs {candidate.kind}")
    return None


def _rule_multi_ingredient(incoming: IngredientIdentity, candidate: IngredientIdentity):
    if incoming.is_multi_ingredient and not candidate.is_multi_ingredient:
        return GuardVerdict("review", "multi_ingredient",
                            f"{len(incoming.tokens)} tokens; label lists several ingredients")
    if len(incoming.tokens) > _MAX_SINGLE_TOKENS + len(candidate.tokens):
        return GuardVerdict("review", "multi_ingredient",
                            f"{len(incoming.tokens)} tokens vs {len(candidate.tokens)}")
    return None


def _rule_residual_qualifier(incoming: IngredientIdentity, candidate: IngredientIdentity):
    if incoming.residual_qualifiers and not candidate.residual_qualifiers:
        return GuardVerdict("review", "residual_qualifier_mismatch",
                            f"{sorted(incoming.residual_qualifiers)} names a remainder, not the whole")
    return None


def _rule_chirality(incoming: IngredientIdentity, candidate: IngredientIdentity):
    left, right = incoming.chirality, candidate.chirality
    if left and right and left != right:
        return GuardVerdict("review", "chirality_conflict", f"{left}- vs {right}-")
    return None


def _rule_source_qualifier(incoming: IngredientIdentity, candidate: IngredientIdentity):
    left, right = incoming.source_qualifiers, candidate.source_qualifiers
    if left and right and not (left & right):
        return GuardVerdict("review", "source_qualifier_mismatch",
                            f"{sorted(left)} vs {sorted(right)}")
    return None


def _rule_binomial(incoming: IngredientIdentity, candidate: IngredientIdentity):
    left, right = incoming.binomials, candidate.binomials
    if left and right and not (left & right):
        return GuardVerdict("review", "binomial_mismatch", f"{sorted(left)} vs {sorted(right)}")
    return None


def _rule_identity_marker(incoming: IngredientIdentity, candidate: IngredientIdentity):
    """D2 vs D3, K1 vs K2 — only when the rest of the name matches."""
    if _singular_text(incoming.stem_tokens) != _singular_text(candidate.stem_tokens):
        return None
    left, right = set(incoming.identity_markers), set(candidate.identity_markers)
    if left == right:
        return None
    if not left or not right:
        return GuardVerdict("adjudicate", "identity_marker_missing",
                            f"{sorted(left) or 'none'} vs {sorted(right) or 'none'}")
    left_digits = {_marker_digits(m) for m in left}
    right_digits = {_marker_digits(m) for m in right}
    if left_digits == {""} or right_digits == {""}:
        # "Vitamin D" vs "Vitamin D3": same letter, one unnumbered. The raw label may
        # settle it via an "(as ...)" clause the key dropped, so ask, do not guess.
        if {_marker_alpha(m) for m in left} == {_marker_alpha(m) for m in right}:
            return GuardVerdict("adjudicate", "identity_marker_missing",
                                f"{sorted(left)} vs {sorted(right)}")
    return GuardVerdict("review", "identity_marker_conflict",
                        f"{sorted(left)} vs {sorted(right)}")


def _rule_surface_distance(incoming: IngredientIdentity, candidate: IngredientIdentity):
    """Names too far apart to verify structurally — e.g. folic acid vs folate."""
    left, right = _singular_text(incoming.tokens), _singular_text(candidate.tokens)
    if not left or not right or left == right:
        return None
    if left in right or right in left:
        return None                      # containment: a qualified form of the same name
    distance = _edit_distance(left, right)
    if _NEAR_MISS_MIN <= distance <= _NEAR_MISS_MAX:
        return GuardVerdict("adjudicate", "near_miss_molecule", f"edit distance {distance}")
    if not (set(incoming.tokens) & set(candidate.tokens)):
        return GuardVerdict("adjudicate", "unrelated_surface", f"{left!r} vs {right!r}")
    return None


# -- helpers ---------------------------------------------------------------------
def _split_markers(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Separate identity markers from the stem, joining split ones.

    "vitamin b 12" and "vitamin b12" must yield the same marker, or every
    hyphenated label would look like a different substance from its own base name.
    """
    markers: list[str] = []
    stem: list[str] = []
    pending: list[str] = []
    for token in tokens:
        if _IDENTITY_MARKER.match(token):
            pending.append(token)
            continue
        if pending:
            markers.append("".join(pending))
            pending = []
        stem.append(token)
    if pending:
        markers.append("".join(pending))
    return tuple(markers), tuple(stem)


def _marker_digits(marker: str) -> str:
    match = _MARKER_PARTS.match(marker)
    return match.group(2) if match else ""


def _marker_alpha(marker: str) -> str:
    match = _MARKER_PARTS.match(marker)
    return match.group(1) if match else marker


def _singular_text(tokens: tuple[str, ...]) -> str:
    return " ".join(_singularize_token(t) for t in tokens)


def _looks_multi_ingredient(raw_text: str, tokens: tuple[str, ...]) -> bool:
    if len(_BINOMIAL.findall(raw_text)) >= 2:
        return True
    return len(tokens) > _MAX_SINGLE_TOKENS * 2


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]
