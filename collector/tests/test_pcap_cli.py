import struct

from zigbee_ninja.decode.ash import encode_ack, encode_data_frame
from zigbee_ninja.decode.pcap_cli import analyze

COORD = ("10.0.0.50", 6638)
HOST = ("10.0.0.2", 41000)

LEGACY_VERSION_CMD = bytes([0x00, 0x00, 0x00, 13])
LEGACY_VERSION_RSP = bytes([0x00, 0x80, 0x00, 13, 2, 0xAA, 0xBB])
EXT_SEND_UNICAST = bytes([0x01, 0x00, 0x01, 0x34, 0x00]) + b"\x11" * 12
EXT_INCOMING_CB = bytes([0x02, 0x90, 0x01, 0x45, 0x00]) + b"\x22" * 20


def ip_bytes(ip: str) -> bytes:
    return bytes(int(part) for part in ip.split("."))


def tcp_packet(src, dst, seq: int, payload: bytes) -> bytes:
    tcp = struct.pack(
        "!HHIIBBHHH", src[1], dst[1], seq, 0, 5 << 4, 0x18, 65535, 0, 0
    ) + payload
    total = 20 + len(tcp)
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0, total, 0, 0, 64, 6, 0, ip_bytes(src[0]), ip_bytes(dst[0]),
    )
    ethernet = b"\xaa" * 6 + b"\xbb" * 6 + b"\x08\x00"
    return ethernet + ip + tcp


def build_pcap(packets: list[tuple[float, bytes]]) -> bytes:
    header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    body = b""
    for ts, packet in packets:
        seconds = int(ts)
        micros = int(round((ts - seconds) * 1e6))
        body += struct.pack("<IIII", seconds, micros, len(packet), len(packet)) + packet
    return header + body


def conversation() -> tuple[bytes, dict]:
    to_coord_wire = [
        encode_data_frame(LEGACY_VERSION_CMD, frm_num=0, ack_num=0),
        encode_data_frame(EXT_SEND_UNICAST, frm_num=1, ack_num=1),
    ]
    from_coord_wire = [
        encode_data_frame(LEGACY_VERSION_RSP, frm_num=0, ack_num=1),
        encode_ack(2),
        encode_data_frame(EXT_INCOMING_CB, frm_num=1, ack_num=2),
    ]

    packets = []
    ts = 100.0
    host_seq, coord_seq = 5000, 9000

    def send(direction_wire, src, dst, seq):
        nonlocal ts
        ts += 0.01
        packets.append((ts, tcp_packet(src, dst, seq, direction_wire)))
        return seq + len(direction_wire)

    host_seq = send(to_coord_wire[0], HOST, COORD, host_seq)
    coord_seq = send(from_coord_wire[0], COORD, HOST, coord_seq)
    coord_seq = send(from_coord_wire[1], COORD, HOST, coord_seq)
    host_seq = send(to_coord_wire[1], HOST, COORD, host_seq)
    # exact TCP retransmit of the last host segment (must dedupe, not re-decode)
    ts += 0.01
    packets.append(
        (ts, tcp_packet(HOST, COORD, host_seq - len(to_coord_wire[1]), to_coord_wire[1]))
    )
    coord_seq = send(from_coord_wire[2], COORD, HOST, coord_seq)

    return build_pcap(packets), {"host_seq": host_seq, "coord_seq": coord_seq}


def test_end_to_end_synthetic_capture(tmp_path):
    pcap, _ = conversation()

    report = analyze(pcap, 6638)
    assert report["tcp_segments"] == 6
    assert len(report["connections"]) == 1
    conn = report["connections"][0]

    # Legacy version handshake is present in this synthetic capture, so the
    # stream negotiates down from the extended default and reads the version.
    assert conn["protocol_version"] == 13
    assert conn["ezsp_frames"] == {
        "version": 2,
        "sendUnicast": 1,
        "incomingMessageHandler": 1,
    }
    assert conn["commands"] == 2
    assert conn["responses_and_callbacks"] == 2
    assert conn["ezsp_parse_errors"] == 0

    to_coord = conn["to_coord"]
    assert to_coord["ash_frames"] == {"data": 2}
    assert to_coord["crc_errors"] == 0
    assert to_coord["duplicate_bytes"] > 0  # the retransmit was deduped
    assert to_coord["tcp_gaps"] == 0

    from_coord = conn["from_coord"]
    assert from_coord["ash_frames"] == {"data": 2, "ack": 1}


def test_cli_main_prints_and_exits_zero(tmp_path, capsys):
    from zigbee_ninja.decode.pcap_cli import main

    pcap, _ = conversation()
    path = tmp_path / "capture.pcap"
    path.write_bytes(pcap)

    exit_code = main([str(path), "--port", "6638"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "sendUnicast" in output
    assert "EZSP v13" in output


def test_non_pcap_rejected(tmp_path):
    from zigbee_ninja.decode.pcap import read_tcp_segments

    try:
        read_tcp_segments(b"\x00" * 100)
        raised = False
    except ValueError:
        raised = True
    assert raised
