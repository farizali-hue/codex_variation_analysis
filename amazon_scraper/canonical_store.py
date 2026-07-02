"""Persistent master vocabulary for ingredient canonicalization (the "memory").

This is the self-populating store that lets the canonicalizer resolve a previously
seen ingredient with zero LLM calls. It maps already-normalized variant keys to a
canonical ingredient name, and records a disambiguation class per canonical so the
engine can enforce guards like "Vitamin D2 must never merge into Vitamin D3".

The store is intentionally normalization-agnostic: callers normalize an ingredient
string first (see `ingredient_canonicalizer`) and pass the normalized key here. That
keeps this module a plain, well-tested data structure with no LLM/embedding deps.

JSON schema on disk (version 1):
    {
      "version": 1,
      "canonicals": [
        {"canonical_name": "Vitamin D3", "aliases": ["vitamin d3", "vitamin d"],
         "disambiguation_class": "vitamin_d", "created_by": "llm", "confidence": 1.0}
      ]
    }

The fast `variant_index` (normalized variant -> canonical name) is derived from the
aliases on load, not stored separately, to avoid drift.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1
_DEFAULT_STORE_PATH = Path(".cache/canonical_ingredients_store.json")


@dataclass
class CanonicalEntry:
    canonical_name: str
    aliases: set[str] = field(default_factory=set)
    disambiguation_class: str = ""
    created_by: str = "llm"
    confidence: float = 1.0


class CanonicalStore:
    """An append-only-by-the-engine map of normalized variant -> canonical name."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else _DEFAULT_STORE_PATH
        self._entries: dict[str, CanonicalEntry] = {}
        self._variant_index: dict[str, str] = {}

    # -- construction -----------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None) -> "CanonicalStore":
        store = cls(path)
        if store.path.exists():
            store._load_from_disk()
        return store

    def _load_from_disk(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for raw in data.get("canonicals", []):
            name = str(raw.get("canonical_name", "")).strip()
            if not name:
                continue
            entry = CanonicalEntry(
                canonical_name=name,
                aliases={str(a) for a in raw.get("aliases", []) if str(a)},
                disambiguation_class=str(raw.get("disambiguation_class", "") or ""),
                created_by=str(raw.get("created_by", "llm") or "llm"),
                confidence=_coerce_confidence(raw.get("confidence")),
            )
            self._entries[name] = entry
            for alias in entry.aliases:
                self._variant_index[alias] = name

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _STORE_VERSION,
            "canonicals": [
                {
                    "canonical_name": entry.canonical_name,
                    "aliases": sorted(entry.aliases),
                    "disambiguation_class": entry.disambiguation_class,
                    "created_by": entry.created_by,
                    "confidence": entry.confidence,
                }
                for entry in self._entries.values()
            ],
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # -- reads ------------------------------------------------------------------
    def lookup(self, variant_key: str) -> str | None:
        """Return the canonical name for a normalized variant key, or None."""
        return self._variant_index.get(variant_key)

    def canonical_names(self) -> list[str]:
        """Current master list, in insertion order (the v1 whole-list candidates)."""
        return list(self._entries.keys())

    def entries(self) -> list[CanonicalEntry]:
        return list(self._entries.values())

    def disambiguation_class_of(self, canonical_name: str) -> str:
        entry = self._entries.get(canonical_name)
        return entry.disambiguation_class if entry else ""

    def __contains__(self, canonical_name: object) -> bool:
        return canonical_name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    # -- writes -----------------------------------------------------------------
    def add_canonical(
        self,
        canonical_name: str,
        *,
        disambiguation_class: str = "",
        created_by: str = "llm",
        confidence: float = 1.0,
        variant_key: str | None = None,
    ) -> CanonicalEntry:
        """Create the canonical if absent (idempotent), optionally indexing a variant."""
        name = canonical_name.strip()
        if not name:
            raise ValueError("canonical_name must be non-empty")
        entry = self._entries.get(name)
        if entry is None:
            entry = CanonicalEntry(
                canonical_name=name,
                disambiguation_class=disambiguation_class,
                created_by=created_by,
                confidence=_coerce_confidence(confidence),
            )
            self._entries[name] = entry
            LOGGER.debug("canonical_store add_canonical name=%s class=%s", name, disambiguation_class)
        elif disambiguation_class and not entry.disambiguation_class:
            entry.disambiguation_class = disambiguation_class
        if variant_key:
            self.add_variant(variant_key, name)
        return entry

    def add_variant(self, variant_key: str, canonical_name: str) -> None:
        """Index a normalized variant to an existing canonical (last write wins)."""
        key = variant_key.strip()
        name = canonical_name.strip()
        if not key:
            return
        if name not in self._entries:
            raise KeyError(f"unknown canonical_name: {name!r}; call add_canonical first")
        previous = self._variant_index.get(key)
        if previous is not None and previous != name:
            # re-pointing a variant: drop it from the old entry's alias set
            self._entries[previous].aliases.discard(key)
        self._variant_index[key] = name
        self._entries[name].aliases.add(key)


def _coerce_confidence(raw_value: object) -> float:
    try:
        value = float(raw_value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # Engine-written entries always carry a numeric confidence; a missing or
        # unparseable value means hand-edited/corrupt input — distrust it (0.0)
        # rather than granting full trust, matching the other coercion helpers.
        return 0.0
    return max(0.0, min(1.0, value))
