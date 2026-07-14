"""Typed key-value settings on top of the settings table (JSON-encoded values)."""

from __future__ import annotations

import json
from typing import Any

from .db import Database


class ConfigStore:
    def __init__(self, db: Database):
        self._db = db

    def get(self, key: str, default: Any = None) -> Any:
        conn = self._db.connect()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        conn.commit()

    def delete(self, key: str) -> None:
        conn = self._db.connect()
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()

    def all(self) -> dict[str, Any]:
        conn = self._db.connect()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: json.loads(row["value"]) for row in rows}
