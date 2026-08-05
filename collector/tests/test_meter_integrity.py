"""The collector must never let its own runtime distort what it reports:
loop-lag telemetry, thread-safe chain draining, and the self-health seed."""

import gc
import threading

from zigbee_ninja import alerts
from zigbee_ninja.attribution.chains import ChainTracker
from zigbee_ninja.ingest.engine import (
    ACTIVITY_ENTRIES_KEPT,
    GC_GEN2_THRESHOLD,
    GC_MAINTENANCE_WINDOWS_KEPT,
    LOOP_LAG_STALLS_KEPT,
    LOOP_LAG_WINDOW_SECONDS,
    GcMaintenance,
    LoopActivityLog,
    LoopLagMonitor,
    _quiet_full_collections,
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


def test_gc_maintenance_skips_the_startup_interval_then_comes_due():
    now = {"t": 1000.0}
    keeper = GcMaintenance(
        interval_seconds=100.0, clock=lambda: now["t"], collect=lambda: 0
    )
    # Startup has just collected and frozen, so the first check must arm the
    # clock rather than fire a second pause for nothing to reclaim.
    assert keeper.due() is False
    now["t"] += 99.0
    assert keeper.due() is False
    now["t"] += 2.0
    assert keeper.due() is True


def test_gc_maintenance_records_bounded_wall_clock_windows():
    now = {"mono": 1000.0, "wall": 1_700_000_000.0}
    keeper = GcMaintenance(
        interval_seconds=10.0,
        clock=lambda: now["mono"],
        wall=lambda: now["wall"],
        collect=lambda: (now.__setitem__("mono", now["mono"] + 1.25), 42)[1],
    )
    window = keeper.run()
    assert window == {
        "at": 1_700_000_000.0,
        "until": 1_700_000_001.25,
        "ms": 1250.0,
        "freed": 42,
    }
    # Running resets the interval, so a cycle cannot re-fire immediately.
    assert keeper.due() is False

    for _ in range(GC_MAINTENANCE_WINDOWS_KEPT + 5):
        keeper.run()
    stats = keeper.stats()
    assert len(stats["recent_windows"]) == GC_MAINTENANCE_WINDOWS_KEPT
    assert stats["runs"] == GC_MAINTENANCE_WINDOWS_KEPT + 6
    assert stats["next_due_in_s"] == 10.0


def test_gc_maintenance_books_its_pause_apart_from_unscheduled_ones():
    """The whole point of the schedule is that gc_gen2 becomes a number that
    means 'pauses nobody asked for'. If the scheduled pass landed in the same
    bucket it would mask exactly what it is meant to remove."""
    now = {"mono": 10.0, "wall": 1_700_000_000.0}
    log = LoopActivityLog(clock=lambda: now["mono"], wall=lambda: now["wall"])

    def fake_collect() -> int:
        log._on_gc("start", {"generation": 2})
        now["mono"] += 2.0
        log._on_gc("stop", {"generation": 2})
        return 7

    keeper = GcMaintenance(clock=lambda: now["mono"], wall=lambda: now["wall"],
                           collect=fake_collect)
    keeper.run(log)

    totals = log.stats()["totals"]
    assert totals["gc_maintenance"]["max_ms"] == 2000.0
    assert "gc_gen2" not in totals

    # Outside the window the generation label is back.
    log._on_gc("start", {"generation": 2})
    now["mono"] += 0.4
    log._on_gc("stop", {"generation": 2})
    assert log.stats()["totals"]["gc_gen2"]["max_ms"] == 400.0


def test_gc_relabel_does_not_capture_another_thread_s_collection():
    """A collection another thread trips during the window is still an
    unscheduled event; stealing its label would undercount them."""
    log = LoopActivityLog()
    seen: list[str] = []

    def other_thread():
        log._on_gc("start", {"generation": 2})
        log._on_gc("stop", {"generation": 2})
        seen.extend(log.stats()["totals"])

    with log.relabel_gc("gc_maintenance"):
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()

    assert "gc_gen2" in seen
    assert "gc_maintenance" not in seen


def test_gc_maintenance_real_cycle_leaves_the_graph_frozen():
    """Exercises the actual unfreeze/collect/freeze against CPython rather
    than a stub: the freeze must be re-established, or the next interval runs
    against an unfrozen graph and the startup fix is silently undone."""
    gc.freeze()
    before = gc.get_freeze_count()
    assert before > 0
    keeper = GcMaintenance()
    window = keeper.run()
    assert isinstance(window["freed"], int)
    assert gc.get_freeze_count() > 0


def test_startup_quieting_hands_full_passes_to_the_schedule():
    original = gc.get_threshold()
    try:
        _quiet_full_collections()
        assert gc.get_threshold()[2] == GC_GEN2_THRESHOLD
    finally:
        gc.set_threshold(*original)
        gc.unfreeze()


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
