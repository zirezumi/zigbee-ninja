"""EZSP envelope parsing (frame accounting level, not deep parameter decode).

Two header formats exist: the legacy 3-byte header (seq, control, frameId) used
by EZSP ≤7 AND by the version handshake on every protocol version, and the
extended 5-byte header (seq, control lo/hi, frameId LE16) used once a version
≥8 is negotiated. `EzspStream` tracks that negotiation per connection.

Frame names below are convenience labels for the core set the wire tier cares
about; the inventory is validated against a live capture in spike S1, and
unknown IDs degrade to hex labels rather than errors. Deep parameter decode
(APS frames, LQI/RSSI) lands after S1 pins real fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass

VERSION_FRAME_ID = 0x0000

FRAME_NAMES = {
    0x0000: "version",
    0x0006: "callback",
    0x0007: "noCallbacks",
    0x0019: "stackStatusHandler",
    0x0034: "sendUnicast",
    0x0036: "sendBroadcast",
    0x0038: "sendMulticast",
    0x003F: "messageSentHandler",
    0x0045: "incomingMessageHandler",
    0x0065: "readAndClearCounters",
    0x00F1: "readCounters",
}


@dataclass
class EzspFrame:
    seq: int
    frame_id: int
    name: str
    is_response: bool
    is_callback: bool
    param_length: int
    header_format: str  # "legacy" | "extended"


def _name(frame_id: int) -> str:
    return FRAME_NAMES.get(frame_id, f"unknown_0x{frame_id:04X}")


def parse_legacy(payload: bytes) -> EzspFrame | None:
    if len(payload) < 3:
        return None
    seq, control, frame_id = payload[0], payload[1], payload[2]
    return EzspFrame(
        seq=seq,
        frame_id=frame_id,
        name=_name(frame_id),
        is_response=bool(control & 0x80),
        is_callback=bool(control & 0x10),
        param_length=len(payload) - 3,
        header_format="legacy",
    )


def parse_extended(payload: bytes) -> EzspFrame | None:
    if len(payload) < 5:
        return None
    seq = payload[0]
    control_low = payload[1]
    frame_id = int.from_bytes(payload[3:5], "little")
    return EzspFrame(
        seq=seq,
        frame_id=frame_id,
        name=_name(frame_id),
        is_response=bool(control_low & 0x80),
        is_callback=bool(control_low & 0x10),
        param_length=len(payload) - 5,
        header_format="extended",
    )


class EzspStream:
    """Per-connection EZSP parser: legacy until a version response negotiates ≥8."""

    def __init__(self):
        self.protocol_version: int | None = None
        self.frames: list[EzspFrame] = []
        self.parse_errors = 0

    @property
    def extended(self) -> bool:
        return self.protocol_version is not None and self.protocol_version >= 8

    def feed(self, payload: bytes) -> EzspFrame | None:
        frame = parse_extended(payload) if self.extended else parse_legacy(payload)
        if frame is None:
            self.parse_errors += 1
            return None
        if (
            frame.header_format == "legacy"
            and frame.frame_id == VERSION_FRAME_ID
            and frame.is_response
            and frame.param_length >= 1
        ):
            self.protocol_version = payload[3]
        self.frames.append(frame)
        return frame
