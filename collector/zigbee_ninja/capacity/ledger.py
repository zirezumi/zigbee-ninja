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
import statistics
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

# Which cost model produced a stored row. Bump this whenever a change makes
# ledger µs non-comparable with previously stored µs; §V2-6 verification then
# refuses to grade an applied recommendation across the boundary instead of
# reading the re-pricing as a traffic change (a REGRESSED_RATIO of 1.25 would
# otherwise mark half the queue regressed the day a model lands).
#
#   1: unicast priced at the coordinator's own hop only.
#   2: unicast priced per §10 as hops x (frame + ACK + IFS) x (1 + retry_rate),
#      with hop depth from topology snapshots. Groupcast pricing is unchanged,
#      so only unicast-bearing rows move, and they move upward.
#
# A daily row that accumulated under more than one model records
# MIXED_PRICING_VERSION rather than the last writer's value.
PRICING_MODEL_VERSION = 2
MIXED_PRICING_VERSION = 0

# The device ledger prices autonomous reports arriving at the coordinator, a
# last-hop quantity the unicast hop model does not touch. It carries its own
# version so a command-pricing change never pauses reporting verdicts: each
# ledger tracks the model that actually prices it.
AUTONOMOUS_PRICING_MODEL_VERSION = 1

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
    n_routers: int,
    avg_tx: float | None,
    retry_rate: float | None,
    hops_from_topology: bool | None = None,
) -> dict:
    """Pricing context recorded on a ledger row: the values in force when the
    row was last written, and whether each came from a counter window or the
    model default. This is what lets a later parameter improvement be told
    apart from a real traffic change.

    Hop counts are per target, so a daily row (which aggregates many targets)
    records only whether a topology snapshot was available to price them; the
    alternative was a single hop number that would be wrong for most of the
    chains it covers. `hops_from_topology` is tri-state: True when depths were
    derived and used, False when a unicast was priced without them (the
    conservative default applied), and None when the pass priced no unicast at
    all and the question never arose. False and None are genuinely different
    facts to anyone later asking why a number moved.
    """
    return {
        "n_routers": n_routers,
        "avg_tx": round(avg_tx, 3) if avg_tx is not None else airtime.DEFAULT_AVG_TX,
        "avg_tx_measured": avg_tx is not None,
        "retry_rate": round(retry_rate, 4) if retry_rate is not None else 0.0,
        "retry_rate_measured": retry_rate is not None,
        "hops_from_topology": hops_from_topology,
        "pricing_version": PRICING_MODEL_VERSION,
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
    hops: int = 1,
) -> ChainPrice:
    """Model the airtime one command chain cost the mesh.

    TX: one groupcast amplified across the router census for a group target,
    else one unicast scaled by the measured MAC retry rate and by `hops`, the
    target's route depth. Both sides of that choice are now mesh-wide
    quantities: pricing the groupcast across every router while pricing the
    unicast at the coordinator's own hop made the two incomparable and
    systematically flattered any change that replaced a groupcast with
    unicasts. `hops` defaults to 1 so a caller that does not know the route
    gets the coordinator hop only.

    This is deliberately a different question from what ninja-tap measures:
    the wire tier taps the coordinator's link and physically cannot observe a
    relay, so it prices what it saw (one hop) while the ledger prices what the
    mesh spent. The two are separate currencies by design (§10); do not
    "reconcile" them.

    RX: each state echo as one report frame arriving at the coordinator (last
    hop only, matching the §10 RX accounting).
    """
    payload = ZCL_GET_BYTES if verb == "get" else ZCL_SET_BYTES
    effective_avg_tx = avg_tx if avg_tx is not None else airtime.DEFAULT_AVG_TX
    effective_retry = retry_rate if retry_rate is not None else 0.0
    if group_target:
        tx = airtime.groupcast_airtime_us(payload, n_routers, avg_tx=effective_avg_tx)
    else:
        tx = airtime.unicast_airtime_us(payload, retry_rate=effective_retry, hops=hops)
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
            "hops": None if group_target else hops,
            "payload_bytes": payload,
        },
    )


def autonomous_publish_cost_us() -> float:
    """Modeled cost of one device-initiated report reaching the coordinator."""
    return airtime.incoming_airtime_us(ZCL_REPORT_BYTES, group_addressed=False, acked=True)


# -- read side (GET /api/ledger) ---------------------------------------------

TOP_LIMIT = 25

# Cost baselines (V2_PROPOSAL.md §V2-4): rolling median of µs/day over the
# completed days the ledger was recording, gated on a minimum history so a
# fresh deployment freezes regression rules instead of alerting on noise.
BASELINE_DAYS = 14
MIN_BASELINE_DAYS = 3


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


def _recording_since(conn) -> float | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'ledger_since'").fetchone()
    return float(json.loads(row["value"])) if row else None


def _daily_us_maps(
    conn, days: list[str]
) -> tuple[dict[tuple[str, str], dict[str, float]], dict[tuple[str, str], dict[str, float]]]:
    """µs per day keyed by (instance, commander) and (instance, device)."""
    placeholders = ",".join("?" * len(days))
    commanders: dict[tuple[str, str], dict[str, float]] = {}
    devices: dict[tuple[str, str], dict[str, float]] = {}
    for row in conn.execute(
        f"SELECT instance, day, commander, tx_us + rx_us AS us "
        f"FROM ledger_daily WHERE day IN ({placeholders})",
        days,
    ):
        per_day = commanders.setdefault((row["instance"], row["commander"]), {})
        per_day[row["day"]] = per_day.get(row["day"], 0.0) + row["us"]
    for row in conn.execute(
        f"SELECT instance, day, device, autonomous_us AS us "
        f"FROM ledger_device_daily WHERE day IN ({placeholders})",
        days,
    ):
        per_day = devices.setdefault((row["instance"], row["device"]), {})
        per_day[row["day"]] = per_day.get(row["day"], 0.0) + row["us"]
    return commanders, devices


class _Baseline:
    """Trailing-24 h cost and its ratio to the rolling median of completed
    recording days. The trailing estimate is today's accumulation plus
    yesterday weighted by the fraction of yesterday still inside the window
    (the ledger is daily, so a finer slice does not exist)."""

    def __init__(self, now: float, recording_since: float):
        self.today = utc_day(now)
        self.yesterday = utc_day(now - 86400.0)
        days = _days_covering(now - (BASELINE_DAYS + 1) * 86400.0, now)
        since_day = utc_day(recording_since)
        self.completed = [day for day in days if since_day < day < self.today][-BASELINE_DAYS:]
        elapsed_today = now - _day_start_epoch(self.today)
        self.yesterday_weight = max(0.0, 1.0 - elapsed_today / 86400.0)
        self.rate_denominator = min(86400.0, max(1.0, now - recording_since))
        self.days = days

    def trailing_us(self, per_day: dict[str, float]) -> float:
        return per_day.get(self.today, 0.0) + (
            per_day.get(self.yesterday, 0.0) * self.yesterday_weight
        )

    def us_per_s(self, per_day: dict[str, float]) -> float:
        return self.trailing_us(per_day) / self.rate_denominator

    def ratio(self, per_day: dict[str, float]) -> float | None:
        """None (freeze) until enough history, or when the median is zero:
        a spender with no baseline cannot regress against it. Reports only
        once MIN_BASELINE_DAYS completed days exist, so trailing_us always
        covers a full day and divides the median like for like."""
        if len(self.completed) < MIN_BASELINE_DAYS:
            return None
        median = statistics.median(per_day.get(day, 0.0) for day in self.completed)
        if median <= 0.0:
            return None
        return self.trailing_us(per_day) / median


def cost_metrics(db, now: float | None = None) -> dict[str, dict[str, float]]:
    """Samples for the alert evaluator (V2_PROPOSAL.md §V2-4): regression
    ratios keyed by commander / device name (aggregated across instances,
    since an automation's cost spans coordinators) and budget rates. Keys
    absent = frozen: no history yet, or nothing to baseline against.
    zigbee-ninja's own spend is excluded from the regression ratio (benchmark
    runs are operator-initiated, not drift) but visible to budget rules."""
    now = now if now is not None else time.time()
    conn = db.connect()
    recording_since = _recording_since(conn)
    if recording_since is None:
        return {}
    baseline = _Baseline(now, recording_since)
    commander_rows, device_rows = _daily_us_maps(conn, baseline.days)

    by_commander: dict[str, dict[str, float]] = {}
    for (_instance, commander), per_day in commander_rows.items():
        merged = by_commander.setdefault(commander, {})
        for day, us in per_day.items():
            merged[day] = merged.get(day, 0.0) + us
    by_device: dict[str, dict[str, float]] = {}
    for (_instance, device), per_day in device_rows.items():
        merged = by_device.setdefault(device, {})
        for day, us in per_day.items():
            merged[day] = merged.get(day, 0.0) + us
    by_instance: dict[str, dict[str, float]] = {}
    for (instance, _key), per_day in [*commander_rows.items(), *device_rows.items()]:
        merged = by_instance.setdefault(instance, {})
        for day, us in per_day.items():
            merged[day] = merged.get(day, 0.0) + us

    out: dict[str, dict[str, float]] = {
        "commander_cost_ratio": {},
        "device_cost_ratio": {},
        "commander_cost_us_per_s": {},
        "instance_cost_us_per_s": {},
    }
    for commander, per_day in by_commander.items():
        out["commander_cost_us_per_s"][commander] = round(baseline.us_per_s(per_day), 3)
        if commander != SELF_COMMANDER:
            ratio = baseline.ratio(per_day)
            if ratio is not None:
                out["commander_cost_ratio"][commander] = round(ratio, 3)
    for device, per_day in by_device.items():
        ratio = baseline.ratio(per_day)
        if ratio is not None:
            out["device_cost_ratio"][device] = round(ratio, 3)
    for instance, per_day in by_instance.items():
        out["instance_cost_us_per_s"][instance] = round(baseline.us_per_s(per_day), 3)
    return {name: samples for name, samples in out.items() if samples}


def summary(db, seconds: int, now: float | None = None) -> dict:
    """Ledger rollup over the UTC days intersecting the window. The ledger is
    daily, so the window rounds out to whole days; rates divide by the
    elapsed wall clock since the earliest returned day began, bounded by
    when ledger recording actually started, and the response says which
    days and denominator it used."""
    now = now if now is not None else time.time()
    days = _days_covering(now - seconds, now)
    conn = db.connect()
    effective_start = _day_start_epoch(days[0])
    recording_since = _recording_since(conn)
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

    # Per-row trend: this row's trailing 24 h against its own 14-day median
    # (§V2-4 baselines at the row's own instance grain), None until history.
    if recording_since is not None:
        baseline = _Baseline(now, recording_since)
        commander_maps, device_maps = _daily_us_maps(conn, baseline.days)
        for entry in commander_rows:
            per_day = commander_maps.get((entry["instance"], entry["commander"]), {})
            ratio = baseline.ratio(per_day)
            entry["trend"] = round(ratio, 3) if ratio is not None else None
        for entry in device_rows:
            per_day = device_maps.get((entry["instance"], entry["device"]), {})
            ratio = baseline.ratio(per_day)
            entry["trend"] = round(ratio, 3) if ratio is not None else None
    else:
        for entry in [*commander_rows, *device_rows]:
            entry["trend"] = None

    instance_totals: dict[str, float] = {}
    for entry in commander_rows:
        instance_totals[entry["instance"]] = (
            instance_totals.get(entry["instance"], 0.0) + entry["total_us"]
        )
    for entry in device_rows:
        instance_totals[entry["instance"]] = (
            instance_totals.get(entry["instance"], 0.0) + entry["autonomous_us"]
        )
    instances = {
        instance: {"total_us": round(total, 1), **_rates(total, effective_seconds)}
        for instance, total in sorted(instance_totals.items())
    }

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
        "instances": instances,
        "totals": totals,
    }
