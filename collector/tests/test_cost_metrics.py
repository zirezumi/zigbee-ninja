"""Cost baselines, regression metrics, and their ride on the alert evaluator
(V2_PROPOSAL.md §V2-4)."""

import calendar
import time

import pytest

from zigbee_ninja.alerts import AlertManager
from zigbee_ninja.capacity import ledger
from zigbee_ninja.store.config import ConfigStore
from zigbee_ninja.store.db import Database

# A fixed mid-day moment: 2026-07-16 12:00 UTC. Half of yesterday still sits
# inside the trailing 24 h window, so its weight is exactly 0.5.
NOW = calendar.timegm(time.strptime("2026-07-16", "%Y-%m-%d")) + 43200.0


def day(offset: int) -> str:
    return ledger.utc_day(NOW - offset * 86400.0)


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path)


def seed_commander(db, commander, per_day_us, instance="z2m-a"):
    conn = db.connect()
    conn.executemany(
        "INSERT INTO ledger_daily (instance, day, commander, chains, tx_us, rx_us) "
        "VALUES (?, ?, ?, 1, ?, 0) "
        "ON CONFLICT(instance, day, commander) DO UPDATE SET tx_us = tx_us + excluded.tx_us",
        [(instance, d, commander, us) for d, us in per_day_us.items()],
    )
    conn.commit()


def seed_device(db, device, per_day_us, instance="z2m-a"):
    conn = db.connect()
    conn.executemany(
        "INSERT INTO ledger_device_daily (instance, day, device, publishes, autonomous_us) "
        "VALUES (?, ?, ?, 1, ?)",
        [(instance, d, device, us) for d, us in per_day_us.items()],
    )
    conn.commit()


def set_recording_since(db, ts):
    ConfigStore(db).set("ledger_since", ts)


def test_ratio_is_trailing_24h_over_14_day_median(db):
    set_recording_since(db, NOW - 20 * 86400.0)
    history = {day(k): 1000.0 for k in range(1, 15)}
    history[day(0)] = 3000.0  # today runs hot
    seed_commander(db, "automation: X", history)

    metrics = ledger.cost_metrics(db, NOW)
    # trailing = 3000 (today) + 1000 x 0.5 (yesterday's remaining half) = 3500
    assert metrics["commander_cost_ratio"]["automation: X"] == pytest.approx(3.5)
    assert metrics["commander_cost_us_per_s"]["automation: X"] == pytest.approx(
        3500.0 / 86400.0, abs=1e-3
    )
    assert metrics["instance_cost_us_per_s"]["z2m-a"] == pytest.approx(
        3500.0 / 86400.0, abs=1e-3
    )


def test_ratio_gates_on_history_and_zero_median(db):
    # Only two completed recording days: below the minimum history, so the
    # regression metric freezes while the budget rate still reports.
    set_recording_since(db, NOW - 2.5 * 86400.0)
    seed_commander(db, "automation: young", {day(0): 500.0, day(1): 500.0})
    metrics = ledger.cost_metrics(db, NOW)
    assert "commander_cost_ratio" not in metrics
    assert metrics["commander_cost_us_per_s"]["automation: young"] > 0

    # Plenty of history but a zero median: a spender silent for most of the
    # baseline window has nothing to regress against.
    set_recording_since(db, NOW - 20 * 86400.0)
    seed_commander(db, "automation: sparse", {day(0): 900.0, day(1): 900.0})
    metrics = ledger.cost_metrics(db, NOW)
    assert "automation: sparse" not in metrics.get("commander_cost_ratio", {})
    assert metrics["commander_cost_us_per_s"]["automation: sparse"] > 0


def test_self_commander_and_cross_instance_aggregation(db):
    set_recording_since(db, NOW - 20 * 86400.0)
    steady = {day(k): 1000.0 for k in range(15)}
    seed_commander(db, ledger.SELF_COMMANDER, steady)
    # The same automation spends on two coordinators; its regression baseline
    # is the aggregate (the automation is the spender, not the instance).
    seed_commander(db, "automation: wide", {day(k): 400.0 for k in range(15)}, "z2m-a")
    seed_commander(db, "automation: wide", {day(k): 600.0 for k in range(15)}, "z2m-b")

    metrics = ledger.cost_metrics(db, NOW)
    assert ledger.SELF_COMMANDER not in metrics["commander_cost_ratio"]
    assert metrics["commander_cost_us_per_s"][ledger.SELF_COMMANDER] > 0
    # 400+600 steady across both: trailing = 1000 + 1000 x 0.5, median 1000.
    assert metrics["commander_cost_ratio"]["automation: wide"] == pytest.approx(1.5)


def test_device_ratio_reports(db):
    set_recording_since(db, NOW - 20 * 86400.0)
    history = {day(k): 200.0 for k in range(1, 15)}
    history[day(0)] = 800.0
    seed_device(db, "chatty_sensor", history)
    metrics = ledger.cost_metrics(db, NOW)
    # trailing = 800 + 200 x 0.5 = 900 over median 200
    assert metrics["device_cost_ratio"]["chatty_sensor"] == pytest.approx(4.5)


def test_regression_rule_rides_the_evaluator(db):
    config = ConfigStore(db)
    samples = {"commander_cost_ratio": {"automation: X": 3.4}}
    clock = {"now": 1000.0}
    manager = AlertManager(
        db, config, provider=lambda names: samples, clock=lambda: clock["now"]
    )
    rule = next(
        r for r in manager.rules() if r["builtin"] == "commander_cost_regression"
    )
    assert not rule["enabled"]
    assert rule["threshold"] == 2.0
    assert rule["sustain_seconds"] == 86400

    manager.update_rule(rule["id"], {**rule, "sustain_seconds": 0, "enabled": True})
    manager.tick()
    active = manager.active()
    assert len(active) == 1
    assert active[0]["instance"] == "automation: X"
    assert active[0]["value"] == 3.4


def test_new_builtins_seed_on_an_already_seeded_install(db):
    config = ConfigStore(db)
    pre_v2 = [
        "avg_tx_high",
        "broker_down",
        "budget_pct",
        "delivery_failures",
        "ha_down",
        "knee_utilization",
        "layout_mismatch",
        "probe_stale",
        "seq_gaps",
        "steady_headroom",
        "tap_down",
        "wire_p95",
    ]
    config.set("alert_rules_seeded", pre_v2)
    manager = AlertManager(db, config, provider=lambda names: {}, clock=time.time)
    builtins = {rule["builtin"]: rule for rule in manager.rules() if rule["builtin"]}
    for name in (
        "commander_cost_regression",
        "device_cost_regression",
        "commander_cost_budget",
        "instance_cost_budget",
    ):
        assert name in builtins
        assert not builtins[name]["enabled"]
    # Only the new arrivals were inserted; the pre-V2 names were respected.
    assert set(config.get("alert_rules_seeded")) >= set(pre_v2)
    assert set(builtins) == {
        "commander_cost_regression",
        "device_cost_regression",
        "commander_cost_budget",
        "instance_cost_budget",
        "collector_loop_lag",
    }


def test_summary_carries_trends_and_instance_rollup(db):
    set_recording_since(db, NOW - 20 * 86400.0)
    history = {day(k): 1000.0 for k in range(1, 15)}
    history[day(0)] = 3000.0
    seed_commander(db, "automation: X", history)
    seed_device(db, "sensor_y", {day(k): 100.0 for k in range(15)})

    view = ledger.summary(db, 86400, now=NOW)
    row = next(r for r in view["commanders"] if r["commander"] == "automation: X")
    assert row["trend"] == pytest.approx(3.5)
    device = next(r for r in view["devices"] if r["device"] == "sensor_y")
    assert device["trend"] == pytest.approx(1.5)
    assert "z2m-a" in view["instances"]
    rollup = view["instances"]["z2m-a"]
    assert rollup["total_us"] == pytest.approx(
        row["total_us"] + device["autonomous_us"], rel=1e-6
    )
    assert rollup["us_per_s"] > 0
