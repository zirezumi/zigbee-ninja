"""Rebalancing advisor: §V2-5 detector 5 judged through the scenario
engine (V2_PROPOSAL.md §V2-11).

Fixtures mirror the REGISTRY's flattened shapes (friendly_name, type,
vendor/model at the top level), never raw Z2M payloads.
"""

import json

import pytest

from zigbee_ninja.recommend import rebalance
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.store.db import Database
from zigbee_ninja.store.events import RawEventLog

NOW = 1_700_000_000.0
LOOKBACK = 24 * 3600.0
START = NOW - LOOKBACK


def clock() -> float:
    return NOW


class FakeRegistry:
    def __init__(self, instances, devices, groups):
        self._instances = instances
        self._devices = devices
        self._groups = groups

    def snapshot(self):
        return self._instances

    def devices(self, base):
        return self._devices.get(base, [])

    def groups(self, base):
        return self._groups.get(base, [])

    def router_count_for(self, base):
        return sum(1 for d in self._devices.get(base, []) if d.get("type") == "Router")

    def is_group(self, base, target):
        return any(g.get("friendly_name") == target for g in self._groups.get(base, []))

    def group_members(self, base, target):
        names = {d["ieee_address"]: d["friendly_name"] for d in self.devices(base)}
        for group in self._groups.get(base, []):
            if group.get("friendly_name") == target:
                return [names[i] for i in group.get("member_ieee", []) if i in names]
        return []


def device(name, ieee, kind="Router"):
    return {
        "ieee_address": ieee,
        "friendly_name": name,
        "type": kind,
        "power_source": "Mains (single phase)",
        "vendor": "Acme",
        "model": "Bulb 9000",
        "network_address": abs(hash(name)) % 65000,
        "get_attribute": "state",
        "published_measurements": [],
        "binding_count": 0,
    }


def add_calibration(db, instance, knee_eps, achieved, z2m_version="2.12.1"):
    detail = {
        "plan": {"mode": "spread", "target": "routers (spread)"},
        "knee": {"eps": knee_eps, "censored": False, "breach": "saturated"},
        "steps": [{"achieved_eps": achieved}],
        "environment": {"z2m_version": z2m_version},
    }
    db.connect().execute(
        "INSERT INTO calibrations (instance, target, started_at, finished_at, "
        "status, knee_eps, detail) VALUES (?, 't', ?, ?, 'completed', ?, ?)",
        (instance, START - 7200, START - 7100, knee_eps, json.dumps(detail)),
    )
    db.connect().commit()


def add_burst(db, events, instance, target, at, count, spacing=0.05):
    """One recorded burst: T0 command events (the judged stream) plus the
    matching chains rows (candidate ranking and steady pricing)."""
    conn = db.connect()
    for i in range(count):
        ts = at + i * spacing
        events.record(ts, "mqtt", instance, "command", "in", f"{target}/set", 10)
        conn.execute(
            "INSERT INTO chains (instance, target, verb, opened_at, client, "
            "payload_size, echo_count, redundant) VALUES (?, ?, 'set', ?, 'ha', 10, 0, 0)",
            (instance, target, ts),
        )
    conn.commit()


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path)
    events = RawEventLog(tmp_path, clock=clock)
    conn = db.connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('ledger_since', ?)",
        (json.dumps(START - 86400),),
    )
    conn.commit()
    registry = FakeRegistry(
        instances=[
            {"base_topic": "z2m-a", "channel": 20, "version": "2.12.1"},
            {"base_topic": "z2m-b", "channel": 25, "version": "2.12.1"},
        ],
        devices={
            "z2m-a": [
                device("hot_1", "0x01"),
                device("hot_2", "0x02"),
                device("lamp_1", "0x03"),
                device("lamp_2", "0x04"),
            ],
            "z2m-b": [device("plug_1", "0x0b")],
        },
        groups={
            "z2m-a": [
                {
                    "id": 1,
                    "friendly_name": "grp_a",
                    "member_count": 2,
                    "member_ieee": ["0x03", "0x04"],
                }
            ],
            "z2m-b": [],
        },
    )
    return db, events, registry


def context(db, events, registry):
    return DetectorContext(
        conn=db.connect(),
        now=NOW,
        lookback_seconds=LOOKBACK,
        instances=[i["base_topic"] for i in registry.snapshot()],
        instance_info={i["base_topic"]: i for i in registry.snapshot()},
        knees={},
        is_group=registry.is_group,
        group_members=registry.group_members,
        groups=registry.groups,
        devices=registry.devices,
        router_count_for=registry.router_count_for,
        pricing=lambda base: (None, None),
        db=db,
        registry=registry,
        events_log=events,
        topology_latest=lambda base: {},
    )


def test_no_pressure_no_findings(world):
    db, events, registry = world
    add_calibration(db, "z2m-a", 8.0, 12.0)
    add_calibration(db, "z2m-b", 8.0, 12.0)
    add_burst(db, events, "z2m-a", "hot_1", START + 100, 3)
    events.flush()
    assert rebalance.detect(context(db, events, registry)) == []


def test_split_load_proposes_smallest_clearing_move(world):
    db, events, registry = world
    add_calibration(db, "z2m-a", 8.0, 12.0)
    add_calibration(db, "z2m-b", 8.0, 12.0)
    # Two subjects interleaved in the same second: peak 12/s against the
    # 8/s limit. Moving one subject halves the coincident peak on both ends.
    for repeat in range(3):
        at = START + 100 + repeat * 600
        add_burst(db, events, "z2m-a", "hot_1", at, 6, spacing=0.1)
        add_burst(db, events, "z2m-a", "hot_2", at + 0.05, 6, spacing=0.1)
    events.flush()

    findings = rebalance.detect(context(db, events, registry))
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "rebalancing"
    assert finding.instance == "z2m-a"
    assert finding.subject == "z2m-a"
    assert finding.action["kind"] == "rebalance"
    assert len(finding.action["moves"]) == 1
    move = finding.action["moves"][0]
    assert move["kind"] == "device"
    assert move["subject"] in ("hot_1", "hot_2")
    assert move["to_instance"] == "z2m-b"
    assert finding.confidence == "medium"
    kinds = {entry["kind"] for entry in finding.evidence}
    assert {"burst_pressure", "capacity_limit", "scenario", "radio"} <= kinds
    scenario_evidence = next(e for e in finding.evidence if e["kind"] == "scenario")
    assert scenario_evidence["source_verdict_after"] in ("ok", "near_sustained")
    assert scenario_evidence["dest_verdict_after"] in ("ok", "near_sustained")
    assert finding.saving["provenance"] == "modeled"


def test_burst_that_merely_relocates_is_not_a_finding(world):
    db, events, registry = world
    add_calibration(db, "z2m-a", 8.0, 12.0)
    add_calibration(db, "z2m-b", 8.0, 12.0)
    # One subject carries the whole 10/s peak: moving it hands the same
    # above-limit burst to the destination, which the guard rejects.
    for repeat in range(3):
        add_burst(db, events, "z2m-a", "hot_1", START + 100 + repeat * 600, 10)
    events.flush()
    assert rebalance.detect(context(db, events, registry)) == []


def test_group_target_moves_as_a_group(world):
    db, events, registry = world
    add_calibration(db, "z2m-a", 8.0, 12.0)
    add_calibration(db, "z2m-b", 8.0, 12.0)
    for repeat in range(3):
        at = START + 100 + repeat * 600
        add_burst(db, events, "z2m-a", "grp_a", at, 6, spacing=0.1)
        add_burst(db, events, "z2m-a", "hot_1", at + 0.05, 6, spacing=0.1)
    events.flush()

    findings = rebalance.detect(context(db, events, registry))
    assert len(findings) == 1
    move = findings[0].action["moves"][0]
    assert move["kind"] in ("group", "device")
    if move["kind"] == "group":
        assert move["subject"] == "grp_a"


def test_stale_environment_downgrades_confidence(world):
    db, events, registry = world
    add_calibration(db, "z2m-a", 8.0, 12.0, z2m_version="2.10.1")
    add_calibration(db, "z2m-b", 8.0, 12.0)
    for repeat in range(3):
        at = START + 100 + repeat * 600
        add_burst(db, events, "z2m-a", "hot_1", at, 6, spacing=0.1)
        add_burst(db, events, "z2m-a", "hot_2", at + 0.05, 6, spacing=0.1)
    events.flush()

    findings = rebalance.detect(context(db, events, registry))
    assert len(findings) == 1
    assert findings[0].confidence == "low"
    assert "recalibrat" in findings[0].finding


def test_rare_pressure_downgrades_confidence(world):
    db, events, registry = world
    add_calibration(db, "z2m-a", 8.0, 12.0)
    add_calibration(db, "z2m-b", 8.0, 12.0)
    at = START + 100
    add_burst(db, events, "z2m-a", "hot_1", at, 6, spacing=0.1)
    add_burst(db, events, "z2m-a", "hot_2", at + 0.05, 6, spacing=0.1)
    events.flush()

    findings = rebalance.detect(context(db, events, registry))
    assert len(findings) == 1
    assert findings[0].confidence == "low"
    assert "rare event" in findings[0].finding


def test_without_scenario_dependencies_detector_degrades(world):
    db, events, registry = world
    ctx = context(db, events, registry)
    ctx.events_log = None
    assert rebalance.detect(ctx) == []
