"""CLI: copy the legacy JSON caches into their SQLite replacements.

The JSON caches rewrote the whole file on every save, which grew into a MemoryError
once .cache/embeddings.json passed ~234 MB. The SQLite stores write one row at a time
and load nothing at startup. Run this once to carry the existing entries across:

    python -m amazon_scraper.run_cache_migration

It reads (never writes) the old files and creates:
  .cache/embeddings.sqlite3  -- from .cache/embeddings.json
  .cache/vision.sqlite3      -- from .cache/vision.json

Keys are unchanged, so every existing entry stays reachable. Embedding vectors are
packed as float32, which is lossless for OpenAI embeddings: the model emits float32
values and the JSON only printed them at wider precision.

Reading the old embeddings file needs roughly 3x its size in free memory, so close
other heavy applications first. The old files are left exactly as they are; delete
them yourself once you are satisfied.
"""

from __future__ import annotations

import argparse
import json
import logging
from array import array
from pathlib import Path

from .kv_store import SqliteKeyValueStore

LOGGER = logging.getLogger(__name__)
_COMMIT_EVERY = 500
_MIGRATIONS = (
    (".cache/embeddings.json", ".cache/embeddings.sqlite3", "embedding"),
    (".cache/vision.json", ".cache/vision.sqlite3", "vision"),
)


def _encode(kind: str, value: object) -> bytes | None:
    if kind == "embedding":
        if not isinstance(value, list) or not value:
            return None
        return array("f", value).tobytes()
    if not isinstance(value, dict):
        return None
    return json.dumps(value).encode("utf-8")


def migrate_file(source: Path, destination: Path, kind: str, dry_run: bool) -> int:
    if not source.exists():
        print(f"skip {source} (not found)")
        return 0

    try:
        entries = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, MemoryError) as exc:
        print(f"skip {source}: could not read it ({type(exc).__name__})")
        return 0
    if not isinstance(entries, dict):
        print(f"skip {source}: not a cache object")
        return 0

    if dry_run:
        print(f"would copy {len(entries):,} {kind} entries {source} -> {destination}")
        return len(entries)

    store = SqliteKeyValueStore(destination)
    copied = skipped = 0
    try:
        for index, (key, value) in enumerate(entries.items(), start=1):
            blob = _encode(kind, value)
            if blob is None:
                skipped += 1
                continue
            store.set(key, blob)
            copied += 1
            if index % _COMMIT_EVERY == 0:
                store.commit()
        store.commit()
    finally:
        store.close()

    old_mb = source.stat().st_size / 1024 / 1024
    new_mb = destination.stat().st_size / 1024 / 1024
    print(
        f"copied {copied:,} {kind} entries ({skipped} unreadable) "
        f"{source} {old_mb:.1f} MB -> {destination} {new_mb:.1f} MB"
    )
    return copied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy the legacy JSON caches into SQLite.")
    parser.add_argument("--dry-run", action="store_true", help="report what would be copied, write nothing")
    args = parser.parse_args(argv)

    total = 0
    for source, destination, kind in _MIGRATIONS:
        total += migrate_file(Path(source), Path(destination), kind, args.dry_run)
    print(f"total entries: {total:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
