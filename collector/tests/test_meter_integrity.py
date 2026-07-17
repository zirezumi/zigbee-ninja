"""The collector must never let its own runtime distort what it reports:
loop-lag telemetry, thread-safe chain draining, and the self-health seed."""

import threading

from zigbee_ninja import alerts
from zigbee_ninja.attribution.chains import ChainTracker
from zigbee_ninja.ingest.engine import (
    ACTIVITY_ENTRIES_KEPT,
    LOOP_LAG_STALLS_KEPT,
    LOOP_LAG_WINDOW_SECONDS,
    LoopActivityLog,
    LoopLagMonitor,
)


def test_loop_lag_monitor_tracks_window_max_and_stalls():
    now = {"t": 1000.0}
    monitor = LoopLagMonitor(clock=lambda: now["t"])
    monitor.record(0.005)
    now["t"] += 1
    monitor.record(1.2)
    now["t"] += 1
    monitor.record(0.010)
    stats = monitor.stats()
    assert stats["last_ms"] == 10.0
    assert stats["max_60s_ms"] == 1200.0
    assert stats["stalls_over_250ms"] == 1
    assert stats["ewma_ms"] is not None

    # Samples age out of the window; the max follows.
    now["t"] += LOOP_LAG_WINDOW_SECONDS + 1
    monitor.record(0.002)
    assert monitor.stats()["max_60s_ms"] == 2.0
    # Negative lag (clock adjustments) clamps to zero, never corrupts.
    monitor.record(-0.5)
    assert monitor.stats()["last_ms"] == 0.0


def test_loop_lag_monitor_keeps_recent_stall_timestamps():
    now = {"mono": 1000.0, "wall": 1_700_000_000.0}
    monitor = LoopLagMonitor(clock=lambda: now["mono"], wall=lambda: now["wall"])
    monitor.record(0.010)  # below the stall threshold: not kept
    now["wall"] += 5
    monitor.record(3.1)
    stalls = monitor.stats()["recent_stalls"]
    assert stalls == [{"at": 1_700_000_005.0, "lag_ms": 3100.0}]

    for _ in range(LOOP_LAG_STALLS_KEPT + 10):
        now["wall"] += 1
        monitor.record(0.5)
    stalls = monitor.stats()["recent_stalls"]
    assert len(stalls) == LOOP_LAG_STALLS_KEPT
    assert stalls[-1]["at"] == now["wall"]


def test_activity_log_records_totals_and_slow_entries():
    now = {"mono": 50.0, "wall": 1_700_000_000.0}
    log = LoopActivityLog(clock=lambda: now["mono"], wall=lambda: now["wall"])

    with log.span("mqtt_message"):
        now["mono"] += 0.002  # fast: counted, not kept in the slow ring
    with log.span("mqtt_message"):
        now["mono"] += 0.350  # slow: kept with its wall-clock stamp

    stats = log.stats()
    assert stats["totals"]["mqtt_message"] == {"count": 2, "slow": 1, "max_ms": 350.0}
    assert stats["recent_slow"] == [
        {"label": "mqtt_message", "at": 1_700_000_000.0, "ms": 350.0}
    ]

    for _ in range(ACTIVITY_ENTRIES_KEPT + 10):
        log.note("tile_heartbeat_write", 200.0)
    assert len(log.stats()["recent_slow"]) == ACTIVITY_ENTRIES_KEPT


def test_activity_log_times_gc_pauses():
    now = {"mono": 10.0, "wall": 1_700_000_000.0}
    log = LoopActivityLog(clock=lambda: now["mono"], wall=lambda: now["wall"])
    log._on_gc("start", {"generation": 2})
    now["mono"] += 1.5
    log._on_gc("stop", {"generation": 2})
    stats = log.stats()
    assert stats["totals"]["gc_gen2"]["max_ms"] == 1500.0
    assert stats["recent_slow"][0]["label"] == "gc_gen2"
    # A stop with no matching start (callback installed mid-collection)
    # records nothing rather than a garbage duration.
    log._on_gc("stop", {"generation": 1})
    assert "gc_gen1" not in log.stats()["totals"]


def test_loop_lag_metric_and_seed_rule_registered():
    assert alerts.METRICS["collector_loop_lag_ms"]["scope"] == "global"
    seed = next(
        rule for rule in alerts.SEED_RULES if rule["builtin"] == "collector_loop_lag"
    )
    assert seed["metric"] == "collector_loop_lag_ms"
    assert seed["enabled"] == 1  # self-health rules ship enabled


def test_chain_tracker_survives_concurrent_ingest_and_drain():
    clock = {"t": 1000.0}
    tracker = ChainTracker(clock=lambda: clock["t"])
    errors: list[Exception] = []
    drained: list = []
    stop = threading.Event()

    def drain_loop():
        try:
            while not stop.is_set():
                drained.extend(tracker.drain_finalized())
        except Exception as exc:  # pragma: no cover - the failure signal
            errors.append(exc)

    thread = threading.Thread(target=drain_loop)
    thread.start()
    try:
        for index in range(2000):
            tracker.on_command("z2m-x", f"dev-{index % 7}", "set", b"{}")
            tracker.on_state("z2m-x", f"dev-{index % 7}")
            clock["t"] += 0.01
        clock["t"] += 60.0  # expire everything still open
    finally:
        stop.set()
        thread.join(timeout=10)
    drained.extend(tracker.drain_finalized())
    assert not errors
    assert len(drained) == 2000
