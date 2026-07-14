"""Deep EZSP parameter decode for the frames the capacity/latency models need.

Layouts here are pinned EMPIRICALLY against live SLZB-06MG24 captures speaking
the EZSP v14-era encoding (EmberZNet 8.x: 32-bit sl_status, 16-bit message
tags, rx-packet-info struct) — the spike swept 100% of live sendUnicast /
sendMulticast / messageSentHandler / incomingRouteRecordHandler frames. Every
parser self-checks the frame's internal length arithmetic and returns None when
it doesn't hold, so a firmware layout change degrades to visible
`layout_mismatch` accounting instead of silently wrong numbers (DESIGN.md
§7.3). The v13-era (8-bit EmberStatus) layouts are deliberately NOT guessed at;
pin them against a real v13 capture before adding them.

Field notes from the live captures:
- incomingMessageHandler radio frames carry exactly ONE byte after the message
  contents (values 0x02/0x04 observed; identity unconfirmed) while loopback
  deliveries carry none — tolerated and surfaced as `trailing`.
- The rx-packet-info sender EUI64 is zeroed on this firmware; sender identity
  is the NWK short address only.
"""

from __future__ import annotations

from dataclasses import dataclass

SL_STATUS_OK = 0x0000

# sl_zigbee_incoming_message_type_t
INCOMING_UNICAST = 0x00
INCOMING_UNICAST_REPLY = 0x01
INCOMING_MULTICAST = 0x02
INCOMING_MULTICAST_LOOPBACK = 0x03
INCOMING_BROADCAST = 0x04
INCOMING_BROADCAST_LOOPBACK = 0x05

_LOOPBACK_TYPES = (INCOMING_MULTICAST_LOOPBACK, INCOMING_BROADCAST_LOOPBACK)
_ACKED_TYPES = (INCOMING_UNICAST, INCOMING_UNICAST_REPLY)


def _le16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _le32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


@dataclass(frozen=True)
class ApsFrame:
    profile_id: int
    cluster_id: int
    src_ep: int
    dst_ep: int
    options: int
    group_id: int
    sequence: int


def _aps(params: bytes, offset: int) -> ApsFrame:
    return ApsFrame(
        profile_id=_le16(params, offset),
        cluster_id=_le16(params, offset + 2),
        src_ep=params[offset + 4],
        dst_ep=params[offset + 5],
        options=_le16(params, offset + 6),
        group_id=_le16(params, offset + 8),
        sequence=params[offset + 10],
    )


@dataclass(frozen=True)
class SendUnicast:
    msg_type: int
    destination: int
    aps: ApsFrame
    tag: int
    payload_len: int


def parse_send_unicast(params: bytes) -> SendUnicast | None:
    # type u8, destination u16, apsFrame 11B, messageTag u16, messageLength u8, contents
    if len(params) < 17 or len(params) != 17 + params[16]:
        return None
    return SendUnicast(
        msg_type=params[0],
        destination=_le16(params, 1),
        aps=_aps(params, 3),
        tag=_le16(params, 14),
        payload_len=params[16],
    )


@dataclass(frozen=True)
class SendMulticast:
    aps: ApsFrame
    tag: int
    payload_len: int


def parse_send_multicast(params: bytes) -> SendMulticast | None:
    # apsFrame 11B, hops/broadcast/alias region 6B (observed `0c 00 00 ff ff 00`),
    # messageTag u16, messageLength u8, contents
    if len(params) < 20 or len(params) != 20 + params[19]:
        return None
    return SendMulticast(aps=_aps(params, 0), tag=_le16(params, 17), payload_len=params[19])


@dataclass(frozen=True)
class SendBroadcast:
    tag: int | None
    payload_len: int
    exact: bool


def parse_send_broadcast(params: bytes) -> SendBroadcast:
    # No live sample captured yet. Try the multicast-shaped hypothesis (fixed 20,
    # tag @17, length @19); degrade to a conservative payload estimate otherwise.
    if len(params) >= 20 and len(params) == 20 + params[19]:
        return SendBroadcast(tag=_le16(params, 17), payload_len=params[19], exact=True)
    return SendBroadcast(tag=None, payload_len=max(len(params) - 20, 0), exact=False)


@dataclass(frozen=True)
class MessageSent:
    status: int  # sl_status_t
    msg_type: int
    dest_or_index: int
    aps: ApsFrame
    tag: int

    @property
    def ok(self) -> bool:
        return self.status == SL_STATUS_OK


def parse_message_sent(params: bytes) -> MessageSent | None:
    # status u32, type u8, destination u16, apsFrame 11B, messageTag u16,
    # messageLength u8, contents (length 0 observed live — contents not echoed)
    if len(params) < 21 or len(params) != 21 + params[20]:
        return None
    return MessageSent(
        status=_le32(params, 0),
        msg_type=params[4],
        dest_or_index=_le16(params, 5),
        aps=_aps(params, 7),
        tag=_le16(params, 18),
    )


@dataclass(frozen=True)
class IncomingMessage:
    msg_type: int
    aps: ApsFrame
    sender: int
    lqi: int
    rssi: int
    payload_len: int
    trailing: bytes

    @property
    def loopback(self) -> bool:
        """NCP-internal delivery of our own group/broadcast — not a radio frame."""
        return self.msg_type in _LOOPBACK_TYPES

    @property
    def acked(self) -> bool:
        """The coordinator MAC-acks received unicasts."""
        return self.msg_type in _ACKED_TYPES


def parse_incoming(params: bytes) -> IncomingMessage | None:
    # type u8, apsFrame 11B, packet-info 18B (sender u16, senderEui64 8B,
    # bindingIndex u8, addressIndex u8, lastHopLqi u8, lastHopRssi i8,
    # timestamp u32), messageLength u8, contents, 0-1 trailing bytes
    if len(params) < 31:
        return None
    payload_len = params[30]
    trailing_len = len(params) - 31 - payload_len
    if trailing_len < 0 or trailing_len > 2:
        return None
    return IncomingMessage(
        msg_type=params[0],
        aps=_aps(params, 1),
        sender=_le16(params, 12),
        lqi=params[24],
        rssi=int.from_bytes(params[25:26], "little", signed=True),
        payload_len=payload_len,
        trailing=bytes(params[31 + payload_len :]),
    )


@dataclass(frozen=True)
class RouteRecord:
    source: int
    lqi: int
    rssi: int
    relay_count: int


def parse_route_record(params: bytes) -> RouteRecord | None:
    # source u16, sourceEui64 8B, lastHopLqi u8, lastHopRssi i8, relayCount u8,
    # relayList u16 × relayCount
    if len(params) < 13 or len(params) != 13 + 2 * params[12]:
        return None
    return RouteRecord(
        source=_le16(params, 0),
        lqi=params[10],
        rssi=int.from_bytes(params[11:12], "little", signed=True),
        relay_count=params[12],
    )


@dataclass(frozen=True)
class NetworkStatus:
    code: int  # NWK status code (0x0c = many-to-one route failure, ...)
    target: int


def parse_network_status(params: bytes) -> NetworkStatus | None:
    # errorCode u8, target u16
    if len(params) != 3:
        return None
    return NetworkStatus(code=params[0], target=_le16(params, 1))


@dataclass(frozen=True)
class RouteError:
    status: int  # sl_status_t (0x0C14 = SL_STATUS_ZIGBEE_DELIVERY_FAILED, ...)
    target: int


def parse_route_error(params: bytes) -> RouteError | None:
    # sl_status u32, target u16
    if len(params) != 6:
        return None
    return RouteError(status=_le32(params, 0), target=_le16(params, 4))


def parse_counters(params: bytes) -> list[int] | None:
    """readCounters/readAndClearCounters response: a bare u16 array."""
    if len(params) < 40 or len(params) % 2:
        return None
    return [_le16(params, offset) for offset in range(0, len(params), 2)]
