"""Utilization, headroom, and the knee-validation scatter (DESIGN.md §10).

Joins the persisted 10 s rollups — airtime_10s (load: TX frames per window,
radio airtime per window) and latency_10s (the wire latency SLI) — with each
instance's latest completed calibration to report the §10 outputs:

- utilization per denominator, side by side: channel airtime budget, the
  calibrated knee, and the pipeline ceiling where the ramp saturated;
- steady headroom (knee − p95 load) and burst headroom (knee − max load),
  both at 10 s granularity (1 s burst windows stay in-memory only);
- the latency-vs-load scatter that validates the knee continuously from
  natural traffic — a capacity regression shows up as points bending upward
  well below the recorded knee.

Knee semantics are preserved, not flattened: a ramp that ended in driver
saturation measured the *pipeline* per-device ceiling (denominator 3) and
only lower-bounds the NCP knee; a censored ramp is likewise a lower bound.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from ..store.db import Database
from . import airtime

TX_BUCKETS = ("tx_unicast", "tx_groupcast")
SCATTER_MINUTE_AGG_ABOVE_SECONDS = 7200
ROLLUP_SECONDS = 10


def _percentile(ordered: list[float], fraction: float) -> float:
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def latest_knees(db: Database) -> dict[str, dict[str, dict]]:
    """Latest knee per (instance, calibration mode) — the modes measure
    different denominators: a single-target ramp finds the per-device pipeline
    ceiling, a spread ramp probes the NCP/global pipeline knee."""
    rows = db.connect().execute(
        "SELECT instance, knee_eps, finished_at, detail FROM calibrations "
        "WHERE status = 'completed' AND knee_eps IS NOT NULL ORDER BY id"
    ).fetchall()
    knees: dict[str, dict[str, dict]] = {}
    for row in rows:  # ordered by id → later rows overwrite = latest per mode
        detail = json.loads(row["detail"])
        plan = detail.get("plan") or {}
        mode = plan.get("mode", "single")
        knee = detail.get("knee") or {}
        breach = knee.get("breach")
        censored = bool(knee.get("censored"))
        # A saturated ramp measured a pipeline ceiling; the NCP knee is
        # bounded from below either way the ramp ended without a mesh breach.
        kind = (
            "pipeline_ceiling"
            if breach == "saturated"
            else ("lower_bound" if censored else "mesh_knee")
        )
        knees.setdefault(row["instance"], {})[mode] = {
            "eps": row["knee_eps"],
            "kind": kind,
            "mode": mode,
            "breach": breach,
            "censored": censored,
            "rtt_source": knee.get("rtt_source"),
            "target": plan.get("target"),
            "measured_at": row["finished_at"],
            "environment": detail.get("environment") or {},
        }
    return knees


def summarize(
    db: Database,
    seconds: int,
    instances_info: list[dict],
    clock: Callable[[], float] = time.time,
) -> dict:
    since = int(clock()) - seconds
    conn = db.connect()

    load_rows = conn.execute(
        "SELECT ts, instance, "
        "SUM(CASE WHEN bucket IN (?, ?) THEN frames ELSE 0 END) AS tx_frames, "
        "SUM(airtime_us) AS airtime_us "
        "FROM airtime_10s WHERE ts >= ? GROUP BY ts, instance",
        (*TX_BUCKETS, since),
    ).fetchall()
    latency_rows = conn.execute(
        "SELECT ts, instance, p95_ms FROM latency_10s WHERE ts >= ?", (since,)
    ).fetchall()
    latency_by_window = {(row["ts"], row["instance"]): row["p95_ms"] for row in latency_rows}

    # Benchmark windows are self-traffic and are excluded from every headroom
    # aggregate (§11.5) — otherwise each calibration ramp would masquerade as
    # a natural load peak and flatter the burst numbers. The ramp's own curve
    # lives in its calibration record; /api/airtime keeps raw physical totals.
    benchmark_windows: dict[str, list[tuple[float, float]]] = {}
    for row in conn.execute(
        "SELECT instance, started_at, finished_at FROM calibrations "
        "WHERE finished_at IS NOT NULL AND finished_at >= ?",
        (since,),
    ).fetchall():
        benchmark_windows.setdefault(row["instance"], []).append(
            (row["started_at"] - ROLLUP_SECONDS, row["finished_at"])
        )

    def in_benchmark(instance: str, ts: int) -> bool:
        return any(
            start <= ts <= end for start, end in benchmark_windows.get(instance, ())
        )

    per_instance: dict[str, dict] = {}
    for row in load_rows:
        entry = per_instance.setdefault(
            row["instance"],
            {"eps": [], "airtime_us": 0.0, "scatter": [], "excluded": 0},
        )
        if in_benchmark(row["instance"], row["ts"]):
            entry["excluded"] += 1
            continue
        eps = row["tx_frames"] / float(ROLLUP_SECONDS)
        entry["eps"].append(eps)
        entry["airtime_us"] += row["airtime_us"]
        p95 = latency_by_window.get((row["ts"], row["instance"]))
        if p95 is not None:
            entry["scatter"].append((row["ts"], eps, p95))

    current_env = {
        info["base_topic"]: (info.get("version"), info.get("coordinator_revision"))
        for info in instances_info
    }
    knees = latest_knees(db)

    instances: dict[str, dict] = {}
    for base in sorted(set(per_instance) | set(knees)):
        loads = per_instance.get(
            base, {"eps": [], "airtime_us": 0.0, "scatter": [], "excluded": 0}
        )
        eps_sorted = sorted(loads["eps"])
        rates = None
        if eps_sorted:
            rates = {
                "p50_eps": round(_percentile(eps_sorted, 0.50), 2),
                "p95_eps": round(_percentile(eps_sorted, 0.95), 2),
                "max_eps": round(eps_sorted[-1], 2),
                "windows": len(eps_sorted),
            }
        us_per_s = loads["airtime_us"] / seconds
        modes = knees.get(base, {})
        spread_knee = modes.get("spread")
        single_knee = modes.get("single")
        # Headline knee = aggregate capacity: the spread measurement when one
        # exists, else the single-target result (a per-device lower bound).
        knee = spread_knee or single_knee
        stale = False
        if knee is not None and base in current_env:
            environment = knee["environment"]
            stale = current_env[base] != (
                environment.get("z2m_version"),
                environment.get("coordinator_revision"),
            )
        headroom = None
        if knee is not None and rates is not None:
            headroom = {
                "steady_eps": round(knee["eps"] - rates["p95_eps"], 2),
                "burst_eps": round(knee["eps"] - rates["max_eps"], 2),
                "knee_utilization_pct": round(rates["p95_eps"] / knee["eps"] * 100.0, 1),
                "granularity": f"{ROLLUP_SECONDS}s windows",
            }

        scatter = loads["scatter"]
        if seconds > SCATTER_MINUTE_AGG_ABOVE_SECONDS and scatter:
            by_minute: dict[int, list[tuple[float, float]]] = {}
            for ts, eps, p95 in scatter:
                by_minute.setdefault(ts // 60, []).append((eps, p95))
            scatter = [
                (
                    minute * 60,
                    round(sum(eps for eps, _ in points) / len(points), 2),
                    max(p95 for _, p95 in points),
                )
                for minute, points in sorted(by_minute.items())
            ]

        instances[base] = {
            "knee": None
            if knee is None
            else {**knee, "stale_environment": stale},
            "denominators": {
                "channel_budget": {
                    "us_per_s": round(us_per_s, 1),
                    "pct": round(us_per_s / airtime.CHANNEL_BUDGET_US_PER_S * 100.0, 3),
                    "provenance": airtime.PROVENANCE,
                },
                # Denominator 2 comes from the spread ramp when one has run;
                # a single-target result only bounds it from below.
                "ncp_knee": None
                if knee is None
                else {
                    "eps": knee["eps"],
                    "provenance": "measured"
                    if knee["kind"] == "mesh_knee"
                    else "lower_bound",
                },
                # Denominator 3, per device, from the single-target ramp.
                "pipeline": None
                if single_knee is None or single_knee["kind"] != "pipeline_ceiling"
                else {"eps": single_knee["eps"], "provenance": "measured"},
            },
            "rates": rates,
            "headroom": headroom,
            "scatter": [
                {"eps": eps, "p95_ms": p95} for _, eps, p95 in scatter
            ],
            "benchmark_windows_excluded": loads["excluded"],
        }
    return {"window_seconds": seconds, "instances": instances}
