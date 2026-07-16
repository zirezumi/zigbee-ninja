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


def test_autonomous_cluster_report_not_paired_as_latency():
    # A command to a presence dimmer followed only by autonomous sensor/OTA/mmWave
    # reports from that same device must NOT produce a (false, inflated) sample.
    ingest = ProbeIngest()
    ingest.handle(
        "z2m-test",
        "zigbee-ninja/probe/events",
        events_payload(
            1,
            [
                [1000.0, "mi", "z2m-test/presence_dimmer/set", 12],
                [1000.4, "dm", "presence_dimmer", "msIlluminanceMeasurement", "r", 90, 8],
                [1002.1, "dm", "presence_dimmer", "genOta", "commandQueryNextImage", 90, 8],
                [1002.3, "dm", "presence_dimmer", "manuSpecificInovelliMMWave", "cmd", 90, 8],
            ],
        ),
    )
    assert ingest.latency.snapshot() == {}


def test_vendor_state_echo_cluster_is_paired():
    # Hue bulbs echo state on manuSpecificPhilips2 — that IS a command response.
    ingest = ProbeIngest()
    ingest.handle(
        "z2m-test",
        "zigbee-ninja/probe/events",
        events_payload(
            1,
            [
                [1000.0, "mi", "z2m-test/bulb/set", 20],
                [1000.3, "dm", "bulb", "manuSpecificPhilips2", "attributeReport", 100, 25],
            ],
        ),
    )
    assert ingest.latency.snapshot()["z2m-test"]["count"] == 1


def test_newest_command_pairing_under_burst():
    # Two commands to the same device within the window, then one state echo:
    # it answers the NEWEST command (300ms), not the oldest (would be 1300ms).
    ingest = ProbeIngest()
    ingest.handle(
        "z2m-test",
        "zigbee-ninja/probe/events",
        events_payload(
            1,
            [
                [1000.0, "mi", "z2m-test/lamp/set", 10],
                [1001.0, "mi", "z2m-test/lamp/set", 10],
                [1001.3, "dm", "lamp", "genLevelCtrl", "attributeReport", 100, 25],
            ],
        ),
    )
    snapshot = ingest.latency.snapshot()
    assert snapshot["z2m-test"]["count"] == 1
    assert 290 <= snapshot["z2m-test"]["p50_ms"] <= 310


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


def test_device_seq_callback_only_for_sequenced_events():
    seen = []
    ingest = ProbeIngest(
        on_device_seq=lambda base, name, seq, ts: seen.append((base, name, seq, ts))
    )
    ingest.handle(
        "z2m-test",
        "zigbee-ninja/probe/events",
        events_payload(
            1,
            [
                # Probe v0.4 appends [zcl_seq, endpoint] to dm events.
                [1000.0, "dm", "lamp", "genOnOff", "attributeReport", 120, 30, 146, 11],
                # -1 marks a message without a ZCL sequence — never forwarded.
                [1000.1, "dm", "lamp", "genOnOff", "attributeReport", 120, 30, -1, 11],
                # v0.3 shape (no fusion fields) still parses for latency only.
                [1000.2, "dm", "lamp", "genOnOff", "attributeReport", 120, 30],
            ],
        ),
    )
    assert seen == [("z2m-test", "lamp", 146, 1000.0)]
