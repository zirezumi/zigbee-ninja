"""Migration manifest export: the §V2-11 ratified contract, frozen at
first ship (V2_PROPOSAL.md). Field names asserted here are the external
contract controller-side tooling builds against; renaming any of them is
a breaking change."""

import pytest

from tests.test_scenario import (
    NOW,
    WINDOW,
    add_autonomous,
    add_chains,
    clock,
    move,
    price,
    world,  # noqa: F401  (pytest fixture import)
)
from zigbee_ninja.capacity import manifest

SETUP = {"username": "admin", "password": "correct-horse"}


def build(db, events, registry, moves, source="simulator"):
    report = price(db, events, registry, moves)
    return manifest.build(report, registry, moves, source, clock=clock)


def test_envelope_and_device_move_contract(world):  # noqa: F811
    db, events, registry = world
    add_chains(db, "z2m-a", "sensor_1", "set", 60, echoes_per_chain=1)
    add_autonomous(db, "z2m-a", "sensor_1", 720)

    doc = build(db, events, registry, [move("sensor_1")])

    # The versioned envelope.
    assert doc["manifest_version"] == manifest.MANIFEST_VERSION
    assert doc["generated_at"] == NOW
    assert doc["source"] == "simulator"
    assert doc["window_seconds"] == WINDOW
    assert "predictions" in doc["basis"]

    # One entry per move, subject identity embedded.
    assert len(doc["moves"]) == 1
    entry = doc["moves"][0]
    assert entry["kind"] == "device"
    assert entry["subject"] == "sensor_1"
    assert entry["ieee"] == "0x04"
    assert entry["from_instance"] == "z2m-a"
    assert entry["to_instance"] == "z2m-b"
    predicted = entry["predicted"]
    assert predicted["commands_before_us_per_s"] > 0
    assert predicted["commands_after_us_per_s"] > 0
    assert predicted["reports_us_per_s"] > 0
    assert predicted["provenance"]
    assert predicted["basis"]
    assert entry["radio"]["status"] == "unknown"

    # Predicted per-instance after-state: the verification receipts.
    for base in ("z2m-a", "z2m-b", "z2m-c"):
        state = doc["predicted_instances"][base]
        assert "steady_after_us_per_s" in state
        assert "steady_after_pct_of_budget" in state
        assert "burst_after_peak_1s_eps" in state
        assert "verdict" in state
        assert "routers_after" in state
    assert doc["predicted_instances"]["z2m-a"]["touched"] is True
    assert doc["predicted_instances"]["z2m-c"]["touched"] is False


def test_group_move_carries_member_identities(world):  # noqa: F811
    db, events, registry = world
    add_chains(db, "z2m-a", "grp_a", "set", 100)
    doc = build(db, events, registry, [move("grp_a", kind="group")])
    entry = doc["moves"][0]
    assert entry["kind"] == "group"
    assert "ieee" not in entry
    members = {m["name"]: m["ieee"] for m in entry["members"]}
    assert members == {"lamp_1": "0x01", "lamp_2": "0x02", "lamp_3": "0x03"}


def test_split_move_records_resolution_and_both_prices(world):  # noqa: F811
    db, events, registry = world
    add_chains(db, "z2m-a", "grp_a", "set", 100, echoes_per_chain=3)
    doc = build(db, events, registry, [move("lamp_1", resolution="new_group")])
    entry = doc["moves"][0]
    assert entry["group_resolution"] == "new_group"
    split = entry["group_split"]
    assert split["group"] == "grp_a"
    assert split["stayers"] == 2
    assert set(split["resolutions_us_per_s"]) == {"unicasts", "new_group"}


def test_build_rejects_unknown_source_and_mismatched_moves(world):  # noqa: F811
    db, events, registry = world
    add_chains(db, "z2m-a", "sensor_1", "set", 10)
    moves = [move("sensor_1")]
    report = price(db, events, registry, moves)
    with pytest.raises(ValueError):
        manifest.build(report, registry, moves, "wishful", clock=clock)
    with pytest.raises(ValueError):
        manifest.build(report, registry, [move("lamp_1")], "simulator", clock=clock)


def test_manifest_endpoint_validates(client):
    client.post("/api/setup", json=SETUP)
    bad_source = client.post(
        "/api/scenario/manifest",
        json={
            "moves": [
                {
                    "kind": "device",
                    "subject": "x",
                    "from_instance": "a",
                    "to_instance": "b",
                }
            ],
            "source": "wishful",
        },
    )
    assert bad_source.status_code == 400
    unknown_instance = client.post(
        "/api/scenario/manifest",
        json={
            "moves": [
                {
                    "kind": "device",
                    "subject": "x",
                    "from_instance": "a",
                    "to_instance": "b",
                }
            ],
        },
    )
    assert unknown_instance.status_code == 400
