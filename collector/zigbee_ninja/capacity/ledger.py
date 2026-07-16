"""Chain airtime pricing for the V2 cost ledger (V2_PROPOSAL.md §V2-2).

Prices are modeled at T0 fidelity. The command's MQTT payload stands in for
the ZCL payload through fixed byte estimates (DESIGN.md §10 calls this path
"inferred"); structure comes from the registry (group or device target,
router census) and from the measured per-flow parameters (avg_tx, MAC retry
rate) when the wire has produced them. Every price carries its provenance
and the parameters used, so a later parameter change is distinguishable
from a real traffic change.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import airtime

# Typical ZCL payload sizes for the message shapes chains observe. A set
# carrying on/off, level, and a transition lands near 12 bytes; a get is a
# short attribute-id list; a state report echoes roughly what a set carries.
ZCL_SET_BYTES = 12
ZCL_GET_BYTES = 4
ZCL_REPORT_BYTES = 12

PROVENANCE = "inferred (T0 payload estimate)"
AUTONOMOUS_PROVENANCE = "modeled (report size estimate)"

# Commander labels for ledger rows without an attributed client.
UNATTRIBUTED = "(unattributed)"
SELF_COMMANDER = "zigbee-ninja"

# Daily rows are tiny (instances x commanders), so the ledger keeps a year:
# enough history for any regression baseline the alert engine grows.
RETENTION_DAYS = 365


def utc_day(ts: float) -> str:
    """UTC calendar day a timestamp falls in; the ledger's rollup key."""
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def instance_params(
    n_routers: int, avg_tx: float | None, retry_rate: float | None
) -> dict:
    """Pricing context recorded on a ledger row: the values in force when the
    row was last written, and whether each came from a counter window or the
    model default. This is what lets a later parameter improvement be told
    apart from a real traffic change."""
    return {
        "n_routers": n_routers,
        "avg_tx": round(avg_tx, 3) if avg_tx is not None else airtime.DEFAULT_AVG_TX,
        "avg_tx_measured": avg_tx is not None,
        "retry_rate": round(retry_rate, 4) if retry_rate is not None else 0.0,
        "retry_rate_measured": retry_rate is not None,
    }


@dataclass(frozen=True)
class ChainPrice:
    tx_us: float
    rx_us: float
    provenance: str
    params: dict

    @property
    def total_us(self) -> float:
        return self.tx_us + self.rx_us


def price_chain(
    *,
    verb: str,
    group_target: bool,
    n_routers: int,
    echo_count: int,
    avg_tx: float | None = None,
    retry_rate: float | None = None,
) -> ChainPrice:
    """Model the airtime one command chain cost the mesh.

    TX: one groupcast amplified across the router census for a group target,
    else one unicast scaled by the measured MAC retry rate. RX: each state
    echo as one report frame arriving at the coordinator (last hop only,
    matching the §10 RX accounting).
    """
    payload = ZCL_GET_BYTES if verb == "get" else ZCL_SET_BYTES
    effective_avg_tx = avg_tx if avg_tx is not None else airtime.DEFAULT_AVG_TX
    effective_retry = retry_rate if retry_rate is not None else 0.0
    if group_target:
        tx = airtime.groupcast_airtime_us(payload, n_routers, avg_tx=effective_avg_tx)
    else:
        tx = airtime.unicast_airtime_us(payload, retry_rate=effective_retry)
    rx = max(0, echo_count) * airtime.incoming_airtime_us(
        ZCL_REPORT_BYTES, group_addressed=False, acked=True
    )
    return ChainPrice(
        tx_us=tx,
        rx_us=rx,
        provenance=PROVENANCE,
        params={
            "group_target": group_target,
            "n_routers": n_routers if group_target else 0,
            "avg_tx": round(effective_avg_tx, 3) if group_target else None,
            "retry_rate": round(effective_retry, 4) if not group_target else None,
            "payload_bytes": payload,
        },
    )


def autonomous_publish_cost_us() -> float:
    """Modeled cost of one device-initiated report reaching the coordinator."""
    return airtime.incoming_airtime_us(ZCL_REPORT_BYTES, group_addressed=False, acked=True)
