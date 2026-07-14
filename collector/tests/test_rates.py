from zigbee_ninja.ingest.rates import RateTracker, classify


def test_classify_taxonomy():
    base = "z2m-test"
    assert classify("z2m-test/kitchen_light/set", base) == "command"
    assert classify("z2m-test/kitchen_light/get", base) == "command"
    assert classify("z2m-test/bridge/info", base) == "bridge"
    assert classify("z2m-test/kitchen_light/availability", base) == "availability"
    assert classify("z2m-test/zigbee-ninja/probe/events", base) == "probe"
    assert classify("z2m-test/kitchen_light", base) == "state"


def test_classify_multilevel_base():
    assert classify("home/z2m/lamp/set", "home/z2m") == "command"
    assert classify("home/z2m/lamp", "home/z2m") == "state"


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_rate_snapshot_counts_last_complete_second():
    clock = FakeClock(1000.0)
    tracker = RateTracker(clock=clock)
    tracker.record("z2m-test", "state")
    tracker.record("z2m-test", "state")
    tracker.record("z2m-test", "command")
    clock.now = 1001.0  # second 1000 is now complete

    snapshot = tracker.snapshot()
    assert snapshot["z2m-test"]["state"] == 2
    assert snapshot["z2m-test"]["command"] == 1
    assert snapshot["z2m-test"]["total_60s"] == 3


def test_drain_returns_each_window_exactly_once():
    clock = FakeClock(1000.0)  # aligned to a 10s boundary
    tracker = RateTracker(clock=clock)
    tracker.record("z2m-test", "state")
    clock.now = 1005.0
    tracker.record("z2m-test", "state")
    clock.now = 1012.0  # window [1000, 1010) is complete

    rows = tracker.drain_completed_windows()
    assert rows == [(1000, "z2m-test", "state", 2)]
    assert tracker.drain_completed_windows() == []  # watermark advanced

    clock.now = 1021.0
    tracker.record("z2m-test", "command")
    clock.now = 1030.0
    rows = tracker.drain_completed_windows()
    assert (1020, "z2m-test", "command", 1) in rows
