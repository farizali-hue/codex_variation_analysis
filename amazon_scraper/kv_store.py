from __future__ import annotations

import sqlite3
from pathlib import Path


class SqliteKeyValueStore:
    def __init__(self, store_file: Path | str) -> None:
        self.store_file = Path(store_file)
        self.store_file.parent.mkdir(parents=True, exist_ok=True)
        # timeout lets the Streamlit UI and the CLI hold the same store open
        # without the second one failing outright on a locked write.
        self._connection = sqlite3.connect(self.store_file, timeout=30.0)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB NOT NULL)"
        )
        self._connection.commit()

    def get(self, key: str) -> bytes | None:
        row = self._connection.execute(
            "SELECT value FROM cache WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None else None

    def set(self, key: str, value: bytes) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)", (key, value)
        )

    def commit(self) -> None:
        self._connection.commit()

    def close(self) -> None:
        self._connection.commit()
        self._connection.close()
