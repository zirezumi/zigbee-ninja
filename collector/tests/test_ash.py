from zigbee_ninja.decode.ash import (
    AshDecoder,
    crc16_ccitt,
    encode_ack,
    encode_data_frame,
    encode_rstack,
    randomize,
)


def test_crc16_ccitt_known_vector():
    # CRC-CCITT (false) of "123456789" is 0x29B1: standard check value.
    assert crc16_ccitt(b"123456789") == 0x29B1


def test_randomize_is_self_inverse_and_starts_at_0x42():
    payload = bytes(range(32))
    scrambled = randomize(payload)
    assert scrambled != payload
    assert randomize(scrambled) == payload
    assert randomize(b"\x00")[0] == 0x42  # first LFSR byte


def test_data_frame_round_trip():
    payload = b"\x01\x02\x03\x04\x05"
    wire = encode_data_frame(payload, frm_num=3, ack_num=5)
    frames = AshDecoder().feed(wire)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.type == "data"
    assert frame.payload == payload
    assert frame.frm_num == 3
    assert frame.ack_num == 5
    assert frame.re_tx is False
    assert frame.crc_ok is True


def test_escaping_round_trip():
    # 0x3C ^ 0x42 (first LFSR byte) == 0x7E, forcing an escape on the wire.
    payload = b"\x3c\x01\x02"
    wire = encode_data_frame(payload, frm_num=0, ack_num=0)
    assert b"\x7d" in wire  # escape byte actually present
    frames = AshDecoder().feed(wire)
    assert frames[0].payload == payload


def test_frame_split_across_feeds():
    wire = encode_data_frame(b"hello", frm_num=1, ack_num=2)
    decoder = AshDecoder()
    assert decoder.feed(wire[:4]) == []
    frames = decoder.feed(wire[4:])
    assert len(frames) == 1
    assert frames[0].payload == b"hello"


def test_crc_corruption_yields_invalid():
    wire = bytearray(encode_data_frame(b"hello", frm_num=1, ack_num=2))
    wire[2] ^= 0xFF  # corrupt a payload byte
    decoder = AshDecoder()
    frames = decoder.feed(bytes(wire))
    assert frames[0].type == "invalid"
    assert decoder.stats.crc_errors == 1


def test_retransmit_flag_counted():
    wire = encode_data_frame(b"x", frm_num=1, ack_num=0, re_tx=True)
    decoder = AshDecoder()
    frames = decoder.feed(wire)
    assert frames[0].re_tx is True
    assert decoder.stats.retransmits == 1


def test_ack_and_rstack():
    decoder = AshDecoder()
    frames = decoder.feed(encode_ack(4) + encode_rstack())
    assert [frame.type for frame in frames] == ["ack", "rstack"]
    assert frames[0].ack_num == 4
    assert frames[1].payload[1] == 0x0B  # reset code


def test_cancel_drops_partial_frame():
    wire = encode_data_frame(b"hello", frm_num=1, ack_num=2)
    decoder = AshDecoder()
    frames = decoder.feed(wire[:5] + b"\x1a" + encode_ack(1))
    assert [frame.type for frame in frames] == ["ack"]
    assert decoder.stats.cancelled == 1


def test_xon_xoff_ignored_inside_stream():
    wire = encode_data_frame(b"ab", frm_num=0, ack_num=0)
    noisy = wire[:2] + b"\x11\x13" + wire[2:]
    frames = AshDecoder().feed(noisy)
    assert frames[0].type == "data"
    assert frames[0].payload == b"ab"
