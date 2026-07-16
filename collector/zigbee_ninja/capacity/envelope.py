"""Burst envelope analysis (V2_PROPOSAL.md §V2-5, the rebalancing core).

Steady-state averages understate what binds a mesh: bursts do. This module
characterizes each coordinator's load as fine-grained peaks and composes
worst-case envelopes from recorded traffic:

- the measured peak: 1 s and 10 s peak TX command rates from the raw event
  store's wire stream (send frames crossing the EZSP boundary), refined to
  a sliding 1 s window around the busiest seconds; an instance without wire
  coverage falls back to T0 command chains, tagged accordingly;
- per-commander worst observed bursts from the recorded chains;
- composed worst cases: commander sets observed bursting concurrently on an
  instance, each member priced at its own worst burst, and single commanders
  observed fanning out across several coordinators at once (the number a
  consolidation what-if must survive);
- everything judged against the calibrated capacity limits: the sustained
  limit and the hard ceiling the spread ramp actually achieved.

Benchmark windows are excluded from every aggregate (DESIGN.md §11.5).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable

from ..store.db import Database
from ..store.events import RawEventLog
from . import headroom

WIRE_TX_KINDS = ("sendUnicast", "sendMulticast", "sendBroadcast")
# Chains cluster into a burst while inter-command gaps stay under the gap;
# clusters below the minimum cannot meaningfully load a coordinator.
BURST_GAP_SECONDS = 1.0
MIN_BURST_COMMANDS = 4
# Bursts whose padded spans overlap count as concurrent for composition.
COFIRE_PAD_SECONDS = 0.5
TOP_BURSTS = 5
TOP_COMMANDERS = 8
# The busiest fixed 1 s bins get an exact sliding-window refinement pass; a
# burst straddling a bin boundary is undercounted by fixed bins alone.
PEAK_REFINE_BINS = 12
UNATTRIBUTED = "(unattributed)"
WIRE_PROVENANCE = "measured (wire frames)"
COMMANDS_PROVENANCE = "inferred (T0 commands; no wire coverage)"


def _sliding_peak(times: list[float], span: float = 1.0) -> tuple[float, float]:
    """Highest event count inside any sliding window of ``span`` seconds,
    with that window's start time. ``times`` must be sorted."""
    if not times:
        return 0.0, 0.0
    best, best_at = 1, times[0]
    left = 0
    for right in range(len(times)):
        while times[right] - times[left] > span:
            left += 1
        if right - left + 1 > best:
            best = right - left + 1
            best_at = times[left]
    return float(best), best_at


def _benchmark_windows(conn, since: float) -> dict[str, list[tuple[float, float]]]:
    """Calibration spans per instance, padded a second each side; traffic
    inside them is self-generated and stays out of the envelope (§11.5)."""
    windows: dict[str, list[tuple[float, float]]] = {}
    for row in conn.execute(
        "SELECT instance, started_at, finished_at FROM calibrations "
        "WHERE finished_at IS NOT NULL AND finished_at >= ?",
        (since,),
    ):
        windows.setdefault(row["instance"], []).append(
            (row["started_at"] - 1.0, row["finished_at"] + 1.0)
        )
    return windows


def _span_excluded(
    start: float, end: float, windows: list[tuple[float, float]]
) -> bool:
    return any(start <= w_end and end >= w_start for w_start, w_end in windows)


def _hard_ceiling(conn, instance: str) -> float | None:
    """Highest rate the latest completed spread ramp actually achieved: the
    short-burst ceiling above the sustained limit."""
    for row in conn.execute(
        "SELECT detail FROM calibrations WHERE instance = ? AND status = 'completed' "
        "ORDER BY id DESC",
        (instance,),
    ):
        detail = json.loads(row["detail"])
        if (detail.get("plan") or {}).get("mode") != "spread":
            continue
        achieved = [
            float(step["achieved_eps"])
            for step in detail.get("steps") or []
            if step.get("achieved_eps")
        ]
        return round(max(achieved), 2) if achieved else None
    return None


# -- measured peaks -------------------------------------------------------------------


def _wire_peaks(
    events_log: RawEventLog,
    instance: str,
    start: float,
    end: float,
    windows: list[tuple[float, float]],
) -> dict | None:
    """Fine-grained TX peaks from the wire stream, or None without wire
    coverage in the window (any wire event counts as coverage)."""
    covered = events_log.rate_bins(
        instance, start, end, max(end - start, 1.0), source="wire"
    )
    if not covered:
        return None
    bins = events_log.rate_bins(
        instance, start, end, 1.0, source="wire", kinds=WIRE_TX_KINDS, direction="out"
    )
    kept: list[tuple[int, int]] = []
    excluded = 0
    for index, count in bins:
        bin_start = start + index
        if _span_excluded(bin_start, bin_start + 1.0, windows):
            excluded += 1
        else:
            kept.append((index, count))
    if not kept:
        return {"peak": None, "top_bursts": [], "excluded_bins": excluded}

    # Exact sliding 1 s peaks around the busiest fixed bins.
    refined: list[tuple[float, float]] = []
    for index, count in sorted(kept, key=lambda item: item[1], reverse=True)[
        :PEAK_REFINE_BINS
    ]:
        bin_start = start + index
        times = [
            ts
            for ts in events_log.event_times(
                instance,
                bin_start - 1.0,
                bin_start + 2.0,
                source="wire",
                kinds=WIRE_TX_KINDS,
                direction="out",
            )
            if not _span_excluded(ts, ts, windows)
        ]
        peak, at = _sliding_peak(times)
        refined.append((max(peak, float(count)), at if peak else bin_start))

    top: list[dict] = []
    for peak, at in sorted(refined, reverse=True):
        if any(abs(at - entry["at"]) <= 2.0 for entry in top):
            continue
        top.append({"at": round(at, 3), "eps_1s": peak})
        if len(top) >= TOP_BURSTS:
            break

    ten_second = [
        count
        for index, count in events_log.rate_bins(
            instance,
            start,
            end,
            10.0,
            source="wire",
            kinds=WIRE_TX_KINDS,
            direction="out",
        )
        if not _span_excluded(start + index * 10.0, start + index * 10.0 + 10.0, windows)
    ]
    peak_entry = {
        "eps_1s": top[0]["eps_1s"],
        "at": top[0]["at"],
        "eps_10s": round(max(ten_second) / 10.0, 2) if ten_second else 0.0,
    }
    return {"peak": peak_entry, "top_bursts": top, "excluded_bins": excluded}


def _command_peaks(times: list[float]) -> dict | None:
    """Fallback peaks from T0 command chains when no tap covers the instance."""
    if not times:
        return None
    peak, at = _sliding_peak(times)
    top: list[dict] = [{"at": round(at, 3), "eps_1s": peak}]
    ten_bins: dict[int, int] = {}
    for ts in times:
        key = int(ts // 10)
        ten_bins[key] = ten_bins.get(key, 0) + 1
    return {
        "peak": {
            "eps_1s": peak,
            "at": round(at, 3),
            "eps_10s": round(max(ten_bins.values()) / 10.0, 2),
        },
        "top_bursts": top,
        "excluded_bins": 0,
    }


# -- commander bursts and composition ---------------------------------------------------


def _commander_bursts(conn, instance: str, start: float) -> dict[str, list[dict]]:
    """Bursts per commander from recorded chains: runs of commands with
    gaps under BURST_GAP_SECONDS, at least MIN_BURST_COMMANDS long."""
    per_commander: dict[str, list[float]] = {}
    for row in conn.execute(
        "SELECT opened_at, client FROM chains "
        "WHERE instance = ? AND opened_at >= ? ORDER BY opened_at",
        (instance, start),
    ):
        per_commander.setdefault(row["client"] or UNATTRIBUTED, []).append(
            row["opened_at"]
        )
    out: dict[str, list[dict]] = {}
    for commander, times in per_commander.items():
        bursts: list[dict] = []
        cluster: list[float] = []
        for ts in [*times, None]:
            if cluster and (ts is None or ts - cluster[-1] > BURST_GAP_SECONDS):
                if len(cluster) >= MIN_BURST_COMMANDS:
                    peak, at = _sliding_peak(cluster)
                    bursts.append(
                        {
                            "start": cluster[0],
                            "end": cluster[-1],
                            "commands": len(cluster),
                            "duration_s": max(cluster[-1] - cluster[0], 0.001),
                            "peak_eps": peak,
                            "peak_at": at,
                        }
                    )
                cluster = []
            if ts is not None:
                cluster.append(ts)
        if bursts:
            out[commander] = bursts
    return out


def _worst_peaks(bursts_by_commander: dict[str, list[dict]]) -> dict[str, float]:
    return {
        commander: max(burst["peak_eps"] for burst in bursts)
        for commander, bursts in bursts_by_commander.items()
    }


def _composed_worst(bursts_by_commander: dict[str, list[dict]]) -> dict | None:
    """Worst case over commander sets observed bursting concurrently: each
    member priced at its own worst burst anywhere in the window. Membership
    is observed, never hypothesized; the pricing is the composition."""
    worst = _worst_peaks(bursts_by_commander)
    intervals = sorted(
        (
            burst["start"] - COFIRE_PAD_SECONDS,
            burst["end"] + COFIRE_PAD_SECONDS,
            commander,
        )
        for commander, bursts in bursts_by_commander.items()
        for burst in bursts
    )
    best: dict | None = None
    active: list[tuple[float, str]] = []
    for start, end, commander in intervals:
        active = [(e, c) for e, c in active if e >= start]
        members = {c for _, c in active} | {commander}
        if len(members) >= 2:
            eps = sum(worst[c] for c in members)
            if best is None or eps > best["eps"]:
                best = {
                    "eps": round(eps, 1),
                    "commanders": sorted(members),
                    "observed_at": round(start + COFIRE_PAD_SECONDS, 3),
                }
        active.append((end, commander))
    return best


def _fanouts(bursts_by_instance: dict[str, dict[str, list[dict]]]) -> list[dict]:
    """Commanders observed bursting on several coordinators at once. The
    combined rate (each instance at that commander's worst burst there) is
    the load a consolidation of those meshes must absorb in one place."""
    spans: dict[str, list[tuple[float, float, str]]] = {}
    worst: dict[tuple[str, str], float] = {}
    for instance, by_commander in bursts_by_instance.items():
        for commander, bursts in by_commander.items():
            worst[(commander, instance)] = max(b["peak_eps"] for b in bursts)
            for burst in bursts:
                spans.setdefault(commander, []).append(
                    (
                        burst["start"] - COFIRE_PAD_SECONDS,
                        burst["end"] + COFIRE_PAD_SECONDS,
                        instance,
                    )
                )
    fanouts: list[dict] = []
    for commander, intervals in spans.items():
        if len({instance for _, _, instance in intervals}) < 2:
            continue
        intervals.sort()
        best: dict | None = None
        active: list[tuple[float, str]] = []
        for start, end, instance in intervals:
            active = [(e, i) for e, i in active if e >= start]
            members = {i for _, i in active} | {instance}
            if len(members) >= 2:
                eps = sum(worst[(commander, i)] for i in members)
                if best is None or eps > best["eps"]:
                    best = {
                        "eps": eps,
                        "members": sorted(members),
                        "observed_at": round(start + COFIRE_PAD_SECONDS, 3),
                    }
            active.append((end, instance))
        if best is not None:
            fanouts.append(
                {
                    "commander": commander,
                    "combined_eps": round(best["eps"], 1),
                    "observed_at": best["observed_at"],
                    "instances": {
                        instance: worst[(commander, instance)]
                        for instance in best["members"]
                    },
                }
            )
    fanouts.sort(key=lambda entry: entry["combined_eps"], reverse=True)
    return fanouts


# -- the summary ------------------------------------------------------------------------


def summarize(
    events_log: RawEventLog,
    db: Database,
    seconds: int,
    instances_info: list[dict],
    clock: Callable[[], float] = time.time,
) -> dict:
    now = clock()
    start = now - seconds
    conn = db.connect()
    windows = _benchmark_windows(conn, start)
    knees = headroom.latest_knees(db)
    bases = sorted({info["base_topic"] for info in instances_info} | set(knees))

    bursts_by_instance: dict[str, dict[str, list[dict]]] = {}
    instances: dict[str, dict] = {}
    for base in bases:
        by_commander = _commander_bursts(conn, base, start)
        bursts_by_instance[base] = by_commander

        peaks = _wire_peaks(events_log, base, start, now, windows.get(base, []))
        if peaks is not None:
            coverage, provenance = "wire", WIRE_PROVENANCE
        else:
            all_times = sorted(
                row["opened_at"]
                for row in conn.execute(
                    "SELECT opened_at FROM chains "
                    "WHERE instance = ? AND opened_at >= ? ORDER BY opened_at",
                    (base, start),
                )
            )
            peaks = _command_peaks(all_times)
            coverage = "commands" if peaks is not None else "none"
            provenance = COMMANDS_PROVENANCE if peaks is not None else None
        if peaks is None:
            peaks = {"peak": None, "top_bursts": [], "excluded_bins": 0}

        modes = knees.get(base, {})
        sustained = modes.get("spread") or modes.get("single")
        ceiling = _hard_ceiling(conn, base)
        limits = None
        if sustained is not None or ceiling is not None:
            limits = {}
            if sustained is not None:
                limits.update(
                    {
                        "sustained_eps": sustained["eps"],
                        "sustained_kind": sustained["kind"],
                        "mode": sustained["mode"],
                        "measured_at": sustained["measured_at"],
                    }
                )
            if ceiling is not None:
                limits["ceiling_eps"] = ceiling

        utilization = None
        if peaks["peak"] is not None and sustained is not None and sustained["eps"]:
            utilization = round(
                peaks["peak"]["eps_1s"] / sustained["eps"] * 100.0, 1
            )

        worst = _worst_peaks(by_commander)
        commanders = [
            {
                "commander": commander,
                "bursts": len(by_commander[commander]),
                "worst": max(
                    by_commander[commander], key=lambda burst: burst["peak_eps"]
                ),
            }
            for commander in sorted(worst, key=worst.get, reverse=True)[
                :TOP_COMMANDERS
            ]
        ]
        for entry in commanders:
            entry["worst"] = {
                "at": round(entry["worst"]["peak_at"], 3),
                "commands": entry["worst"]["commands"],
                "duration_s": round(entry["worst"]["duration_s"], 2),
                "peak_eps": entry["worst"]["peak_eps"],
            }

        instances[base] = {
            "coverage": coverage,
            "provenance": provenance,
            "peak": peaks["peak"],
            "top_bursts": peaks["top_bursts"],
            "benchmark_windows_excluded": peaks["excluded_bins"],
            "limits": limits,
            "burst_utilization_pct": utilization,
            "commanders": commanders,
            "composed_worst": _composed_worst(by_commander),
        }

    return {
        "window_seconds": seconds,
        "instances": instances,
        "fanouts": _fanouts(bursts_by_instance),
    }
