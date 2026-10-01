import json

from zigbee_ninja.ingest.registry import Registry

INFO = {
    "version": "2.3.0",
    "coordinator": {
        "type": "EmberZNet",
        "meta": {"ieee_address": "0x00124b00aaaaaaaa", "revision": "8.1.0 [GA]"},
    },
    "network": {"channel": 15, "panID": 4660},
    "config": {"serial": {"port": "tcp://coordinator.example:6638"}},
}

DEVICES = [
    {
        "ieee_address": "0x0000000000000001",
        "friendly_name": "Coordinator",
        "type": "Coordinator",
        "power_source": None,
        "definition": None,
    },
    {
        "ieee_address": "0x0000000000000002",
        "friendly_name": "kitchen_light",
        "type": "Router",
        "power_source": "Mains (single phase)",
        "network_address": 4711,
        "definition": {
            "vendor": "ExampleCo",
            "model": "BULB-1",
            # Composite expose (light) wrapping gettable features, plus a
            # published-only metering property: the §11 preview warns on it.
            "exposes": [
                {
                    "type": "light",
                    "features": [
                        {"property": "state", "access": 7},
                        {"property": "brightness", "access": 7},
                    ],
                },
                {"property": "power", "access": 1},
                {"property": "linkquality", "access": 1},
                # Config enum sharing the "power" stem: NOT a measurement.
                {"property": "power_on_behavior", "access": 7},
                # Underscore-bounded stem match: IS a measurement.
                {"property": "device_temperature", "access": 1},
            ],
        },
        "endpoints": {
            "1": {"bindings": [{"cluster": "genOnOff"}, {"cluster": "genLevelCtrl"}]},
            "2": {"bindings": []},
        },
    },
    {
        "ieee_address": "0x0000000000000003",
        "friendly_name": "door_sensor",
        "type": "EndDevice",
        "power_source": "Battery",
        "definition": {
            "vendor": "ExampleCo",
            "model": "SENSE-2",
            "exposes": [{"property": "contact", "access": 1}],
        },
    },
]

GROUPS = [{"id": 1, "friendly_name": "kitchen", "members": [{"ieee_address": "0x02"}]}]


def feed(registry: Registry, base: str) -> None:
    registry.handle(f"{base}/bridge/info", json.dumps(INFO).encode())
    registry.handle(f"{base}/bridge/devices", json.dumps(DEVICES).encode())
    registry.handle(f"{base}/bridge/groups", json.dumps(GROUPS).encode())
    registry.handle(f"{base}/bridge/state", b'{"state":"online"}')


def test_discovery_from_bridge_topics():
    registry = Registry()
    feed(registry, "z2m-test")

    snapshot = registry.snapshot()
    assert len(snapshot) == 1
    instance = snapshot[0]
    assert instance["base_topic"] == "z2m-test"
    assert instance["version"] == "2.3.0"
    assert instance["channel"] == 15
    assert instance["adapter_port"] == "tcp://coordinator.example:6638"
    assert instance["coordinator_ieee"] == "0x00124b00aaaaaaaa"
    assert instance["device_count"] == 3
    assert instance["router_count"] == 1
    assert instance["end_device_count"] == 1
    assert instance["group_count"] == 1
    assert instance["online"] is True
    assert instance["coordinator_revision"] == "8.1.0 [GA]"

    devices = {device["friendly_name"]: device for device in registry.devices("z2m-test")}
    light = devices["kitchen_light"]
    assert light["get_attribute"] == "state"  # preferred over brightness
    assert light["published_measurements"] == ["device_temperature", "power"]
    assert light["binding_count"] == 2
    assert light["network_address"] == 4711
    assert registry.network_address_for("z2m-test", "kitchen_light") == 4711
    assert registry.network_address_for("z2m-test", "Coordinator") is None  # no nwk field
    assert registry.network_address_for("z2m-test", "nope") is None
    sensor = devices["door_sensor"]
    assert sensor["get_attribute"] is None  # nothing gettable on the contact sensor
    assert devices["Coordinator"]["get_attribute"] is None


def test_multilevel_base_topic_and_base_for():
    registry = Registry()
    feed(registry, "home/z2m")

    assert registry.snapshot()[0]["base_topic"] == "home/z2m"
    assert registry.base_for("home/z2m/kitchen_light/set") == "home/z2m"
    assert registry.base_for("unrelated/topic") is None


def test_plain_state_payload_and_offline():
    registry = Registry()
    registry.handle("z2m-test/bridge/state", b"offline")
    assert registry.snapshot()[0]["online"] is False


def test_junk_payloads_are_ignored():
    registry = Registry()
    registry.handle("z2m-test/bridge/info", b"\x00\xffnot-json")
    registry.handle("z2m-test/bridge/devices", b'{"not": "a list"}')
    # info parse failed → no fields, but instance may not even exist; junk must not raise
    assert registry.base_for("z2m-test/lamp") in (None, "z2m-test")


def test_non_bridge_topics_not_handled():
    registry = Registry()
    assert registry.handle("z2m-test/kitchen_light", b'{"state":"ON"}') is False


# --- group membership resolution -------------------------------------------
# Z2M accepts either the friendly name or the numeric group id in a command
# topic. `is_group` honoured both while `group_members` matched the name alone,
# so a numeric-id topic looked like "not a group" to the no-op detector, which
# then judged the command against the group's own state topic: Z2M's synthetic
# composition of ONE member, and the exact comparison group expansion exists to
# avoid. These pin both halves to the same matcher.

GROUP_DEVICES = [
    {"ieee_address": "0xaa", "friendly_name": "bulb_a", "type": "Router"},
    {"ieee_address": "0xbb", "friendly_name": "bulb_b", "type": "Router"},
]


def _grouped(groups) -> Registry:
    registry = Registry()
    registry.handle("z2m-test/bridge/devices", json.dumps(GROUP_DEVICES).encode())
    registry.handle("z2m-test/bridge/groups", json.dumps(groups).encode())
    return registry


def test_group_resolves_by_numeric_id_as_well_as_friendly_name():
    registry = _grouped(
        [{"id": 7, "friendly_name": "bulbs",
          "members": [{"ieee_address": "0xaa"}, {"ieee_address": "0xbb"}]}]
    )
    for target in ("bulbs", "7"):
        assert registry.is_group("z2m-test", target) is True
        assert registry.group_members("z2m-test", target) == ["bulb_a", "bulb_b"]
        assert registry.group_members_strict("z2m-test", target) == (
            ["bulb_a", "bulb_b"],
            True,
        )


def test_unresolvable_member_marks_the_roster_incomplete():
    """A member IEEE with no device row is dropped from the list. Dropping the
    member that would have disagreed is what manufactures a false `noop`, so
    the shortfall has to be reported rather than absorbed."""
    registry = _grouped(
        [{"id": 7, "friendly_name": "bulbs",
          "members": [{"ieee_address": "0xaa"}, {"ieee_address": "0xdeadbeef"}]}]
    )
    members, complete = registry.group_members_strict("z2m-test", "bulbs")
    assert members == ["bulb_a"]
    assert complete is False


def test_group_with_no_resolvable_members_is_incomplete_not_empty_and_fine():
    registry = _grouped([{"id": 7, "friendly_name": "bulbs", "members": []}])
    assert registry.group_members_strict("z2m-test", "bulbs") == ([], False)


def test_non_group_target_is_complete_and_empty():
    """A plain device is not a degenerate group: ([], True) tells the detector
    to assess the target against itself."""
    registry = _grouped([{"id": 7, "friendly_name": "bulbs", "members": []}])
    assert registry.group_members_strict("z2m-test", "bulb_a") == ([], True)
    assert registry.is_group("z2m-test", "bulb_a") is False


BOUND_DEVICES = [
    {"ieee_address": "0x00000000000000c0", "friendly_name": "Coordinator", "type": "Coordinator"},
    {
        "ieee_address": "0x00000000000000a1",
        "friendly_name": "plate",
        "type": "Router",
        "endpoints": {
            "1": {"bindings": [
                # Reporting to the coordinator moves nothing.
                {"cluster": "genOnOff", "target": {"type": "endpoint", "endpoint": 1,
                                                   "ieee_address": "0x00000000000000c0"}},
            ]},
            "2": {"bindings": [
                {"cluster": "genOnOff", "target": {"type": "group", "id": 7}},
                {"cluster": "genLevelCtrl", "target": {"type": "group", "id": 7}},
                {"cluster": "genLevelCtrl", "target": {"type": "endpoint", "endpoint": 11,
                                                       "ieee_address": "0x00000000000000b3"}},
                # A cluster a switch does not actuate with.
                {"cluster": "haDiagnostic", "target": {"type": "group", "id": 9}},
            ]},
        },
    },
    {"ieee_address": "0x00000000000000b1", "friendly_name": "bulb_a", "type": "Router"},
    {"ieee_address": "0x00000000000000b2", "friendly_name": "bulb_b", "type": "Router"},
    {"ieee_address": "0x00000000000000b3", "friendly_name": "lamp", "type": "Router"},
    {"ieee_address": "0x00000000000000b4", "friendly_name": "other", "type": "Router"},
]
BOUND_GROUPS = [
    {"id": 7, "friendly_name": "plate_group", "members": [
        {"ieee_address": "0x00000000000000b1", "endpoint": 11},
        {"ieee_address": "0x00000000000000b2", "endpoint": 11},
    ]},
    {"id": 9, "friendly_name": "diag",
     "members": [{"ieee_address": "0x00000000000000b4", "endpoint": 1}]},
]


def test_bound_devices_follow_actuating_bindings_only():
    registry = Registry()
    registry.handle("z2m-test/bridge/devices", json.dumps(BOUND_DEVICES).encode())
    registry.handle("z2m-test/bridge/groups", json.dumps(BOUND_GROUPS).encode())
    # Group members and a directly bound device; never the coordinator, never
    # the non-actuating group.
    assert registry.bound_devices("z2m-test", "plate") == ["bulb_a", "bulb_b", "lamp"]
    assert registry.bound_devices("z2m-test", "bulb_a") == []
    assert registry.bound_devices("z2m-test", "no_such_device") == []
    assert registry.bound_devices("z2m-other", "plate") == []


def test_bound_devices_resolve_groups_that_arrive_after_the_devices():
    registry = Registry()
    registry.handle("z2m-test/bridge/devices", json.dumps(BOUND_DEVICES).encode())
    assert registry.bound_devices("z2m-test", "plate") == ["lamp"]
    registry.handle("z2m-test/bridge/groups", json.dumps(BOUND_GROUPS).encode())
    assert registry.bound_devices("z2m-test", "plate") == ["bulb_a", "bulb_b", "lamp"]
