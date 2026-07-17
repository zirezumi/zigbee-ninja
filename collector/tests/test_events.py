"""Raw event store: flush, hourly export, retention, queries (DESIGN.md §12)."""

import threading

from zigbee_ninja.store.events import (
    BUFFER_MAX_EVENTS,
    INSERT_CHUNK_ROWS,
    RawEventLog,
)


class Clock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_log(tmp_path, start: float = 7200.0):
    clock = Clock(start)
    return RawEventLog(tmp_path, clock=clock), clock


def test_record_flush_query_roundtrip(tmp_path):
    log, clock = make_log(tmp_path, start=7200.0)
    log.record(7201.0, "mqtt", "z2m-a", "command", "in", "lamp/set", 42)
    log.record(7201.5, "wire", "z2m-a", "sendUnicast", "out", None, 30)
    log.record(7202.0, "mqtt", "z2m-b", "state", "in", "plug", 10)
    log.flush()

    events = log.events("z2m-a", 7200.0, 7300.0, limit=10)
    assert [event["kind"] for event in events] == ["command", "sendUnicast"]
    assert events[0]["target"] == "lamp/set"
    assert events[0]["size"] == 42

    timeline = log.timeline("z2m-a", 7200.0, 7210.0, bucket_ms=1000)
    bins = {entry["bin"]: entry for entry in timeline["bins"]}
    assert bins[1]["mqtt"]["events"] == 1
    assert bins[1]["wire"]["events"] == 1
    assert 2 not in bins  # 7202.0 belongs to z2m-b: instance-scoped out
    assert log.stats()["hot_rows"] == 3


def test_timeline_is_instance_scoped(tmp_path):
    log, _clock = make_log(tmp_path)
    log.record(7201.0, "mqtt", "z2m-a", "command", "in", "x", 1)
    log.record(7201.0, "mqtt", "z2m-b", "command", "in", "x", 1)
    log.flush()
    timeline = log.timeline("z2m-a", 7200.0, 7210.0, bucket_ms=1000)
    assert sum(entry["mqtt"]["events"] for entry in timeline["bins"]) == 1


def test_hour_rollover_exports_parquet_and_unions_queries(tmp_path):
    log, clock = make_log(tmp_path, start=7200.0)  # hour 2
    log.record(7201.0, "mqtt", "z2m-a", "command", "in", "lamp/set", 5)
    log.flush()

    clock.now = 10801.0  # hour 3
    log.record(10801.5, "mqtt", "z2m-a", "state", "in", "lamp", 7)
    log.flush()

    stats = log.stats()
    assert stats["segments"] == 1  # hour 2 exported
    assert stats["hot_rows"] == 1  # only the open hour remains hot
    assert (tmp_path / "events" / "segment-2.parquet").exists()

    events = log.events("z2m-a", 7200.0, 10900.0, limit=10)
    assert [event["kind"] for event in events] == ["command", "state"]


def test_horizon_and_quota_prune_segments(tmp_path):
    log, clock = make_log(tmp_path, start=7200.0)
    log.record(7201.0, "mqtt", "z2m-a", "command", "in", "x", 5)
    log.flush()
    clock.now = 10801.0  # closes hour 2
    log.record(10801.0, "mqtt", "z2m-a", "command", "in", "x", 5)
    log.flush()
    clock.now = 14401.0  # closes hour 3
    log.flush()
    assert log.stats()["segments"] == 2

    # Horizon of 1 hour keeps only the segment newer than (now-hour − 1).
    clock.now = 18001.0  # hour 5
    log.record(18001.0, "mqtt", "z2m-a", "command", "in", "x", 5)
    log.flush(horizon_hours=1)
    assert log.stats()["segments"] == 0  # hours 2 and 3 both older than cutoff 4

    # Quota of 0 MB deletes whatever remains once a new segment lands.
    clock.now = 21601.0  # hour 6, closes hour 5
    log.flush(quota_mb=0)
    assert log.stats()["segments"] == 0


def test_buffer_cap_drops_and_counts(tmp_path):
    log, _clock = make_log(tmp_path)
    log._buffer = [(0.0, "mqtt", "z", "k", "in", None, 0)] * BUFFER_MAX_EVENTS
    log.record(1.0, "mqtt", "z2m-a", "command", "in", "x", 1)
    assert log.dropped == 1
    assert log.stats()["dropped"] == 1


def test_flush_lands_every_row_across_chunk_boundaries(tmp_path):
    log, _clock = make_log(tmp_path)
    total = INSERT_CHUNK_ROWS * 2 + 1  # full chunk, full chunk, remainder of 1
    for index in range(total):
        log.record(7201.0 + index * 0.001, "mqtt", "z2m-a", "command", "in", "x", index)
    log.flush()
    assert log.stats()["hot_rows"] == total
    events = log.events("z2m-a", 7200.0, 7300.0, limit=total + 10)
    assert len(events) == total
    assert events[-1]["size"] == total - 1


def test_record_never_blocks_behind_flush_or_queries(tmp_path):
    """record() shares no lock with DuckDB work: a concurrent flush/query
    storm must not corrupt the buffer or lose rows."""
    log, _clock = make_log(tmp_path)
    errors: list[Exception] = []
    stop = threading.Event()

    def storm():
        try:
            while not stop.is_set():
                log.flush()
                log.stats()
        except Exception as exc:  # pragma: no cover - the failure signal
            errors.append(exc)

    thread = threading.Thread(target=storm)
    thread.start()
    try:
        for index in range(5000):
            log.record(7201.0 + index * 0.0001, "mqtt", "z2m-a", "state", "in", "x", 1)
    finally:
        stop.set()
        thread.join(timeout=10)
    log.flush()
    assert not errors
    assert log.stats()["hot_rows"] == 5000
    assert log.dropped == 0
