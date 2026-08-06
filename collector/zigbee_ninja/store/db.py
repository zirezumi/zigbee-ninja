"""SQLite database with thread-local connections and linear migrations."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# A checkpoint returns the WAL to this size. Without it the file keeps its
# high-water mark forever: one oversized transaction (or a stretch where
# checkpointing was blocked) leaves a WAL that never shrinks again. Observed
# live at 2.0 GB against a 260 MB database.
_WAL_SIZE_LIMIT_BYTES = 128 * 1024 * 1024

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
    """
    ALTER TABLE recommendations ADD COLUMN significance TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE recommendations ADD COLUMN cost TEXT NOT NULL DEFAULT '{}';
    """,
    """
    ALTER TABLE ledger_daily ADD COLUMN pricing_version INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE ledger_device_daily ADD COLUMN pricing_version INTEGER NOT NULL DEFAULT 1;
    """,
    """
    ALTER TABLE chains ADD COLUMN clock_skew_ms REAL NOT NULL DEFAULT 0;
    """,
    """
    ALTER TABLE chains ADD COLUMN payload_keys TEXT;
    """,
    """
    ALTER TABLE chains ADD COLUMN noop_verdict TEXT;
    ALTER TABLE chains ADD COLUMN noop_basis TEXT;
    """,
]


class Database:
    def __init__(self, data_dir: Path | str):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "zigbee-ninja.db"
        self._local = threading.local()
        self.write_failures = 0
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA journal_size_limit={_WAL_SIZE_LIMIT_BYTES}")
            # Writers run on the flush worker, API threads, the event loop and
            # the detector thread; WAL admits one at a time, so a collision
            # waits here rather than raising immediately. It is a bounded wait,
            # NOT a guarantee: the events/rollup flush has been measured holding
            # its transaction past this budget, and the writer that gives up
            # raises SQLITE_BUSY. Every write therefore has to go through
            # write() below, which is what keeps a timed-out writer from
            # poisoning the connection it timed out on.
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Run a write, committing on success and leaving NO transaction open on
        failure.

        This exists because of how Python's sqlite3 legacy mode fails. It issues
        the BEGIN itself before a DML statement, but rolling back is nobody's
        job, so a statement that raises (SQLITE_BUSY against a slow flush, say)
        returns with the transaction still OPEN. That connection now holds a read
        snapshot older than every later commit, and SQLite will not upgrade a
        stale snapshot: it refuses instantly, so busy_timeout cannot help and
        never will, because waiting cannot make the snapshot current. The
        connection is wedged for the life of the process.

        Live consequence, 2026-07-28/29: one such timeout wedged the event-loop
        thread's connection for 29 hours. Probe heartbeat writes were lost the
        whole time (silently: see MqttIngest.handler_errors) and every async
        endpoint, including /api/ws/fleet, returned 500 while the threadpool
        connections stayed perfectly healthy. It also pinned the WAL, which
        cannot checkpoint past the oldest live snapshot.
        """
        conn = self.connect()
        if conn.in_transaction:
            # Same reasoning as fresh_read, and the reason this is checked on the
            # way IN as well as cleaned up on the way out: no write() block nests
            # inside another (tests/test_write_discipline.py keeps it that way),
            # so a transaction already open here was leaked by something that
            # failed earlier, and a leaked transaction is precisely what wedges
            # the connection for good.
            logger.warning("rolling back a leaked transaction before a write")
            conn.rollback()
        try:
            yield conn
            conn.commit()
        except BaseException:
            self.write_failures += 1
            try:
                conn.rollback()
            except sqlite3.Error:
                # A connection too sick to roll back is not reusable.
                self.discard_connection()
            raise

    def fresh_read(self) -> sqlite3.Connection:
        """A connection that will not serve the next read from a stale snapshot.

        Leaf reads only. Ending the transaction is safe here precisely because
        there is no caller further out holding uncommitted work on this thread;
        calling it mid-transaction would discard that work.
        """
        conn = self.connect()
        if conn.in_transaction:
            # Nothing legitimate left this open, so it is a leaked transaction
            # from an earlier failure. Reads on it are stale, which is worse
            # than loud: a stale read of `sessions` cannot see a session that
            # was created after the snapshot, so a valid cookie reads as
            # expired.
            logger.warning("discarding a leaked transaction before a read")
            conn.rollback()
        return conn

    def discard_connection(self) -> None:
        """Drop this thread's connection so the next caller reconnects clean."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        try:
            conn.close()
        except sqlite3.Error:
            pass

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
