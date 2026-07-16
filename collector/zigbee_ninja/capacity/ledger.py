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

import calendar
import json
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


# -- read side (GET /api/ledger) ---------------------------------------------

TOP_LIMIT = 25


def _day_start_epoch(day: str) -> float:
    return float(calendar.timegm(time.strptime(day, "%Y-%m-%d")))


def _days_covering(start: float, end: float) -> list[str]:
    """UTC day strings intersecting [start, end], oldest first."""
    days = [utc_day(start)]
    t = start
    last = utc_day(end)
    while days[-1] != last:
        t += 86400.0
        day = utc_day(min(t, end))
        if day != days[-1]:
            days.append(day)
    return days


def _rates(total_us: float, effective_seconds: float) -> dict:
    us_per_s = total_us / effective_seconds if effective_seconds > 0 else 0.0
    return {
        "us_per_s": round(us_per_s, 3),
        "pct_of_budget": round(us_per_s / airtime.CHANNEL_BUDGET_US_PER_S * 100.0, 6),
    }


def summary(db, seconds: int) -> dict:
    """Ledger rollup over the UTC days intersecting the window. The ledger is
    daily, so the window rounds out to whole days; rates divide by the
    elapsed wall clock since the earliest returned day began, bounded by
    when ledger recording actually started, and the response says which
    days and denominator it used."""
    now = time.time()
    days = _days_covering(now - seconds, now)
    conn = db.connect()
    effective_start = _day_start_epoch(days[0])
    since_row = conn.execute(
        "SELECT value FROM settings WHERE key = 'ledger_since'"
    ).fetchone()
    recording_since = float(json.loads(since_row["value"])) if since_row else None
    if recording_since is not None:
        effective_start = max(effective_start, recording_since)
    effective_seconds = max(1.0, now - effective_start)
    placeholders = ",".join("?" * len(days))

    commanders: dict[tuple[str, str], dict] = {}
    for row in conn.execute(
        f"SELECT instance, day, commander, chains, tx_us, rx_us, provenance, params "
        f"FROM ledger_daily WHERE day IN ({placeholders}) ORDER BY day",
        days,
    ):
        entry = commanders.setdefault(
            (row["instance"], row["commander"]),
            {
                "instance": row["instance"],
                "commander": row["commander"],
                "chains": 0,
                "tx_us": 0.0,
                "rx_us": 0.0,
            },
        )
        entry["chains"] += row["chains"]
        entry["tx_us"] += row["tx_us"]
        entry["rx_us"] += row["rx_us"]
        # Rows arrive day-ascending; the latest day's pricing context wins.
        entry["provenance"] = row["provenance"]
        entry["params"] = json.loads(row["params"] or "{}")

    devices: dict[tuple[str, str], dict] = {}
    for row in conn.execute(
        f"SELECT instance, day, device, publishes, autonomous_us, provenance "
        f"FROM ledger_device_daily WHERE day IN ({placeholders}) ORDER BY day",
        days,
    ):
        entry = devices.setdefault(
            (row["instance"], row["device"]),
            {
                "instance": row["instance"],
                "device": row["device"],
                "publishes": 0,
                "autonomous_us": 0.0,
            },
        )
        entry["publishes"] += row["publishes"]
        entry["autonomous_us"] += row["autonomous_us"]
        entry["provenance"] = row["provenance"]

    commander_rows = sorted(
        commanders.values(), key=lambda e: e["tx_us"] + e["rx_us"], reverse=True
    )
    device_rows = sorted(devices.values(), key=lambda e: e["autonomous_us"], reverse=True)
    for entry in commander_rows:
        entry["total_us"] = entry["tx_us"] + entry["rx_us"]
        entry.update(_rates(entry["total_us"], effective_seconds))
    for entry in device_rows:
        entry.update(_rates(entry["autonomous_us"], effective_seconds))

    commanded_us = sum(e["total_us"] for e in commander_rows)
    autonomous_us = sum(e["autonomous_us"] for e in device_rows)
    totals = {
        "chains": sum(e["chains"] for e in commander_rows),
        "tx_us": sum(e["tx_us"] for e in commander_rows),
        "rx_us": sum(e["rx_us"] for e in commander_rows),
        "autonomous_publishes": sum(e["publishes"] for e in device_rows),
        "autonomous_us": autonomous_us,
        "total_us": commanded_us + autonomous_us,
        **_rates(commanded_us + autonomous_us, effective_seconds),
    }

    return {
        "window_seconds": seconds,
        "days": days,
        "effective_seconds": round(effective_seconds, 1),
        "recording_since": recording_since,
        "commander_count": len(commander_rows),
        "device_count": len(device_rows),
        "commanders": commander_rows[:TOP_LIMIT],
        "devices": device_rows[:TOP_LIMIT],
        "totals": totals,
    }
