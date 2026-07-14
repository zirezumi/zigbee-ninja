"""Layouts pinned against live SLZB-06MG24 (EmberZNet 8.x / EZSP v14-era)
captures; the incoming/route-error/network-status fixtures are verbatim frame
parameters from that spike (sender EUI64s arrive zeroed from the firmware;
short addresses are ephemeral NWK ids)."""

from zigbee_ninja.decode import ezsp_params as ep

# type 0, aps(0x0104/0x0008 srcEp 0x0b dstEp 1 options 0x0100 seq 0x8f),
# sender 0x01E6, zero EUI, binding 0xff addr 0x20, LQI 108, RSSI -73,
# timestamp, len 5, ZCL default-response, one trailing byte 0x02.
LIVE_INCOMING_RADIO = bytes.fromhex(
    "000401" "0800" "0b01" "0001" "0000" "8f" "e601" "0000000000000000"
    "ff20" "6cb7" "67c29dd5" "05" "18920b0400" "02"
)
# type 3 (multicast loopback), group 0x0005, sender 0x0000, LQI 0xff RSSI 0,
# len 8, no trailing byte.
LIVE_INCOMING_LOOPBACK = bytes.fromhex(
    "030401" "0800" "01ff" "0001" "0500" "80" "0000" "0000000000000000"
    "ffff" "ff00" "d9b287d3" "08" "11c3044432000000"
)
LIVE_ROUTE_ERROR = bytes.fromhex("140c0000e5e1")
LIVE_NETWORK_STATUS = bytes.fromhex("0ce5e1")

APS = bytes.fromhex("0401" "0800" "0101" "4011" "0000" "42")  # 0x0104/0x0008 opts 0x1140


def test_parse_send_unicast():
    params = bytes([0x00, 0xCD, 0x4D]) + APS + bytes([0x34, 0x12, 0x03, 1, 2, 3])
    sent = ep.parse_send_unicast(params)
    assert sent.destination == 0x4DCD
    assert sent.aps.profile_id == 0x0104
    assert sent.aps.cluster_id == 0x0008
    assert sent.aps.options == 0x1140
    assert sent.tag == 0x1234
    assert sent.payload_len == 3
    assert ep.parse_send_unicast(params[:-1]) is None  # length arithmetic broken
    assert ep.parse_send_unicast(b"\x00" * 5) is None


def test_parse_send_multicast():
    params = APS[:8] + bytes([0x0A, 0x00, 0x55]) + bytes.fromhex("0c0000ffff00") + bytes(
        [0x78, 0x56, 0x02, 0xAA, 0xBB]
    )
    sent = ep.parse_send_multicast(params)
    assert sent.aps.group_id == 0x000A
    assert sent.tag == 0x5678
    assert sent.payload_len == 2
    assert ep.parse_send_multicast(params + b"\x00") is None


def test_parse_send_broadcast_degrades_without_live_pin():
    shaped = APS + bytes.fromhex("0c0000ffff00") + bytes([0x11, 0x22, 0x01, 0xEE])
    exact = ep.parse_send_broadcast(shaped)
    assert exact.exact is True
    assert exact.tag == 0x2211
    assert exact.payload_len == 1

    fallback = ep.parse_send_broadcast(b"\x00" * 26)
    assert fallback.exact is False
    assert fallback.tag is None
    assert fallback.payload_len == 6  # conservative estimate, never garbage


def test_parse_message_sent():
    ok = bytes([0, 0, 0, 0, 0x00, 0xCD, 0x4D]) + APS + bytes([0x34, 0x12, 0x00])
    sent = ep.parse_message_sent(ok)
    assert sent.ok is True
    assert sent.tag == 0x1234
    assert sent.dest_or_index == 0x4DCD

    failed = bytes([0x14, 0x0C, 0, 0]) + ok[4:]
    sent = ep.parse_message_sent(failed)
    assert sent.ok is False
    assert sent.status == 0x0C14
    assert ep.parse_message_sent(ok[:-2]) is None


def test_parse_incoming_radio_frame():
    incoming = ep.parse_incoming(LIVE_INCOMING_RADIO)
    assert incoming.msg_type == ep.INCOMING_UNICAST
    assert incoming.aps.cluster_id == 0x0008
    assert incoming.sender == 0x01E6
    assert incoming.lqi == 108
    assert incoming.rssi == -73
    assert incoming.payload_len == 5
    assert incoming.trailing == b"\x02"
    assert incoming.loopback is False
    assert incoming.acked is True


def test_parse_incoming_loopback():
    incoming = ep.parse_incoming(LIVE_INCOMING_LOOPBACK)
    assert incoming.msg_type == ep.INCOMING_MULTICAST_LOOPBACK
    assert incoming.aps.group_id == 0x0005
    assert incoming.sender == 0x0000
    assert incoming.payload_len == 8
    assert incoming.trailing == b""
    assert incoming.loopback is True
    assert incoming.acked is False
    assert ep.parse_incoming(LIVE_INCOMING_LOOPBACK[:20]) is None


def test_parse_route_record():
    params = bytes.fromhex("2a1b" "0000000000000000" "90" "b0" "01" "1122")
    record = ep.parse_route_record(params)
    assert record.source == 0x1B2A
    assert record.lqi == 0x90
    assert record.rssi == -80
    assert record.relay_count == 1
    assert ep.parse_route_record(params + b"\x00") is None


def test_parse_route_error_and_network_status():
    error = ep.parse_route_error(LIVE_ROUTE_ERROR)
    assert error.status == 0x0C14  # SL_STATUS_ZIGBEE_DELIVERY_FAILED
    assert error.target == 0xE1E5

    status = ep.parse_network_status(LIVE_NETWORK_STATUS)
    assert status.code == 0x0C  # many-to-one route failure
    assert status.target == 0xE1E5  # same event, same target as the route error
    assert ep.parse_network_status(LIVE_ROUTE_ERROR) is None


def test_parse_counters():
    values = list(range(40))
    params = b"".join(v.to_bytes(2, "little") for v in values)
    assert ep.parse_counters(params) == values
    assert ep.parse_counters(params[:-1]) is None  # odd length
    assert ep.parse_counters(b"\x00\x00") is None  # too short to be counters
