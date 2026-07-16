"""ASH (UART/TCP framing for EmberZNet EZSP) stream decoder.

Written from the Silicon Labs UG101 protocol description (IP hygiene: no
GPL-derived code: DESIGN.md §16). Spike S1 validates this against a live
coordinator capture before M4 relies on it.

Frame wire format: escaped(control byte + data field + CRC16) + FLAG.
DATA-frame payloads are XOR-scrambled with a fixed LFSR sequence
("randomization") which this module reverses. The encoder helpers exist for
tests and synthetic fixtures: the collector itself never transmits ASH.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FLAG = 0x7E
ESCAPE = 0x7D
XON = 0x11
XOFF = 0x13
SUBSTITUTE = 0x18
CANCEL = 0x1A
RESERVED = {FLAG, ESCAPE, XON, XOFF, SUBSTITUTE, CANCEL}

CONTROL_RST = 0xC0
CONTROL_RSTACK = 0xC1
CONTROL_ERROR = 0xC2


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC16-CCITT ("false" variant): poly 0x1021, init 0xFFFF, no reflection."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _random_sequence(length: int) -> bytes:
    out = bytearray()
    rand = 0x42
    for _ in range(length):
        out.append(rand)
        rand = ((rand >> 1) ^ 0xB8) if rand & 1 else rand >> 1
    return bytes(out)


def randomize(data: bytes) -> bytes:
    """XOR with the ASH pseudo-random sequence; self-inverse."""
    return bytes(b ^ r for b, r in zip(data, _random_sequence(len(data)), strict=True))


@dataclass
class AshFrame:
    type: str  # data | ack | nak | rst | rstack | error | invalid
    control: int
    payload: bytes = b""  # derandomized EZSP bytes for DATA frames
    frm_num: int | None = None
    ack_num: int | None = None
    re_tx: bool = False
    not_ready: bool = False
    crc_ok: bool = True
    wire_len: int = 0  # escaped on-wire size including FLAG


@dataclass
class AshStats:
    frames: dict[str, int] = field(default_factory=dict)
    crc_errors: int = 0
    cancelled: int = 0
    substituted: int = 0
    retransmits: int = 0
    bytes: int = 0

    def count(self, frame: AshFrame) -> None:
        self.frames[frame.type] = self.frames.get(frame.type, 0) + 1
        self.bytes += frame.wire_len
        if frame.type == "data" and frame.re_tx:
            self.retransmits += 1
        if not frame.crc_ok:
            self.crc_errors += 1


class AshDecoder:
    """Feed raw stream bytes for ONE direction; yields completed frames."""

    def __init__(self):
        self._buffer = bytearray()
        self._escaping = False
        self._dropping = False  # SUBSTITUTE seen: discard until next FLAG
        self._wire_count = 0
        self.stats = AshStats()

    def feed(self, data: bytes) -> list[AshFrame]:
        frames: list[AshFrame] = []
        for byte in data:
            self._wire_count += 1
            if byte == CANCEL:
                if self._buffer or self._escaping:
                    self.stats.cancelled += 1
                self._reset(keep_wire=False)
                continue
            if byte == SUBSTITUTE:
                self.stats.substituted += 1
                self._dropping = True
                continue
            if byte in (XON, XOFF):
                continue
            if byte == FLAG:
                if not self._dropping and self._buffer:
                    frame = self._complete(bytes(self._buffer), self._wire_count)
                    self.stats.count(frame)
                    frames.append(frame)
                self._reset(keep_wire=False)
                continue
            if self._dropping:
                continue
            if byte == ESCAPE:
                self._escaping = True
                continue
            if self._escaping:
                byte ^= 0x20
                self._escaping = False
            self._buffer.append(byte)
        return frames

    def _reset(self, keep_wire: bool) -> None:
        self._buffer.clear()
        self._escaping = False
        self._dropping = False
        if not keep_wire:
            self._wire_count = 0

    @staticmethod
    def _complete(raw: bytes, wire_len: int) -> AshFrame:
        if len(raw) < 3:
            return AshFrame(type="invalid", control=raw[0] if raw else -1, crc_ok=False,
                            wire_len=wire_len)
        control, data, crc = raw[0], raw[1:-2], int.from_bytes(raw[-2:], "big")
        crc_ok = crc16_ccitt(raw[:-2]) == crc

        if control & 0x80 == 0:  # DATA: 0fffraaa
            return AshFrame(
                type="data" if crc_ok else "invalid",
                control=control,
                payload=randomize(data) if crc_ok else b"",
                frm_num=(control >> 4) & 0x07,
                ack_num=control & 0x07,
                re_tx=bool(control & 0x08),
                crc_ok=crc_ok,
                wire_len=wire_len,
            )
        if control & 0xE0 == 0x80:  # ACK: 1000naaa
            return AshFrame(type="ack", control=control, ack_num=control & 0x07,
                            not_ready=bool(control & 0x08), crc_ok=crc_ok, wire_len=wire_len)
        if control & 0xE0 == 0xA0:  # NAK: 1010naaa
            return AshFrame(type="nak", control=control, ack_num=control & 0x07,
                            not_ready=bool(control & 0x08), crc_ok=crc_ok, wire_len=wire_len)
        if control == CONTROL_RST:
            return AshFrame(type="rst", control=control, crc_ok=crc_ok, wire_len=wire_len)
        if control == CONTROL_RSTACK:
            return AshFrame(type="rstack", control=control, payload=data, crc_ok=crc_ok,
                            wire_len=wire_len)
        if control == CONTROL_ERROR:
            return AshFrame(type="error", control=control, payload=data, crc_ok=crc_ok,
                            wire_len=wire_len)
        return AshFrame(type="invalid", control=control, crc_ok=crc_ok, wire_len=wire_len)


# -- encoder helpers (tests + synthetic fixtures only) ---------------------------


def _escape(raw: bytes) -> bytes:
    out = bytearray()
    for byte in raw:
        if byte in RESERVED:
            out.append(ESCAPE)
            out.append(byte ^ 0x20)
        else:
            out.append(byte)
    return bytes(out)


def _frame(control: int, data: bytes = b"") -> bytes:
    body = bytes([control]) + data
    crc = crc16_ccitt(body)
    return _escape(body + crc.to_bytes(2, "big")) + bytes([FLAG])


def encode_data_frame(payload: bytes, frm_num: int, ack_num: int, re_tx: bool = False) -> bytes:
    control = ((frm_num & 0x07) << 4) | (0x08 if re_tx else 0) | (ack_num & 0x07)
    return _frame(control, randomize(payload))


def encode_ack(ack_num: int) -> bytes:
    return _frame(0x80 | (ack_num & 0x07))


def encode_rstack(version: int = 2, code: int = 0x0B) -> bytes:
    return _frame(CONTROL_RSTACK, bytes([version, code]))
