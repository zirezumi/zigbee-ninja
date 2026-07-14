import json

from zigbee_ninja.ingest.probe import ProbeIngest


def events_payload(seq: int, events: list) -> bytes:
    return json.dumps({"v": 1, "seq": seq, "events": events}).encode()


def test_latency_from_command_and_device_response():
    ingest = ProbeIngest()
    ingest.handle(
        "z2m-test",
        "zigbee-ninja/probe/events",
        events_payload(
            1,
            [
                [1000.0, "mi", "z2m-test/lamp/set", 17],
                [1000.25, "dm", "lamp", "genOnOff", "commandResponse", 120, 30],
            ],
        ),
    )
    snapshot = ingest.latency.snapshot()
    assert snapshot["z2m-test"]["count"] == 1
    assert 240 <= snapshot["z2m-test"]["p50_ms"] <= 260


def test_group_command_latency_via_member_response():
    ingest = ProbeIngest(
        resolve_members=lambda _i, target: ["bulb_1"] if target == "kitchen" else []
    )
    ingest.handle(
        "z2m-test",
        "zigbee-ninja/probe/events",
        events_payload(
            1,
            [
                [1000.0, "mi", "z2m-test/kitchen/set", 17],
                [1000.1, "dm", "bulb_1", "genLevelCtrl", "attributeReport", 100, 25],
            ],
        ),
    )
    assert ingest.latency.snapshot()["z2m-test"]["count"] == 1


def test_unmatched_device_message_produces_no_sample():
    ingest = ProbeIngest()
    ingest.handle(
        "z2m-test",
        "zigbee-ninja/probe/events",
        events_payload(1, [[1000.0, "dm", "sensor", "msTemperature", "attributeReport", 90, 20]]),
    )
    assert ingest.latency.snapshot() == {}


def test_seq_gap_detection():
    ingest = ProbeIngest()
    ingest.handle("z2m-test", "zigbee-ninja/probe/events", events_payload(1, []))
    ingest.handle("z2m-test", "zigbee-ninja/probe/events", events_payload(4, []))
    assert ingest.stats()["z2m-test"]["seq_gaps"] == 2


def test_heartbeat_updates_stats_and_callback():
    seen = {}
    ingest = ProbeIngest(on_heartbeat=lambda base, hb: seen.update({base: hb}))
    heartbeat = {
        "v": 1,
        "version": "0.3.0",
        "enabled": True,
        "hooks": ["onDeviceMessage", "onMQTTMessage"],
        "counters": {"emitted": 10, "dropped": 0, "handlerErrors": 0},
    }
    ingest.handle("z2m-test", "zigbee-ninja/probe/heartbeat", json.dumps(heartbeat).encode())

    stats = ingest.stats()["z2m-test"]
    assert stats["version"] == "0.3.0"
    assert stats["hooks"] == ["onDeviceMessage", "onMQTTMessage"]
    assert seen["z2m-test"]["version"] == "0.3.0"


def test_junk_payloads_counted_not_raised():
    ingest = ProbeIngest()
    ingest.handle("z2m-test", "zigbee-ninja/probe/events", b"\x00 not json")
    ingest.handle("z2m-test", "zigbee-ninja/probe/heartbeat", b"[]")
    assert ingest.stats()["z2m-test"]["parse_errors"] == 2
