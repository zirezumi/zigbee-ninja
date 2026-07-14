"""Minimal classic-pcap reader + in-order TCP payload extraction.

Enough for S1 captures on a quiet LAN: tcpdump's default classic pcap format,
Ethernet (with optional 802.1Q tag) or raw-IP linktypes, IPv4/TCP, in-order
reassembly with retransmit dedupe and explicit gap accounting. Not a general
pcap library and doesn't try to be — pcapng, IPv6, and out-of-order repair are
out of scope until a real capture demands them.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC_US_LE = 0xA1B2C3D4
MAGIC_NS_LE = 0xA1B23C4D

LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101


@dataclass
class TcpSegment:
    ts: float
    src: tuple[str, int]
    dst: tuple[str, int]
    seq: int
    payload: bytes
    syn: bool
    fin: bool


@dataclass
class StreamBytes:
    """In-order payload for one TCP direction, with loss accounting."""

    data: bytearray = field(default_factory=bytearray)
    next_seq: int | None = None
    duplicate_bytes: int = 0
    gap_bytes: int = 0
    gaps: int = 0

    def add(self, segment: TcpSegment) -> None:
        seq = segment.seq
        if segment.syn:
            self.next_seq = (seq + 1) & 0xFFFFFFFF
            return
        payload = segment.payload
        if not payload:
            return
        if self.next_seq is None:
            self.next_seq = seq
        offset = (seq - self.next_seq) & 0xFFFFFFFF
        if offset >= 0x80000000:  # sequence behind us: retransmit/overlap
            behind = 0x100000000 - offset
            if behind >= len(payload):
                self.duplicate_bytes += len(payload)
                return
            self.duplicate_bytes += behind
            payload = payload[behind:]
        elif offset > 0:  # ahead of us: capture lost segments
            self.gaps += 1
            self.gap_bytes += offset
            self.next_seq = seq  # resync at the new segment
        self.data.extend(payload)
        self.next_seq = (self.next_seq + len(payload)) & 0xFFFFFFFF


def _parse_ip_tcp(ts: float, packet: bytes) -> TcpSegment | None:
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    if packet[9] != 6 or len(packet) < ihl + 20:  # TCP only
        return None
    total_length = int.from_bytes(packet[2:4], "big")
    packet = packet[: max(total_length, ihl + 20)] if total_length >= ihl + 20 else packet
    src_ip = ".".join(str(b) for b in packet[12:16])
    dst_ip = ".".join(str(b) for b in packet[16:20])
    tcp = packet[ihl:]
    src_port, dst_port = struct.unpack_from("!HH", tcp, 0)
    seq = struct.unpack_from("!I", tcp, 4)[0]
    data_offset = (tcp[12] >> 4) * 4
    flags = tcp[13]
    return TcpSegment(
        ts=ts,
        src=(src_ip, src_port),
        dst=(dst_ip, dst_port),
        seq=seq,
        payload=bytes(tcp[data_offset:]),
        syn=bool(flags & 0x02),
        fin=bool(flags & 0x01),
    )


def read_tcp_segments(data: bytes) -> list[TcpSegment]:
    if len(data) < 24:
        raise ValueError("Not a pcap file (truncated header)")
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic in (MAGIC_US_LE, MAGIC_NS_LE):
        endian, ns = "<", magic == MAGIC_NS_LE
    else:
        magic_be = struct.unpack_from(">I", data, 0)[0]
        if magic_be not in (MAGIC_US_LE, MAGIC_NS_LE):
            raise ValueError("Not a classic pcap file (bad magic; pcapng is unsupported)")
        endian, ns = ">", magic_be == MAGIC_NS_LE
    linktype = struct.unpack_from(endian + "I", data, 20)[0] & 0x0FFFFFFF
    if linktype not in (LINKTYPE_ETHERNET, LINKTYPE_RAW, LINKTYPE_NULL):
        raise ValueError(f"Unsupported pcap linktype {linktype}")

    segments: list[TcpSegment] = []
    offset = 24
    divisor = 1e9 if ns else 1e6
    while offset + 16 <= len(data):
        ts_sec, ts_frac, incl_len, _orig_len = struct.unpack_from(endian + "IIII", data, offset)
        offset += 16
        packet = data[offset : offset + incl_len]
        offset += incl_len
        if len(packet) < incl_len:
            break
        ts = ts_sec + ts_frac / divisor

        if linktype == LINKTYPE_ETHERNET:
            if len(packet) < 14:
                continue
            ethertype = int.from_bytes(packet[12:14], "big")
            ip_offset = 14
            if ethertype == 0x8100 and len(packet) >= 18:  # 802.1Q
                ethertype = int.from_bytes(packet[16:18], "big")
                ip_offset = 18
            if ethertype != 0x0800:
                continue
            ip_packet = packet[ip_offset:]
        elif linktype == LINKTYPE_NULL:
            ip_packet = packet[4:]
        else:  # LINKTYPE_RAW
            ip_packet = packet

        segment = _parse_ip_tcp(ts, ip_packet)
        if segment is not None:
            segments.append(segment)
    return segments
