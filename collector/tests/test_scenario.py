"""Scenario engine: §V2-11 physics on recorded traffic (V2_PROPOSAL.md).

Fixtures mirror the REGISTRY's flattened shapes (friendly_name, type,
vendor/model at the top level), never raw Z2M payloads: the registry
contract is what detectors and the engine actually receive.
"""

import json

import pytest

from zigbee_ninja.capacity import ledger, scenario
from zigbee_ninja.store.db import Database
from zigbee_ninja.store.events import RawEventLog

NOW = 1_700_000_000.0
WINDOW = 3600
START = NOW - WINDOW


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
        return sum(
            1 for d in self._devices.get(base, []) if d.get("type") == "Router"
        )

    def is_group(self, base, target):
        return any(
            g.get("friendly_name") == target for g in self._groups.get(base, [])
        )


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
            {"base_topic": "z2m-c", "channel": 15, "version": "2.12.1"},
        ],
        devices={
            "z2m-a": [
                device("lamp_1", "0x01"),
                device("lamp_2", "0x02"),
                device("lamp_3", "0x03"),
                device("sensor_1", "0x04", kind="EndDevice"),
            ],
            "z2m-b": [device("plug_1", "0x0b")],
        },
        groups={
            "z2m-a": [
                {
                    "id": 1,
                    "friendly_name": "grp_a",
                    "member_count": 3,
                    "member_ieee": ["0x01", "0x02", "0x03"],
                }
            ],
            "z2m-b": [],
        },
    )
    return db, events, registry


def add_chains(db, instance, target, verb, count, echoes_per_chain=0):
    conn = db.connect()
    conn.executemany(
        "INSERT INTO chains (instance, target, verb, opened_at, client, "
        "payload_size, echo_count, redundant) VALUES (?, ?, ?, ?, 'ha', 10, ?, 0)",
        [
            (instance, target, verb, START + 10 + i, echoes_per_chain)
            for i in range(count)
        ],
    )
    conn.commit()


def add_autonomous(db, instance, name, publishes):
    conn = db.connect()
    conn.execute(
        "INSERT INTO ledger_device_daily (instance, day, device, publishes, "
        "autonomous_us, provenance) VALUES (?, ?, ?, ?, 0, '')",
        (instance, ledger.utc_day(NOW), name, publishes),
    )
    conn.commit()


def price(db, events, registry, moves, pricing=None, topology=None):
    return scenario.price_scenario(
        events,
        db,
        registry,
        pricing or (lambda base: (None, None)),
        topology or (lambda base: {}),
        moves,
        window_seconds=WINDOW,
        clock=clock,
    )


def move(subject, kind="device", src="z2m-a", dst="z2m-b", resolution=None):
    out = {
        "kind": kind,
        "subject": subject,
        "from_instance": src,
        "to_instance": dst,
    }
    if resolution:
        out["group_resolution"] = resolution
    return out


def test_device_move_relocates_command_and_report_cost(world):
    db, events, registry = world
    add_chains(db, "z2m-a", "sensor_1", "set", 60, echoes_per_chain=1)
    add_autonomous(db, "z2m-a", "sensor_1", 720)

    report = price(db, events, registry, [move("sensor_1")])
    entry = report["moves"][0]
    assert entry["kind"] == "device"
    assert entry["router"] is False
    assert entry["commands"]["chains_per_s"] == pytest.approx(60 / WINDOW, abs=1e-4)
    assert entry["commands"]["before_us_per_s"] > 0
    assert entry["reports"]["us_per_s"] > 0
    assert entry["radio"]["status"] == "unknown"
    assert entry["radio"]["destination_channel"] == 25

    a = report["instances"]["z2m-a"]["steady"]
    b = report["instances"]["z2m-b"]["steady"]
    moved = entry["commands"]["before_us_per_s"] + entry["reports"]["us_per_s"]
    assert a["before_us_per_s"] - a["after_us_per_s"] == pytest.approx(
        moved, abs=0.01
    )
    assert b["after_us_per_s"] - b["before_us_per_s"] == pytest.approx(
        moved, abs=0.01
    )
    # An end-device move never shifts the census.
    assert report["instances"]["z2m-a"]["census"]["routers_after"] == 3
    assert report["instances"]["z2m-b"]["census"]["routers_after"] == 1


def test_router_move_reprices_existing_groupcasts(world):
    db, events, registry = world
    add_chains(db, "z2m-a", "grp_a", "set", 100)
    # lamp_3 carries no traffic of its own; only its router-ness moves.
    report = price(db, events, registry, [move("lamp_3")])

    a = report["instances"]["z2m-a"]
    assert a["census"]["routers_before"] == 3
    assert a["census"]["routers_after"] == 2
    # Losing a router makes every staying groupcast cheaper: negative term.
    assert a["steady"]["second_order_us_per_s"] < 0
    assert report["instances"]["z2m-b"]["census"]["routers_after"] == 2
    # lamp_3 is a member of grp_a, so the move is also a split.
    assert report["splits"] and report["splits"][0]["group"] == "grp_a"


def test_group_move_travels_members_and_prices_destination_census(world):
    db, events, registry = world
    add_chains(db, "z2m-a", "grp_a", "set", 100)
    report = price(db, events, registry, [move("grp_a", kind="group")])

    entry = next(m for m in report["moves"] if m["kind"] == "group")
    assert sorted(entry["members"]) == ["lamp_1", "lamp_2", "lamp_3"]
    # No split: the group moved whole.
    assert report["splits"] == []
    # All three routers travel: destination census grows, source shrinks.
    assert report["instances"]["z2m-a"]["census"]["routers_after"] == 0
    assert report["instances"]["z2m-b"]["census"]["routers_after"] == 4
    # Destination census (4 routers) is larger than the source's (3), so the
    # relocated groupcast traffic costs more there.
    assert entry["commands"]["after_us_per_s"] > entry["commands"]["before_us_per_s"]


def test_subset_move_models_both_resolutions(world):
    db, events, registry = world
    add_chains(db, "z2m-a", "grp_a", "set", 100, echoes_per_chain=3)
    report = price(db, events, registry, [move("lamp_1")])

    split = report["splits"][0]
    assert split["group"] == "grp_a"
    assert split["movers"] == ["lamp_1"]
    assert split["stayers"] == 2
    assert split["applied_resolution"] == scenario.RESOLUTION_UNICASTS
    both = split["added_us_per_s"]
    assert set(both) == {"unicasts", "new_group"}
    assert both["unicasts"] > 0 and both["new_group"] > 0

    explicit = price(
        db, events, registry, [move("lamp_1", resolution="new_group")]
    )
    assert explicit["splits"][0]["applied_resolution"] == "new_group"


def test_burst_overlay_recomposes_t0_peaks(world):
    db, events, registry = world
    # A 10-events-in-one-second burst on sensor_1 plus background traffic.
    for i in range(10):
        kind = "command" if i % 2 else "state"
        target = "sensor_1/set" if kind == "command" else "sensor_1"
        events.record(START + 100 + i * 0.05, "mqtt", "z2m-a", kind, "in", target, 10)
    for i in range(20):
        events.record(START + 200 + i * 10.0, "mqtt", "z2m-a", "state", "in", "lamp_1", 10)
    events.flush()

    report = price(db, events, registry, [move("sensor_1")])
    a_burst = report["instances"]["z2m-a"]["burst"]
    b_burst = report["instances"]["z2m-b"]["burst"]
    assert a_burst["before_peak_1s"]["eps_1s"] == 10.0
    # The moved device's burst leaves z2m-a and lands on z2m-b.
    assert a_burst["after_peak_1s"]["eps_1s"] <= 2.0
    assert b_burst["after_peak_1s"]["eps_1s"] == 10.0
    assert b_burst["verdict"] == "no_limits"
    assert b_burst["provenance"] == scenario.OVERLAY_PROVENANCE


def test_burst_verdict_judges_against_limits(world):
    db, events, registry = world
    detail = {
        "plan": {"mode": "spread", "target": "4 routers (spread)"},
        "knee": {"eps": 8.0, "censored": False, "breach": "saturated"},
        "steps": [{"achieved_eps": 12.0}],
        "environment": {"z2m_version": "2.12.1"},
    }
    conn = db.connect()
    conn.execute(
        "INSERT INTO calibrations (instance, target, started_at, finished_at, "
        "status, knee_eps, detail) VALUES ('z2m-b', 't', ?, ?, 'completed', 8.0, ?)",
        (START - 7200, START - 7100, json.dumps(detail)),
    )
    conn.commit()
    for i in range(10):
        events.record(START + 100 + i * 0.05, "mqtt", "z2m-a", "state", "in", "sensor_1", 10)
    events.flush()

    report = price(db, events, registry, [move("sensor_1")])
    burst = report["instances"]["z2m-b"]["burst"]
    limits = report["instances"]["z2m-b"]["limits"]
    assert limits["sustained_eps"] == 8.0
    assert limits["ceiling_eps"] == 12.0
    assert limits["stale_environment"] is False
    assert burst["after_peak_1s"]["eps_1s"] == 10.0
    assert burst["verdict"] == "above_sustained"


def test_validation_rejects_bad_moves(world):
    db, events, registry = world
    with pytest.raises(scenario.ScenarioError):
        price(db, events, registry, [])
    with pytest.raises(scenario.ScenarioError):
        price(db, events, registry, [move("nope")])
    with pytest.raises(scenario.ScenarioError):
        price(db, events, registry, [move("lamp_1", dst="z2m-a")])
    with pytest.raises(scenario.ScenarioError):
        price(db, events, registry, [move("lamp_1", src="z2m-x")])
    with pytest.raises(scenario.ScenarioError):
        price(
            db,
            events,
            registry,
            [move("lamp_1", resolution="teleport")],
        )
    # One device ordered to two different destinations is contradictory:
    # lamp_1 moves alone to z2m-b but travels with its group to z2m-c.
    with pytest.raises(scenario.ScenarioError):
        price(
            db,
            events,
            registry,
            [move("lamp_1"), move("grp_a", kind="group", dst="z2m-c")],
        )


def test_channel_pool_reports_shared_channels(world):
    db, events, registry = world
    registry._instances[1]["channel"] = 20  # collide with z2m-a
    report = price(db, events, registry, [move("sensor_1")])
    assert report["channel_pools"]
    pool = report["channel_pools"][0]
    assert pool["channel"] == 20
    assert pool["instances"] == ["z2m-a", "z2m-b"]


def test_radio_context_reads_topology_links(world):
    db, events, registry = world
    entry = {
        "raw": {
            "nodes": [],
            "links": [
                {"sourceIeeeAddr": "0x04", "targetIeeeAddr": "0x00", "lqi": 133},
                {
                    "source": {"ieeeAddr": "0x04"},
                    "target": {"ieeeAddr": "0x01"},
                    "linkquality": 210,
                },
            ],
        }
    }
    report = price(
        db, events, registry, [move("sensor_1")], topology=lambda base: entry
    )
    radio = report["moves"][0]["radio"]
    assert radio["best_observed_link_lqi"] == 210
    assert radio["provenance"] == scenario.RADIO_PROVENANCE
