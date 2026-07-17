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
    """
    CREATE TABLE tiles (
        capability     TEXT NOT NULL,
        target         TEXT NOT NULL,
        status         TEXT NOT NULL,
        granted_at     REAL,
        deployed_at    REAL,
        revoked_at     REAL,
        version        TEXT,
        last_health_at REAL,
        detail         TEXT,
        PRIMARY KEY (capability, target)
    );
    """,
    """
    CREATE TABLE airtime_10s (
        ts         INTEGER NOT NULL,
        instance   TEXT NOT NULL,
        bucket     TEXT NOT NULL,
        airtime_us REAL NOT NULL,
        frames     INTEGER NOT NULL,
        PRIMARY KEY (ts, instance, bucket)
    );
    CREATE INDEX idx_airtime_10s_ts ON airtime_10s (ts);
    """,
    """
    CREATE TABLE latency_10s (
        ts       INTEGER NOT NULL,
        instance TEXT NOT NULL,
        count    INTEGER NOT NULL,
        p50_ms   REAL NOT NULL,
        p95_ms   REAL NOT NULL,
        max_ms   REAL NOT NULL,
        PRIMARY KEY (ts, instance)
    );
    CREATE INDEX idx_latency_10s_ts ON latency_10s (ts);
    """,
    """
    CREATE TABLE topology_snapshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        instance   TEXT NOT NULL,
        pulled_at  REAL NOT NULL,
        node_count INTEGER NOT NULL,
        link_count INTEGER NOT NULL,
        summary    TEXT NOT NULL,
        raw        TEXT NOT NULL
    );
    CREATE INDEX idx_topology_instance_time ON topology_snapshots (instance, pulled_at);
    """,
    """
    CREATE TABLE calibrations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        instance    TEXT NOT NULL,
        target      TEXT NOT NULL,
        started_at  REAL NOT NULL,
        finished_at REAL,
        status      TEXT NOT NULL,
        knee_eps    REAL,
        detail      TEXT NOT NULL
    );
    CREATE INDEX idx_calibrations_instance_time ON calibrations (instance, started_at);
    """,
    """
    CREATE TABLE alert_rules (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        builtin         TEXT UNIQUE,
        name            TEXT NOT NULL,
        metric          TEXT NOT NULL,
        instance        TEXT NOT NULL DEFAULT '*',
        op              TEXT NOT NULL DEFAULT '>',
        threshold       REAL NOT NULL,
        clear_threshold REAL,
        sustain_seconds INTEGER NOT NULL DEFAULT 60,
        severity        TEXT NOT NULL DEFAULT 'warning',
        enabled         INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE alert_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id    INTEGER NOT NULL,
        instance   TEXT NOT NULL,
        opened_at  REAL NOT NULL,
        cleared_at REAL,
        peak_value REAL,
        context    TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_alert_events_cleared ON alert_events (cleared_at);
    CREATE INDEX idx_alert_events_opened ON alert_events (opened_at);
    """,
    """
    CREATE TABLE ledger_daily (
        instance   TEXT NOT NULL,
        day        TEXT NOT NULL,
        commander  TEXT NOT NULL,
        chains     INTEGER NOT NULL DEFAULT 0,
        tx_us      REAL NOT NULL DEFAULT 0,
        rx_us      REAL NOT NULL DEFAULT 0,
        provenance TEXT NOT NULL DEFAULT '',
        params     TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (instance, day, commander)
    );
    CREATE INDEX idx_ledger_daily_day ON ledger_daily (day);
    CREATE TABLE ledger_device_daily (
        instance      TEXT NOT NULL,
        day           TEXT NOT NULL,
        device        TEXT NOT NULL,
        publishes     INTEGER NOT NULL DEFAULT 0,
        autonomous_us REAL NOT NULL DEFAULT 0,
        provenance    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (instance, day, device)
    );
    CREATE INDEX idx_ledger_device_daily_day ON ledger_device_daily (day);
    """,
    """
    CREATE TABLE journal (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        ts       REAL NOT NULL,
        instance TEXT NOT NULL,
        kind     TEXT NOT NULL,
        subject  TEXT NOT NULL,
        detail   TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_journal_ts ON journal (ts);
    """,
    """
    CREATE TABLE recommendations (
        id               TEXT PRIMARY KEY,
        detector         TEXT NOT NULL,
        instance         TEXT NOT NULL,
        subject          TEXT NOT NULL,
        finding          TEXT NOT NULL,
        action           TEXT NOT NULL DEFAULT '{}',
        saving           TEXT NOT NULL DEFAULT '{}',
        confidence       TEXT NOT NULL DEFAULT 'low',
        evidence         TEXT NOT NULL DEFAULT '[]',
        state            TEXT NOT NULL DEFAULT 'open',
        fingerprint      TEXT NOT NULL DEFAULT '{}',
        state_note       TEXT,
        created_at       REAL NOT NULL,
        updated_at       REAL NOT NULL,
        state_changed_at REAL
    );
    CREATE INDEX idx_recommendations_state ON recommendations (state);
    ALTER TABLE chains ADD COLUMN payload_digest TEXT;
    """,
    """
    ALTER TABLE recommendations ADD COLUMN verification TEXT;
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
            # Writers run on the flush worker, API threads, and the detector
            # thread; WAL allows one at a time, so brief collisions wait
            # instead of raising SQLITE_BUSY.
            conn.execute("PRAGMA busy_timeout=5000")
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
