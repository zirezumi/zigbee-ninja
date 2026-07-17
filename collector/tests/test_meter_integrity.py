"""The collector must never let its own runtime distort what it reports:
loop-lag telemetry, thread-safe chain draining, and the self-health seed."""

import threading

from zigbee_ninja import alerts
from zigbee_ninja.attribution.chains import ChainTracker
from zigbee_ninja.ingest.engine import LOOP_LAG_WINDOW_SECONDS, LoopLagMonitor


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
