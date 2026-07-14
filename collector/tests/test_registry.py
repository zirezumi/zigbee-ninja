import json

from zigbee_ninja.ingest.registry import Registry

INFO = {
    "version": "2.3.0",
    "coordinator": {"type": "EmberZNet", "meta": {"ieee_address": "0x00124b00aaaaaaaa"}},
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
        "definition": {"vendor": "ExampleCo", "model": "BULB-1"},
    },
    {
        "ieee_address": "0x0000000000000003",
        "friendly_name": "door_sensor",
        "type": "EndDevice",
        "power_source": "Battery",
        "definition": {"vendor": "ExampleCo", "model": "SENSE-2"},
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
