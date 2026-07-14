"""SQLite database with thread-local connections and linear migrations."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

# Append-only list; each entry is one migration script. The applied count is
# tracked in schema_version, so editing an already-shipped entry is forbidden.
_MIGRATIONS = [
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE series_10s (
        ts       INTEGER NOT NULL,
        instance TEXT NOT NULL,
        kind     TEXT NOT NULL,
        count    INTEGER NOT NULL,
        PRIMARY KEY (ts, instance, kind)
    );
    CREATE INDEX idx_series_10s_ts ON series_10s (ts);
    """,
    """
    CREATE TABLE chains (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        instance      TEXT NOT NULL,
        target        TEXT NOT NULL,
        verb          TEXT NOT NULL,
        opened_at     REAL NOT NULL,
        client        TEXT,
        payload_size  INTEGER NOT NULL,
        echo_count    INTEGER NOT NULL,
        first_echo_ms REAL,
        redundant     INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_chains_opened ON chains (opened_at);
    CREATE TABLE attribution_10s (
        ts       INTEGER NOT NULL,
        instance TEXT NOT NULL,
        klass    TEXT NOT NULL,
        count    INTEGER NOT NULL,
        PRIMARY KEY (ts, instance, klass)
    );
    CREATE INDEX idx_attribution_10s_ts ON attribution_10s (ts);
    """,
]


class Database:
    def __init__(self, data_dir: Path | str):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "zigbee-ninja.db"
        self._local = threading.local()
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        conn = self.connect()
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0
        for number, script in enumerate(_MIGRATIONS[current:], start=current + 1):
            conn.executescript(script)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (number,))
            conn.commit()
