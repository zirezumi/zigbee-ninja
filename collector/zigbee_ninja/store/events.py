"""Raw event store for the burst inspector (DESIGN.md §12).

Events land in an in-memory buffer, flush into a *hot* DuckDB table on the
engine's 10 s cadence, and closed hours export to hourly Parquet segments
(ZSTD) under ``<data>/events/``. Queries union the hot table with the
segment files in place: no series-cardinality explosion, event-level
fidelity for the burst-inspector window.

Retention is quota-first: segments older than the horizon are deleted, then
oldest segments go until the directory fits the quota (both settings-backed).
The hot table only ever holds the open hour, so the DuckDB file stays small.

V1 captures the T0 MQTT stream (per-instance topics, plus zigbee-ninja's own
publishes as ``self``) and the T2 wire tier (every decoded EZSP frame, on
pcap timestamps). T1 probe batches already reach the broker as MQTT messages,
so they appear at T0 granularity; per-event probe capture can ride later.
A full buffer drops new events and counts the drops: bursty meshes degrade
visibly, never by blocking the ingest path.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import duckdb

BUFFER_MAX_EVENTS = 200_000
SEGMENT_PREFIX = "segment-"
DEFAULT_QUOTA_MB = 4096
DEFAULT_HORIZON_HOURS = 48
EVENT_COLUMNS = "ts, source, instance, kind, direction, target, size"
INSERT_CHUNK_ROWS = 500


class RawEventLog:
    def __init__(self, data_dir: Path | str, clock: Callable[[], float] = time.time):
        self._dir = Path(data_dir) / "events"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        # Two locks on purpose: record() runs on the event loop for every
        # MQTT message and decoded wire frame, so it may only ever contend
        # with other buffer appends (microseconds). All DuckDB work sits
        # behind its own lock; a slow flush or query must never reach the
        # loop through the buffer (the residual calibration-pacer stalls
        # were exactly this lock coupling).
        self._buffer_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._buffer: list[tuple] = []
        self.dropped = 0
        self._conn = duckdb.connect(str(self._dir / "hot.duckdb"))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "ts DOUBLE, source VARCHAR, instance VARCHAR, kind VARCHAR, "
            "direction VARCHAR, target VARCHAR, size INTEGER)"
        )
        self._exported_hour = int(self._clock() // 3600)

    # -- capture -----------------------------------------------------------------

    def record(
        self,
        ts: float,
        source: str,
        instance: str,
        kind: str,
        direction: str,
        target: str | None,
        size: int,
    ) -> None:
        with self._buffer_lock:
            if len(self._buffer) >= BUFFER_MAX_EVENTS:
                self.dropped += 1
                return
            self._buffer.append((ts, source, instance, kind, direction, target, size))

    # -- flush + retention ---------------------------------------------------------

    def _insert_rows(self, rows: list[tuple]) -> None:
        """One transaction of chunked multi-row inserts: per-row executemany
        autocommits each row, which is seconds of WAL churn at busy-mesh
        volumes."""
        self._conn.execute("BEGIN TRANSACTION")
        try:
            for start in range(0, len(rows), INSERT_CHUNK_ROWS):
                chunk = rows[start : start + INSERT_CHUNK_ROWS]
                placeholders = ", ".join(["(?, ?, ?, ?, ?, ?, ?)"] * len(chunk))
                params = [value for row in chunk for value in row]
                self._conn.execute(
                    f"INSERT INTO events ({EVENT_COLUMNS}) VALUES {placeholders}",
                    params,
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def flush(
        self,
        quota_mb: int = DEFAULT_QUOTA_MB,
        horizon_hours: int = DEFAULT_HORIZON_HOURS,
    ) -> None:
        with self._buffer_lock:
            rows, self._buffer = self._buffer, []
        with self._db_lock:
            if rows:
                self._insert_rows(rows)
            hour = int(self._clock() // 3600)
            if hour > self._exported_hour:
                self._export_closed_hours(hour)
                self._exported_hour = hour
                self._enforce_retention(quota_mb, horizon_hours)

    def _segment_path(self, hour: int) -> Path:
        return self._dir / f"{SEGMENT_PREFIX}{hour}.parquet"

    def _export_closed_hours(self, current_hour: int) -> None:
        hours = self._conn.execute(
            "SELECT DISTINCT CAST(ts // 3600 AS BIGINT) AS h FROM events "
            "WHERE ts < ? ORDER BY h",
            (current_hour * 3600.0,),
        ).fetchall()
        for (hour,) in hours:
            target = self._segment_path(hour)
            self._conn.execute(
                f"COPY (SELECT {EVENT_COLUMNS} FROM events "
                "WHERE ts >= ? AND ts < ? ORDER BY ts) "
                f"TO '{target.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)",
                (hour * 3600.0, (hour + 1) * 3600.0),
            )
        self._conn.execute("DELETE FROM events WHERE ts < ?", (current_hour * 3600.0,))

    def _segments(self) -> list[Path]:
        return sorted(self._dir.glob(f"{SEGMENT_PREFIX}*.parquet"))

    def _enforce_retention(self, quota_mb: int, horizon_hours: int) -> None:
        cutoff_hour = int(self._clock() // 3600) - horizon_hours
        segments = self._segments()
        for path in list(segments):
            try:
                hour = int(path.stem[len(SEGMENT_PREFIX) :])
            except ValueError:
                continue
            if hour < cutoff_hour:
                path.unlink(missing_ok=True)
                segments.remove(path)
        total = sum(path.stat().st_size for path in segments)
        quota = quota_mb * 1024 * 1024
        for path in list(segments):  # oldest first: sorted by hour in the name
            if total <= quota:
                break
            total -= path.stat().st_size
            path.unlink(missing_ok=True)

    # -- queries -------------------------------------------------------------------

    def _from_clause(self, start: float, end: float) -> str:
        """Hot table unioned with only the segments overlapping the window."""
        parts = [f"SELECT {EVENT_COLUMNS} FROM events"]
        for path in self._segments():
            try:
                hour = int(path.stem[len(SEGMENT_PREFIX) :])
            except ValueError:
                continue
            if hour * 3600.0 < end and (hour + 1) * 3600.0 > start:
                parts.append(
                    f"SELECT {EVENT_COLUMNS} FROM read_parquet('{path.as_posix()}')"
                )
        return "(" + " UNION ALL ".join(parts) + ")"

    def timeline(
        self, instance: str, start: float, end: float, bucket_ms: int
    ) -> dict:
        bucket_s = max(1, int(bucket_ms)) / 1000.0
        with self._db_lock:
            rows = self._conn.execute(
                f"SELECT CAST(FLOOR((ts - ?) / ?) AS BIGINT) AS bin, source, "
                f"COUNT(*) AS events, SUM(size) AS bytes "
                f"FROM {self._from_clause(start, end)} "
                "WHERE instance = ? AND ts >= ? AND ts < ? "
                "GROUP BY bin, source ORDER BY bin",
                (start, bucket_s, instance, start, end),
            ).fetchall()
        bins: dict[int, dict] = {}
        for bin_index, source, events, size in rows:
            entry = bins.setdefault(int(bin_index), {})
            entry[source] = {"events": int(events), "bytes": int(size or 0)}
        return {
            "start": start,
            "end": end,
            "bucket_ms": bucket_ms,
            "bins": [{"bin": index, **sources} for index, sources in sorted(bins.items())],
        }

    def events(self, instance: str, start: float, end: float, limit: int) -> list[dict]:
        with self._db_lock:
            rows = self._conn.execute(
                f"SELECT {EVENT_COLUMNS} FROM {self._from_clause(start, end)} "
                "WHERE instance = ? AND ts >= ? AND ts < ? ORDER BY ts LIMIT ?",
                (instance, start, end, limit),
            ).fetchall()
        columns = [name.strip() for name in EVENT_COLUMNS.split(",")]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def _filters(
        self,
        source: str | None,
        kinds: tuple[str, ...] | None,
        direction: str | None,
        targets: tuple[str, ...] | None = None,
    ) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if direction is not None:
            clauses.append("direction = ?")
            params.append(direction)
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if targets:
            clauses.append(f"target IN ({','.join('?' * len(targets))})")
            params.extend(targets)
        return (" AND " + " AND ".join(clauses)) if clauses else "", params

    def rate_bins(
        self,
        instance: str,
        start: float,
        end: float,
        bucket_s: float,
        *,
        source: str | None = None,
        kinds: tuple[str, ...] | None = None,
        direction: str | None = None,
        targets: tuple[str, ...] | None = None,
    ) -> list[tuple[int, int]]:
        """Fixed-bin event counts for one instance, optionally filtered by
        source, kind, direction, and exact target topics. Bins index from
        ``start`` in steps of ``bucket_s``; only nonempty bins return."""
        filter_sql, filter_params = self._filters(source, kinds, direction, targets)
        with self._db_lock:
            rows = self._conn.execute(
                f"SELECT CAST(FLOOR((ts - ?) / ?) AS BIGINT) AS bin, COUNT(*) AS n "
                f"FROM {self._from_clause(start, end)} "
                f"WHERE instance = ? AND ts >= ? AND ts < ?{filter_sql} "
                "GROUP BY bin ORDER BY bin",
                (start, bucket_s, instance, start, end, *filter_params),
            ).fetchall()
        return [(int(bin_index), int(count)) for bin_index, count in rows]

    def event_times(
        self,
        instance: str,
        start: float,
        end: float,
        *,
        source: str | None = None,
        kinds: tuple[str, ...] | None = None,
        direction: str | None = None,
        targets: tuple[str, ...] | None = None,
        limit: int = 50_000,
    ) -> list[float]:
        """Sorted event timestamps in the window under the same filters."""
        filter_sql, filter_params = self._filters(source, kinds, direction, targets)
        with self._db_lock:
            rows = self._conn.execute(
                f"SELECT ts FROM {self._from_clause(start, end)} "
                f"WHERE instance = ? AND ts >= ? AND ts < ?{filter_sql} "
                "ORDER BY ts LIMIT ?",
                (instance, start, end, *filter_params, limit),
            ).fetchall()
        return [float(ts) for (ts,) in rows]

    def stats(self) -> dict:
        with self._buffer_lock:
            buffered = len(self._buffer)
            dropped = self.dropped
        with self._db_lock:
            hot_rows = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            segments = self._segments()
            return {
                "buffered": buffered,
                "dropped": dropped,
                "hot_rows": int(hot_rows),
                "segments": len(segments),
                "segment_bytes": sum(path.stat().st_size for path in segments),
            }
