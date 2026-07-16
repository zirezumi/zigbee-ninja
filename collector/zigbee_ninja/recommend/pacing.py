"""Pacing advisor (V2_PROPOSAL.md §V2-5 detector 4).

Burst microscopy over the recorded command chains: find commanders whose
bursts push a coordinator toward its measured capacity limit, or push a
single device past its measured service ceiling, and say how far to spread
the burst. Both denominators are read from this installation's calibration
records (capacity/headroom.latest_knees), never assumed.

The predicted p95 improvement interpolates the latency-vs-load points this
mesh has actually produced: the calibration ramps' per-step curves plus the
natural 10 s wire-latency rollups. Inside the observed load range that
prediction is measured interpolation; a burst rate beyond every observed
point is reported against the highest measured point and tagged modeled.

Pacing does not recover airtime (a spread burst transmits the same frames),
so the saving is a latency number: ``saving.p95_ms`` carries the predicted
p95 improvement and ``us_per_s`` stays 0.
"""

from __future__ import annotations

import json
import math
import statistics

from .context import DetectorContext
from .store import Finding

NAME = "pacing"

# A burst is a run of commands with gaps under BURST_GAP_SECONDS; smaller
# clusters than MIN_BURST_COMMANDS cannot meaningfully load a coordinator.
BURST_GAP_SECONDS = 1.0
MIN_BURST_COMMANDS = 8
# Aggregate pressure: peak 1 s rate at or above this share of the capacity
# limit. Per-device pressure: exceeding the measured service ceiling at all.
PRESSURE_FRACTION = 0.8
# Proposed stagger paces the burst to this share of the capacity limit.
PACED_FRACTION = 0.5
MAJORITY_FRACTION = 0.6
EVIDENCE_BURSTS = 5
MULTIPLE_COMMANDERS = "(multiple commanders)"
UNATTRIBUTED = "(unattributed)"


def _peak_1s(times: list[float]) -> tuple[float, int]:
    """Highest command count inside any sliding 1 s span (count, as eps)."""
    peak = 1
    left = 0
    for right in range(len(times)):
        while times[right] - times[left] > 1.0:
            left += 1
        peak = max(peak, right - left + 1)
    return float(peak), peak


def _bursts(rows: list) -> list[dict]:
    """Cluster one instance's commands into bursts by inter-command gap."""
    bursts: list[dict] = []
    cluster: list = []
    for row in rows:
        if cluster and row["opened_at"] - cluster[-1]["opened_at"] > BURST_GAP_SECONDS:
            if len(cluster) >= MIN_BURST_COMMANDS:
                bursts.append(_burst_stats(cluster))
            cluster = []
        cluster.append(row)
    if len(cluster) >= MIN_BURST_COMMANDS:
        bursts.append(_burst_stats(cluster))
    return bursts


def _burst_stats(cluster: list) -> dict:
    times = [row["opened_at"] for row in cluster]
    commanders: dict[str, int] = {}
    per_target: dict[str, list[float]] = {}
    for row in cluster:
        commanders[row["client"] or UNATTRIBUTED] = (
            commanders.get(row["client"] or UNATTRIBUTED, 0) + 1
        )
        per_target.setdefault(row["target"], []).append(row["opened_at"])
    peak_eps, _ = _peak_1s(times)
    device_peaks = {
        target: _peak_1s(sorted(stamps))[0]
        for target, stamps in per_target.items()
        if len(stamps) > 1
    }
    return {
        "start": times[0],
        "end": times[-1],
        "commands": len(cluster),
        "duration_s": max(times[-1] - times[0], 0.001),
        "peak_eps": peak_eps,
        "commanders": commanders,
        "device_peaks": device_peaks,
    }


def _commander_label(commanders: dict[str, int]) -> str:
    total = sum(commanders.values())
    top, top_count = max(commanders.items(), key=lambda item: item[1])
    return top if top_count / total >= MAJORITY_FRACTION else MULTIPLE_COMMANDERS


# -- measured latency-vs-load curves ------------------------------------------------


def _curve_points(
    conn, instance: str, now: float, kind: str
) -> list[tuple[float, float]]:
    """(eps, p95_ms) points this mesh has produced. kind 'wire' is the
    coordinator-aggregate curve (ramp wire p95 + natural 10 s rollups);
    kind 'echo' is the per-device queue curve (single-target ramp echoes)."""
    points: list[tuple[float, float]] = []
    for row in conn.execute(
        "SELECT detail FROM calibrations WHERE instance = ? AND status = 'completed' "
        "ORDER BY id DESC LIMIT 6",
        (instance,),
    ):
        detail = json.loads(row["detail"])
        mode = (detail.get("plan") or {}).get("mode", "single")
        for step in detail.get("steps") or []:
            eps = step.get("achieved_eps")
            if not eps:
                continue
            if kind == "wire":
                p95 = step.get("wire_p95_ms")
            else:
                p95 = step.get("echo_p95_ms") if mode == "single" else None
            if p95:
                points.append((float(eps), float(p95)))
    if kind == "wire":
        for row in conn.execute(
            "SELECT a.eps AS eps, l.p95_ms AS p95_ms FROM "
            "(SELECT ts, SUM(CASE WHEN bucket IN ('tx_unicast', 'tx_groupcast') "
            "THEN frames ELSE 0 END) / 10.0 AS eps "
            "FROM airtime_10s WHERE instance = ? AND ts >= ? GROUP BY ts) a "
            "JOIN latency_10s l ON l.ts = a.ts AND l.instance = ?",
            (instance, int(now - 86400), instance),
        ):
            if row["eps"] and row["p95_ms"]:
                points.append((float(row["eps"]), float(row["p95_ms"])))
    return points


def _binned(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    bins: dict[int, list[float]] = {}
    for eps, p95 in points:
        bins.setdefault(int(round(eps)), []).append(p95)
    return sorted((float(eps), statistics.median(values)) for eps, values in bins.items())


def p95_at(points: list[tuple[float, float]], rate: float) -> tuple[float, bool] | None:
    """Interpolated p95 at a load, or None with no points. The bool flags a
    rate beyond every observed point (reported at the highest measured
    point: a floor, not a prediction)."""
    curve = _binned(points)
    if not curve:
        return None
    if rate >= curve[-1][0]:
        return curve[-1][1], rate > curve[-1][0]
    if rate <= curve[0][0]:
        return curve[0][1], False
    for (x0, y0), (x1, y1) in zip(curve, curve[1:], strict=False):
        if x0 <= rate <= x1:
            span = x1 - x0
            fraction = (rate - x0) / span if span > 0 else 0.0
            return y0 + (y1 - y0) * fraction, False
    return curve[-1][1], True


# -- the detector -------------------------------------------------------------------


def _knee_entry(ctx: DetectorContext, instance: str, mode: str) -> dict | None:
    entry = (ctx.knees.get(instance) or {}).get(mode)
    if not entry or not entry.get("eps"):
        return None
    info = ctx.instance_info.get(instance) or {}
    environment = entry.get("environment") or {}
    stale = bool(info) and (
        info.get("version"),
        info.get("coordinator_revision"),
    ) != (environment.get("z2m_version"), environment.get("coordinator_revision"))
    return {**entry, "stale_environment": stale}


def detect(ctx: DetectorContext) -> list[Finding]:
    rows = ctx.conn.execute(
        "SELECT instance, target, verb, opened_at, client FROM chains "
        "WHERE opened_at >= ? ORDER BY instance, opened_at",
        (ctx.window_start(),),
    ).fetchall()
    by_instance: dict[str, list] = {}
    for row in rows:
        by_instance.setdefault(row["instance"], []).append(row)

    findings: list[Finding] = []
    for instance, commands in by_instance.items():
        aggregate_knee = _knee_entry(ctx, instance, "spread")
        device_knee = _knee_entry(ctx, instance, "single")
        if aggregate_knee is None and device_knee is None:
            continue  # no measured denominator: nothing to judge against

        flagged: dict[str, list[dict]] = {}
        for burst in _bursts(commands):
            pressured = (
                aggregate_knee is not None
                and burst["peak_eps"] >= PRESSURE_FRACTION * aggregate_knee["eps"]
            )
            overloads = (
                {
                    target: peak
                    for target, peak in burst["device_peaks"].items()
                    if peak > device_knee["eps"]
                }
                if device_knee is not None
                else {}
            )
            if not pressured and not overloads:
                continue
            burst["pressured"] = pressured
            burst["overloads"] = overloads
            flagged.setdefault(_commander_label(burst["commanders"]), []).append(burst)

        for commander, bursts in flagged.items():
            findings.append(
                _finding(ctx, instance, commander, bursts, aggregate_knee, device_knee)
            )
    return findings


def _finding(
    ctx: DetectorContext,
    instance: str,
    commander: str,
    bursts: list[dict],
    aggregate_knee: dict | None,
    device_knee: dict | None,
) -> Finding:
    worst = max(bursts, key=lambda burst: burst["peak_eps"])
    pressured = [burst for burst in bursts if burst["pressured"]]
    overloaded = [burst for burst in bursts if burst["overloads"]]
    knee = aggregate_knee or device_knee
    assert knee is not None

    # Proposed stagger: spread the worst burst to PACED_FRACTION of the limit.
    paced_eps = PACED_FRACTION * knee["eps"]
    stagger_ms = int(math.ceil(worst["commands"] / paced_eps * 1000.0))

    sentences: list[str] = []
    if pressured:
        worst_pressured = max(pressured, key=lambda burst: burst["peak_eps"])
        sentences.append(
            f"{commander} sent {worst_pressured['commands']} commands in "
            f"{worst_pressured['duration_s']:.1f} s (peak {worst_pressured['peak_eps']:.0f}/s; "
            f"the measured capacity limit is {knee['eps']:.0f}/s)."
        )
    if overloaded:
        worst_overload = max(overloaded, key=lambda burst: max(burst["overloads"].values()))
        target, peak = max(worst_overload["overloads"].items(), key=lambda item: item[1])
        sentences.append(
            f"Commands to {target} peaked at {peak:.0f}/s; one device services about "
            f"{device_knee['eps']:.0f}/s, so the rest wait in its queue."
        )
    if len(bursts) > 1:
        sentences.append(f"{len(bursts)} bursts like this in the last 24 h.")
    sentences.append(
        f"Spreading each burst over at least {stagger_ms / 1000.0:.1f} s would keep it "
        f"under half the limit."
    )

    # Predicted p95 improvement on this mesh's own latency curve.
    curve_kind = "wire" if pressured else "echo"
    points = _curve_points(ctx.conn, instance, ctx.now, curve_kind)
    peak_estimate = p95_at(points, worst["peak_eps"])
    paced_estimate = p95_at(points, paced_eps)
    saving: dict = {"us_per_s": 0.0, "pct_of_budget": 0.0}
    if peak_estimate is not None and paced_estimate is not None:
        peak_p95, beyond = peak_estimate
        paced_p95, _ = paced_estimate
        improvement = round(peak_p95 - paced_p95, 1)
        if improvement > 0:
            saving["p95_ms"] = improvement
            if beyond:
                saving["basis"] = (
                    "burst rate beyond every measured point; improvement floored at "
                    "the highest measured load"
                )
                saving["provenance"] = "modeled"
            else:
                saving["basis"] = (
                    "interpolated on this mesh's measured latency-vs-load curve "
                    f"({len(points)} points)"
                )
                saving["provenance"] = "measured"
            sentences.append(
                f"Predicted p95 latency: about {peak_p95:.0f} ms at the peak vs "
                f"{paced_p95:.0f} ms paced."
            )
    if "basis" not in saving:
        saving["basis"] = "no measured latency coverage at these rates"
        saving["provenance"] = "modeled"

    confidence = "high"
    if knee is device_knee and aggregate_knee is None:
        confidence = "medium"  # judging aggregate pressure with a per-device limit
    if knee.get("stale_environment"):
        confidence = "medium"
        sentences.append(
            "The capacity limit was calibrated under a different Zigbee2MQTT or "
            "firmware version; consider recalibrating."
        )
    if len(bursts) < 3:
        confidence = "medium" if confidence == "high" else "low"
    if saving.get("provenance") == "modeled" and "p95_ms" in saving:
        confidence = "medium" if confidence == "high" else confidence

    evidence: list[dict] = [
        {
            "kind": "window",
            "instance": instance,
            "start": round(burst["start"], 3),
            "end": round(burst["end"], 3),
            "commands": burst["commands"],
            "peak_eps": burst["peak_eps"],
            "commanders": burst["commanders"],
        }
        for burst in sorted(bursts, key=lambda b: b["peak_eps"], reverse=True)[
            :EVIDENCE_BURSTS
        ]
    ]
    evidence.append(
        {
            "kind": "capacity_limit",
            "mode": knee["mode"],
            "eps": knee["eps"],
            "measured_at": knee.get("measured_at"),
            "stale_environment": knee.get("stale_environment", False),
        }
    )

    return Finding(
        detector=NAME,
        instance=instance,
        subject=commander,
        finding=" ".join(sentences),
        action={
            "kind": "pace",
            "commander": commander,
            "instance": instance,
            "commands": worst["commands"],
            "over_ms": int(worst["duration_s"] * 1000),
            "stagger_ms": stagger_ms,
            "target_eps": round(paced_eps, 1),
        },
        saving=saving,
        confidence=confidence,
        evidence=evidence,
        fingerprint={
            "peak_eps": round(worst["peak_eps"], 1),
            "commands": worst["commands"],
            "bursts": len(bursts),
            "device_breaches": len(overloaded),
        },
    )
