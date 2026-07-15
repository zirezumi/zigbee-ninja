"""Alert evaluator state machine, seeding, and delta handling (DESIGN.md §14)."""

import pytest

from zigbee_ninja.alerts import SEED_RULES, AlertManager
from zigbee_ninja.store.config import ConfigStore
from zigbee_ninja.store.db import Database


class Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Harness:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path)
        self.config = ConfigStore(self.db)
        self.values: dict[str, dict[str, float | None]] = {}
        self.clock = Clock()
        self.manager = self._new_manager()

    def _new_manager(self) -> AlertManager:
        return AlertManager(self.db, self.config, provider=self._provide, clock=self.clock)

    def _provide(self, names: set[str]) -> dict[str, dict[str, float | None]]:
        return {metric: dict(per) for metric, per in self.values.items() if metric in names}

    def restart(self) -> AlertManager:
        self.manager = self._new_manager()
        return self.manager

    def tick_after(self, seconds: float = 0.0) -> None:
        self.clock.advance(seconds)
        self.manager.tick()


@pytest.fixture()
def harness(tmp_path):
    return Harness(tmp_path)


def make_rule(harness, **overrides):
    data = {
        "name": "test rule",
        "metric": "wire_p95_ms",
        "instance": "*",
        "op": ">",
        "threshold": 500.0,
        "clear_threshold": 300.0,
        "sustain_seconds": 30,
        "severity": "warning",
        "enabled": True,
    }
    data.update(overrides)
    return harness.manager.create_rule(data)


def test_seeding_is_idempotent_and_deletions_are_durable(harness):
    rules = harness.manager.rules()
    assert len(rules) == len(SEED_RULES)
    enabled = {rule["builtin"] for rule in rules if rule["enabled"]}
    assert enabled == {"probe_stale", "tap_down", "broker_down", "ha_down", "layout_mismatch"}

    # A second manager on the same store must not duplicate the seeds.
    harness.restart()
    assert len(harness.manager.rules()) == len(SEED_RULES)

    # Deleting a built-in is durable: the seed never comes back.
    layout = next(r for r in rules if r["builtin"] == "layout_mismatch")
    assert harness.manager.delete_rule(layout["id"])
    harness.restart()
    assert len(harness.manager.rules()) == len(SEED_RULES) - 1
    assert not any(r["builtin"] == "layout_mismatch" for r in harness.manager.rules())


def test_opens_after_sustain_and_clears_on_hysteresis(harness):
    make_rule(harness)  # >500 open, <=300 clear, sustain 30
    harness.values["wire_p95_ms"] = {"z2m-1": 600.0}

    harness.tick_after(0)
    assert harness.manager.active() == []  # breach seen, sustain not met
    harness.tick_after(10)
    harness.tick_after(10)
    assert harness.manager.active() == []
    harness.tick_after(10)  # 30 s of continuous breach
    active = harness.manager.active()
    assert len(active) == 1
    assert active[0]["instance"] == "z2m-1"
    assert active[0]["value"] == 600.0

    # The middle zone (below threshold, above clear) holds the alert open.
    harness.values["wire_p95_ms"] = {"z2m-1": 400.0}
    for _ in range(20):
        harness.tick_after(10)
    assert len(harness.manager.active()) == 1

    # OK side must hold for max(sustain, 60 s) before the event clears.
    harness.values["wire_p95_ms"] = {"z2m-1": 200.0}
    harness.tick_after(10)
    harness.tick_after(30)
    assert len(harness.manager.active()) == 1  # only 30 s of OK so far
    harness.tick_after(40)  # 70 s of OK
    assert harness.manager.active() == []

    events = harness.manager.history(seconds=86400)
    assert len(events) == 1
    assert events[0]["cleared_at"] is not None
    assert events[0]["peak_value"] == 600.0
    assert events[0]["context"]["value_at_open"] == 600.0


def test_interrupted_breach_resets_sustain(harness):
    make_rule(harness)
    harness.values["wire_p95_ms"] = {"z2m-1": 600.0}
    harness.tick_after(0)
    harness.tick_after(10)
    harness.values["wire_p95_ms"] = {"z2m-1": 100.0}  # recovers before sustain
    harness.tick_after(10)
    harness.values["wire_p95_ms"] = {"z2m-1": 600.0}
    harness.tick_after(10)
    harness.tick_after(10)
    harness.tick_after(10)
    assert harness.manager.active() == []  # the breach clock restarted at 20 s
    harness.tick_after(10)  # 30 s since the new breach began
    assert len(harness.manager.active()) == 1


def test_peak_tracks_the_most_extreme_value(harness):
    make_rule(harness, sustain_seconds=0)
    harness.values["wire_p95_ms"] = {"z2m-1": 600.0}
    harness.tick_after(0)
    harness.values["wire_p95_ms"] = {"z2m-1": 900.0}
    harness.tick_after(10)
    harness.values["wire_p95_ms"] = {"z2m-1": 700.0}
    harness.tick_after(10)
    assert harness.manager.active()[0]["peak"] == 900.0


def test_counter_metric_baselines_then_alerts_on_delta(harness):
    # Remove the seeded layout_mismatch built-in so only the test rule fires.
    seed = next(r for r in harness.manager.rules() if r["builtin"] == "layout_mismatch")
    assert harness.manager.delete_rule(seed["id"])
    make_rule(
        harness,
        name="mismatch",
        metric="layout_mismatch_delta",
        threshold=0.0,
        clear_threshold=None,
        sustain_seconds=0,
        severity="critical",
    )
    # First sight of a nonzero cumulative total is a baseline, not an alert.
    harness.values["layout_mismatch_delta"] = {"z2m-1": 5.0}
    harness.tick_after(0)
    assert harness.manager.active() == []

    harness.values["layout_mismatch_delta"] = {"z2m-1": 7.0}  # +2 this tick
    harness.tick_after(10)
    assert len(harness.manager.active()) == 1

    # Quiet counters clear after the 60 s clear floor.
    harness.tick_after(10)
    harness.tick_after(30)
    assert len(harness.manager.active()) == 1
    harness.tick_after(30)
    assert harness.manager.active() == []

    # A cumulative decrease (collector restart) rebaselines silently.
    harness.values["layout_mismatch_delta"] = {"z2m-1": 3.0}
    harness.tick_after(10)
    assert harness.manager.active() == []


def test_global_metric_reports_under_star(harness):
    make_rule(
        harness,
        name="tap",
        metric="tap_agents",
        op="<",
        threshold=1.0,
        clear_threshold=None,
        sustain_seconds=0,
    )
    harness.values["tap_agents"] = {"*": 0.0}
    harness.tick_after(0)
    active = harness.manager.active()
    assert len(active) == 1
    assert active[0]["instance"] == "*"

    harness.values["tap_agents"] = {"*": 1.0}
    harness.tick_after(10)
    harness.tick_after(60)
    assert harness.manager.active() == []


def test_instance_pinned_rule_ignores_other_instances(harness):
    make_rule(harness, instance="z2m-2", sustain_seconds=0)
    harness.values["wire_p95_ms"] = {"z2m-1": 900.0, "z2m-2": 100.0}
    harness.tick_after(0)
    assert harness.manager.active() == []
    harness.values["wire_p95_ms"] = {"z2m-1": 100.0, "z2m-2": 900.0}
    harness.tick_after(10)
    assert [a["instance"] for a in harness.manager.active()] == ["z2m-2"]


def test_missing_data_freezes_state(harness):
    make_rule(harness, sustain_seconds=0)
    harness.values["wire_p95_ms"] = {"z2m-1": 900.0}
    harness.tick_after(0)
    assert len(harness.manager.active()) == 1

    # No data: the event neither clears nor duplicates.
    harness.values["wire_p95_ms"] = {}
    for _ in range(30):
        harness.tick_after(10)
    assert len(harness.manager.active()) == 1

    harness.values["wire_p95_ms"] = {"z2m-1": 100.0}
    harness.tick_after(10)
    harness.tick_after(60)
    assert harness.manager.active() == []


def test_open_events_reattach_across_restart(harness):
    rule = make_rule(harness, sustain_seconds=0)
    harness.values["wire_p95_ms"] = {"z2m-1": 900.0}
    harness.tick_after(0)
    event_id = harness.manager.active()[0]["event_id"]

    manager = harness.restart()
    active = manager.active()
    assert [a["event_id"] for a in active] == [event_id]
    assert active[0]["name"] == rule["name"]

    # Clearing still requires a sustained OK reading after the restart.
    harness.values["wire_p95_ms"] = {"z2m-1": 100.0}
    harness.tick_after(10)
    assert len(manager.active()) == 1
    harness.tick_after(60)
    assert manager.active() == []


def test_disabling_or_deleting_a_rule_closes_its_events(harness):
    rule = make_rule(harness, sustain_seconds=0)
    harness.values["wire_p95_ms"] = {"z2m-1": 900.0}
    harness.tick_after(0)
    assert len(harness.manager.active()) == 1

    harness.manager.update_rule(rule["id"], {**rule, "enabled": False})
    assert harness.manager.active() == []
    events = harness.manager.history(seconds=86400)
    assert events[0]["cleared_at"] is not None
    assert events[0]["context"]["closed"] == "rule disabled"

    harness.manager.update_rule(rule["id"], {**rule, "enabled": True})
    harness.tick_after(10)  # re-opens: the condition still holds
    assert len(harness.manager.active()) == 1
    assert harness.manager.delete_rule(rule["id"])
    assert harness.manager.active() == []


def test_validation_rejects_bad_rules(harness):
    with pytest.raises(ValueError):
        make_rule(harness, metric="not_a_metric")
    with pytest.raises(ValueError):
        make_rule(harness, op="=")
    with pytest.raises(ValueError):
        make_rule(harness, severity="apocalyptic")
    with pytest.raises(ValueError):
        make_rule(harness, clear_threshold=600.0)  # wrong side for op '>'
    with pytest.raises(ValueError):
        make_rule(harness, metric="tap_agents", op="<", instance="z2m-1")
    with pytest.raises(ValueError):
        make_rule(harness, sustain_seconds=999999)
    with pytest.raises(ValueError):
        make_rule(harness, name="")
