from zigbee_ninja.attribution.chains import ChainTracker, parse_command


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_parse_command_forms():
    assert parse_command("kitchen_light/set") == ("kitchen_light", "set")
    assert parse_command("kitchen_light/get") == ("kitchen_light", "get")
    assert parse_command("kitchen_light/set/state") == ("kitchen_light", "set")
    assert parse_command("nested/name/set") == ("nested/name", "set")
    assert parse_command("kitchen_light") is None
    assert parse_command("bridge/info") is None


def test_echo_within_window_is_provoked():
    clock = FakeClock()
    tracker = ChainTracker(clock=clock)
    tracker.on_command("z2m-test", "lamp", "set", b'{"state":"ON"}')
    clock.now += 0.3
    assert tracker.on_state("z2m-test", "lamp") == "provoked"

    clock.now += 10
    chains = tracker.drain_finalized()
    assert len(chains) == 1
    assert chains[0].echoes == 1
    assert 290 <= chains[0].first_echo_ms <= 310


def test_echo_after_window_is_autonomous():
    clock = FakeClock()
    tracker = ChainTracker(clock=clock)
    tracker.on_command("z2m-test", "lamp", "set", b"x")
    clock.now += 5.0  # beyond set window (1.5s)
    assert tracker.on_state("z2m-test", "lamp") == "autonomous"


def test_unrelated_state_is_autonomous():
    tracker = ChainTracker(clock=FakeClock())
    tracker.on_command("z2m-test", "lamp", "set", b"x")
    assert tracker.on_state("z2m-test", "other_device") == "autonomous"


def test_group_command_claims_member_echoes():
    def resolve(instance, target):
        return ["bulb_1", "bulb_2"] if target == "kitchen_group" else []

    clock = FakeClock()
    tracker = ChainTracker(resolve_members=resolve, clock=clock)
    tracker.on_command("z2m-test", "kitchen_group", "set", b'{"state":"ON"}')
    clock.now += 0.2
    assert tracker.on_state("z2m-test", "bulb_1") == "provoked"
    assert tracker.on_state("z2m-test", "bulb_2") == "provoked"

    clock.now += 10
    chains = tracker.drain_finalized()
    assert chains[0].echoes == 2


def test_redundant_same_payload_within_window():
    clock = FakeClock()
    tracker = ChainTracker(clock=clock)
    first = tracker.on_command("z2m-test", "lamp", "set", b'{"state":"ON"}')
    clock.now += 2.0
    second = tracker.on_command("z2m-test", "lamp", "set", b'{"state":"ON"}')
    clock.now += 2.0
    different = tracker.on_command("z2m-test", "lamp", "set", b'{"state":"OFF"}')

    assert first.redundant is False
    assert second.redundant is True
    assert different.redundant is False


def test_redundant_outside_window_not_flagged():
    clock = FakeClock()
    tracker = ChainTracker(clock=clock)
    tracker.on_command("z2m-test", "lamp", "set", b"same")
    clock.now += 30.0
    later = tracker.on_command("z2m-test", "lamp", "set", b"same")
    assert later.redundant is False


def test_chain_records_the_loop_skew_it_was_stamped_under():
    clock = FakeClock()
    skew = [0.0]
    tracker = ChainTracker(clock=clock, loop_skew_ms=lambda: skew[0])
    quiet = tracker.on_command("z2m-test", "lamp", "set", b"a")
    assert quiet.clock_skew_ms == 0.0

    skew[0] = 4200.0
    stalled = tracker.on_command("z2m-test", "other", "set", b"a")
    assert stalled.clock_skew_ms == 4200.0


def test_loop_stall_does_not_manufacture_a_duplicate():
    """Two identical commands drained together after a stall are not a duplicate.

    `opened_at` is processing time, so a stall collapses the observed gap: the
    pair looks simultaneous even when it really arrived seconds apart. The gap
    has to clear the window with the skew added on before redundancy is claimed.
    """
    clock = FakeClock()
    skew = [0.0]
    tracker = ChainTracker(clock=clock, loop_skew_ms=lambda: skew[0])
    tracker.on_command("z2m-test", "lamp", "set", b"same")

    # The loop stalls for 6 s; the backlog drains with no observable gap.
    skew[0] = 6000.0
    clock.now += 0.01
    drained = tracker.on_command("z2m-test", "lamp", "set", b"same")
    assert drained.redundant is False


def test_healthy_loop_leaves_the_redundancy_test_unchanged():
    """Sub-millisecond lag must not shift the old behaviour either way."""
    clock = FakeClock()
    tracker = ChainTracker(clock=clock, loop_skew_ms=lambda: 0.5)
    tracker.on_command("z2m-test", "lamp", "set", b"same")
    clock.now += 2.0
    inside = tracker.on_command("z2m-test", "lamp", "set", b"same")
    assert inside.redundant is True

    clock.now += 30.0
    outside = tracker.on_command("z2m-test", "lamp", "set", b"same")
    assert outside.redundant is False


def test_client_backfill_attribution():
    clock = FakeClock()
    tracker = ChainTracker(clock=clock)
    chain = tracker.on_command("z2m-test", "lamp", "set", b"x")
    assert chain.client is None
    assert tracker.attribute_client("z2m-test", "lamp", "ha-core") is True
    assert chain.client == "ha-core"
    # already attributed → no unattributed chain remains
    assert tracker.attribute_client("z2m-test", "lamp", "other") is False


def test_finalized_drain_is_once():
    clock = FakeClock()
    tracker = ChainTracker(clock=clock)
    tracker.on_command("z2m-test", "lamp", "set", b"x")
    clock.now += 10
    assert len(tracker.drain_finalized()) == 1
    assert tracker.drain_finalized() == []
    assert tracker.open_count() == 0
