from zigbee_ninja.decode.ezsp import EzspStream, parse_extended, parse_legacy

LEGACY_VERSION_CMD = bytes([0x00, 0x00, 0x00, 13])
LEGACY_VERSION_RSP = bytes([0x00, 0x80, 0x00, 13, 2, 0xAA, 0xBB])
EXT_SEND_UNICAST = bytes([0x01, 0x00, 0x01, 0x34, 0x00]) + b"\x00" * 12
EXT_INCOMING_CB = bytes([0x02, 0x90, 0x01, 0x45, 0x00]) + b"\x00" * 20


def test_parse_legacy_command_and_response():
    command = parse_legacy(LEGACY_VERSION_CMD)
    assert command.name == "version"
    assert command.is_response is False

    response = parse_legacy(LEGACY_VERSION_RSP)
    assert response.is_response is True
    assert response.param_length == 4


def test_parse_extended_frames():
    frame = parse_extended(EXT_SEND_UNICAST)
    assert frame.name == "sendUnicast"
    assert frame.is_response is False
    assert frame.header_format == "extended"

    callback = parse_extended(EXT_INCOMING_CB)
    assert callback.name == "incomingMessageHandler"
    assert callback.is_response is True
    assert callback.is_callback is True


def test_legacy_stream_switches_format_after_version_negotiation():
    # Legacy-first path (old NCPs): explicitly opt out of the extended default.
    stream = EzspStream(default_extended=False)
    assert stream.feed(LEGACY_VERSION_CMD).header_format == "legacy"
    assert stream.feed(LEGACY_VERSION_RSP).header_format == "legacy"
    assert stream.protocol_version == 13
    assert stream.extended is True

    frame = stream.feed(EXT_SEND_UNICAST)
    assert frame.header_format == "extended"
    assert frame.name == "sendUnicast"


def test_stream_defaults_to_extended_for_midstream_capture():
    # A passive capture of an already-running modern link never sees the
    # handshake, yet must decode extended frames from the first byte
    # (validated against a live SLZB-06MG24 capture in spike S1).
    stream = EzspStream()
    frame = stream.feed(EXT_SEND_UNICAST)
    assert frame.header_format == "extended"
    assert frame.name == "sendUnicast"

    callback = stream.feed(EXT_INCOMING_CB)
    assert callback.name == "incomingMessageHandler"
    assert callback.is_callback is True


def test_unknown_frame_id_gets_hex_label():
    payload = bytes([0x03, 0x00, 0x01, 0xEE, 0x7F])
    assert parse_extended(payload).name == "unknown_0x7FEE"


def test_short_payload_counts_parse_error():
    stream = EzspStream()
    assert stream.feed(b"\x01") is None
    assert stream.parse_errors == 1
