"""Per-frame 802.15.4 airtime model (DESIGN.md §10).

2.4 GHz O-QPSK runs 250 kbps → 32 µs per byte. PSDU sizes are reconstructed
from the APS payload length seen at the EZSP boundary plus deterministic
MAC/NWK/APS header + security arithmetic (Zigbee PRO, NWK security with the
extended-nonce aux header):

    MAC data header 9 + FCS 2
    NWK data header 8 + NWK aux security header 14 + MIC 4
    APS data header: unicast 8 / group-addressed 9

TX unicasts from a concentrator may additionally carry a source-route subframe
(2 + 2·relays bytes) that never crosses the EZSP boundary, so TX PSDUs are
lower-bound reconstructions; RX frames carry no such subframe. Airtime derived
this way is provenance "reconstructed" (DESIGN.md P5).

The §10 CSMA-backoff term defaults to 0 µs before calibration: mean backoff is
idle listening rather than channel occupancy, and the channel-budget
denominator's η already discounts CSMA overhead — a calibrated per-mesh factor
can replace either knob later without double counting.
"""

from __future__ import annotations

US_PER_BYTE = 32.0
PHY_OVERHEAD_BYTES = 6  # preamble 4 + SFD 1 + PHR 1

ACK_AIRTIME_US = 352.0  # 11 bytes on air, unicast data frames only
SIFS_US = 192.0
LIFS_US = 640.0
SIFS_MAX_PSDU = 18  # aMaxSIFSFrameSize

UNICAST_OVERHEAD_BYTES = 45  # MAC 9 + NWK 8 + aux 14 + APS 8 + MIC 4 + FCS 2
GROUPCAST_OVERHEAD_BYTES = 46  # group-addressed APS header is 9 bytes
NWK_COMMAND_OVERHEAD_BYTES = 37  # MAC 9 + NWK 8 + aux 14 + MIC 4 + FCS 2

DEFAULT_AVG_TX = 1.3  # broadcast passive-ack retransmissions per router, §10
CHANNEL_ETA = 0.7  # channel-budget CSMA efficiency, §10 denominator 1
CHANNEL_BUDGET_US_PER_S = 1_000_000.0 * CHANNEL_ETA

PROVENANCE = "reconstructed"


def frame_airtime_us(psdu_len: int, *, acked: bool = False) -> float:
    """On-air cost of one frame: PHY+PSDU, its IFS, and the MAC ACK if any."""
    airtime = (PHY_OVERHEAD_BYTES + psdu_len) * US_PER_BYTE
    airtime += LIFS_US if psdu_len > SIFS_MAX_PSDU else SIFS_US
    if acked:
        airtime += ACK_AIRTIME_US
    return airtime


def unicast_airtime_us(aps_payload_len: int, retry_rate: float = 0.0) -> float:
    """One unicast data frame + its MAC ACK (single hop), scaled by the
    coordinator's measured MAC retry rate: each retry re-burns the frame,
    its IFS, and the ACK wait (§10 unicast cost, (1 + retry_rate) term).
    Defaults to 0 until counter windows produce a measured rate."""
    per_attempt = frame_airtime_us(UNICAST_OVERHEAD_BYTES + aps_payload_len, acked=True)
    return per_attempt * (1.0 + max(retry_rate, 0.0))


def groupcast_airtime_us(
    aps_payload_len: int, n_routers: int, avg_tx: float = DEFAULT_AVG_TX
) -> float:
    """Mesh-amplified group/broadcast cost: every router relays, no MAC ACKs.

    §10: (1 + N_routers) × frame_airtime × avg_tx, avg_tx ∈ [1, 3].
    """
    per_tx = frame_airtime_us(GROUPCAST_OVERHEAD_BYTES + aps_payload_len, acked=False)
    return per_tx * (1 + max(n_routers, 0)) * avg_tx


def incoming_airtime_us(aps_payload_len: int, *, group_addressed: bool, acked: bool) -> float:
    """Last-hop cost of a frame the coordinator received (earlier hops are
    invisible until topology-based hop expansion lands)."""
    overhead = GROUPCAST_OVERHEAD_BYTES if group_addressed else UNICAST_OVERHEAD_BYTES
    return frame_airtime_us(overhead + aps_payload_len, acked=acked)


def route_record_airtime_us(relay_count: int) -> float:
    """Many-to-one route record NWK command: opcode + count + u16 relay list."""
    payload = 2 + 2 * max(relay_count, 0)
    return frame_airtime_us(NWK_COMMAND_OVERHEAD_BYTES + payload, acked=True)


def network_status_airtime_us() -> float:
    """NWK status command frame: opcode + status code + u16 destination."""
    return frame_airtime_us(NWK_COMMAND_OVERHEAD_BYTES + 4, acked=True)
