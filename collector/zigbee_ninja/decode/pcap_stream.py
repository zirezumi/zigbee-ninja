"""Incremental pcap parser for the live T2 tap stream.

`read_tcp_segments` in pcap.py parses a complete file; the ninja-tap agent
instead streams pcap bytes as tcpdump writes them (global header once, then
records). StreamingPcapReader buffers partial input and yields TcpSegments as
whole records arrive, sharing the packet-dissection helpers with the offline
reader so the S1-validated decode path is identical.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

from .pcap import (
    LINKTYPE_ETHERNET,
    LINKTYPE_NULL,
    LINKTYPE_RAW,
    MAGIC_NS_LE,
    MAGIC_US_LE,
    TcpSegment,
    _parse_ip_tcp,
)


class StreamingPcapReader:
    def __init__(self):
        self._buffer = bytearray()
        self._endian: str | None = None
        self._ns = False
        self._linktype: int | None = None

    @property
    def ready(self) -> bool:
        return self._linktype is not None

    def feed(self, data: bytes) -> Iterator[TcpSegment]:
        self._buffer.extend(data)
        if self._endian is None:
            if len(self._buffer) < 24:
                return
            self._parse_global_header()
        yield from self._drain_records()

    def _parse_global_header(self) -> None:
        magic = struct.unpack_from("<I", self._buffer, 0)[0]
        if magic in (MAGIC_US_LE, MAGIC_NS_LE):
            self._endian, self._ns = "<", magic == MAGIC_NS_LE
        else:
            magic_be = struct.unpack_from(">I", self._buffer, 0)[0]
            if magic_be not in (MAGIC_US_LE, MAGIC_NS_LE):
                raise ValueError("Not a classic pcap stream (bad magic)")
            self._endian, self._ns = ">", magic_be == MAGIC_NS_LE
        self._linktype = struct.unpack_from(self._endian + "I", self._buffer, 20)[0] & 0x0FFFFFFF
        if self._linktype not in (LINKTYPE_ETHERNET, LINKTYPE_RAW, LINKTYPE_NULL):
            raise ValueError(f"Unsupported pcap linktype {self._linktype}")
        del self._buffer[:24]

    def _drain_records(self) -> Iterator[TcpSegment]:
        divisor = 1e9 if self._ns else 1e6
        while len(self._buffer) >= 16:
            ts_sec, ts_frac, incl_len, _orig = struct.unpack_from(
                self._endian + "IIII", self._buffer, 0
            )
            if len(self._buffer) < 16 + incl_len:
                return  # record body not fully arrived yet
            packet = bytes(self._buffer[16 : 16 + incl_len])
            del self._buffer[: 16 + incl_len]
            ts = ts_sec + ts_frac / divisor
            segment = self._dissect(ts, packet)
            if segment is not None:
                yield segment

    def _dissect(self, ts: float, packet: bytes) -> TcpSegment | None:
        if self._linktype == LINKTYPE_ETHERNET:
            if len(packet) < 14:
                return None
            ethertype = int.from_bytes(packet[12:14], "big")
            ip_offset = 14
            if ethertype == 0x8100 and len(packet) >= 18:
                ethertype = int.from_bytes(packet[16:18], "big")
                ip_offset = 18
            if ethertype != 0x0800:
                return None
            return _parse_ip_tcp(ts, packet[ip_offset:])
        if self._linktype == LINKTYPE_NULL:
            return _parse_ip_tcp(ts, packet[4:])
        return _parse_ip_tcp(ts, packet)
