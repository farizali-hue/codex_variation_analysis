from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from amazon_scraper.kv_store import SqliteKeyValueStore
from amazon_scraper.retrieval import EmbeddingCache
from amazon_scraper.vision_extraction import VisionCache

_MODEL = "text-embedding-3-small"
# a slice of a real text-embedding-3-small vector
_REAL_VECTOR = [
    -0.01079559326171875, -0.04876708984375, 0.0131072998046875, -0.0264739990234375, -0.0218048095703125,
    -0.003139495849609375, 0.032562255859375, 0.008270263671875, -0.01605224609375, -0.0188140869140625,
]


class SqliteKeyValueStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "store.sqlite3"

    def test_round_trips_bytes(self) -> None:
        store = SqliteKeyValueStore(self.path)
        self.addCleanup(store.close)
        store.set("abc", b"payload")
        store.commit()
        self.assertEqual(store.get("abc"), b"payload")

    def test_missing_key_returns_none(self) -> None:
        store = SqliteKeyValueStore(self.path)
        self.addCleanup(store.close)
        self.assertIsNone(store.get("never-written"))

    def test_set_replaces_existing_key(self) -> None:
        store = SqliteKeyValueStore(self.path)
        self.addCleanup(store.close)
        store.set("abc", b"first")
        store.set("abc", b"second")
        store.commit()
        self.assertEqual(store.get("abc"), b"second")

    def test_creates_parent_directory(self) -> None:
        nested = Path(self._dir.name) / "made" / "up" / "store.sqlite3"
        store = SqliteKeyValueStore(nested)
        self.addCleanup(store.close)
        self.assertTrue(nested.parent.is_dir())


class EmbeddingCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "embeddings.sqlite3"

    def test_vector_round_trips_bit_identically(self) -> None:
        cache = EmbeddingCache(self.path)
        self.addCleanup(cache.close)
        cache.set(_MODEL, "Item Form: Softgel", _REAL_VECTOR)
        cache.save()
        # float32 packing is lossless for OpenAI embeddings, so this is exact
        self.assertEqual(cache.get(_MODEL, "Item Form: Softgel"), _REAL_VECTOR)

    def test_miss_returns_none(self) -> None:
        cache = EmbeddingCache(self.path)
        self.addCleanup(cache.close)
        self.assertIsNone(cache.get(_MODEL, "never embedded"))

    def test_entries_survive_close_and_reopen(self) -> None:
        cache = EmbeddingCache(self.path)
        cache.set(_MODEL, "Item Form: Softgel", _REAL_VECTOR)
        cache.save()
        cache.close()

        reopened = EmbeddingCache(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(_MODEL, "Item Form: Softgel"), _REAL_VECTOR)

    def test_key_ignores_surrounding_whitespace(self) -> None:
        cache = EmbeddingCache(self.path)
        self.addCleanup(cache.close)
        cache.set(_MODEL, "Item Form: Softgel", _REAL_VECTOR)
        cache.save()
        self.assertEqual(cache.get(_MODEL, "  Item Form:   Softgel  "), _REAL_VECTOR)

    def test_key_formula_is_unchanged(self) -> None:
        # guards the on-disk key against an accidental rename: every entry
        # migrated from the JSON cache is only reachable by this exact digest
        cache = EmbeddingCache(self.path)
        self.addCleanup(cache.close)
        expected = hashlib.sha256(f"{_MODEL}\nItem Form: Softgel".encode("utf-8")).hexdigest()
        self.assertEqual(cache._key(_MODEL, "Item Form: Softgel"), expected)

    def test_different_models_do_not_share_entries(self) -> None:
        cache = EmbeddingCache(self.path)
        self.addCleanup(cache.close)
        cache.set(_MODEL, "Item Form: Softgel", _REAL_VECTOR)
        cache.save()
        self.assertIsNone(cache.get("text-embedding-3-large", "Item Form: Softgel"))


class VisionCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "vision.sqlite3"

    def test_payload_round_trips(self) -> None:
        cache = VisionCache(self.path)
        self.addCleanup(cache.close)
        payload = {"value": "2 Softgels", "confidence": 0.91, "evidence": "facts panel"}
        cache.set("gpt-5.4-mini", "facts_panel", "fp-1", payload)
        cache.save()
        self.assertEqual(cache.get("gpt-5.4-mini", "facts_panel", "fp-1"), payload)

    def test_context_is_part_of_the_key(self) -> None:
        cache = VisionCache(self.path)
        self.addCleanup(cache.close)
        cache.set("gpt-5.4-mini", "generic_field", "fp-1", {"value": "A"}, context="ingredients")
        cache.save()
        self.assertIsNone(cache.get("gpt-5.4-mini", "generic_field", "fp-1", context="serving_size"))

    def test_corrupt_row_reads_as_a_miss(self) -> None:
        cache = VisionCache(self.path)
        self.addCleanup(cache.close)
        key = cache._key("gpt-5.4-mini", "facts_panel", "fp-1", "")
        cache._store.set(key, b"{not json")
        cache._store.commit()
        self.assertIsNone(cache.get("gpt-5.4-mini", "facts_panel", "fp-1"))

    def test_non_dict_row_reads_as_a_miss(self) -> None:
        cache = VisionCache(self.path)
        self.addCleanup(cache.close)
        key = cache._key("gpt-5.4-mini", "facts_panel", "fp-1", "")
        cache._store.set(key, json.dumps(["not", "a", "dict"]).encode("utf-8"))
        cache._store.commit()
        self.assertIsNone(cache.get("gpt-5.4-mini", "facts_panel", "fp-1"))


if __name__ == "__main__":
    unittest.main()
