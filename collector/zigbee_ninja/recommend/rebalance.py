"""Rebalancing advisor (V2_PROPOSAL.md §V2-5 detector 5, §V2-11).

Finds coordinators whose recorded command bursts cross their measured
capacity limits and proposes small move sets that would relieve them,
judged through the shared scenario engine (capacity/scenario.py) so an
advisor proposal and a hand-built simulator scenario are priced by the
same arithmetic and can never disagree.

Candidate generation: the subjects (devices or groups) receiving the most
commands inside the source's worst recorded seconds are the load the peak
is made of; moving the heaviest of them to the coordinator with the most
burst headroom splits coincident load across meshes. A proposal is emitted
only when the scenario engine's recomposed after-peaks clear the source
without pushing the destination past its own limits: a burst that merely
relocates whole is not a finding.

Confidence is medium at best by construction: radio reach is unknowable
from recorded data (§V2-11 item 7), and the after-peaks are modeled
recompositions of the T0 stream. Stale calibration environments or rarely
recurring pressure drop it to low.
"""

from __future__ import annotations

from collections import Counter

from ..capacity import scenario
from ..capacity.airtime import CHANNEL_BUDGET_US_PER_S
from .context import DetectorContext
from .store import Finding

NAME = "rebalancing"

# Verdicts a mesh may sit at after the proposed moves; anything hotter
# rejects the proposal (the point is relief, not relocation of the problem).
# An idle mesh reads no_traffic and is the calmest state of all.
ACCEPTABLE_VERDICTS = ("ok", "near_sustained", "no_traffic")
PRESSURED_VERDICTS = ("above_sustained", "above_ceiling")
# The busiest fixed 1 s bins supply the move candidates; commands inside
# them are what the peak is made of.
CANDIDATE_BINS = 5
MAX_CANDIDATE_SUBJECTS = 5
MAX_MOVES_PER_PROPOSAL = 3
MAX_DESTINATIONS_TRIED = 2
# Pressure seen in fewer distinct seconds than this reads as a rare event.
MIN_PRESSURE_SECONDS = 3
SAVING_BASIS = (
    "recorded traffic recomposed by the scenario engine; the headline is "
    "any steady airtime freed, the burst relief is the finding"
)


def _pressure_scan(ctx: DetectorContext) -> dict[str, dict]:
    """Per instance: the recomposed-currency command peak, its limits, and
    the verdict, using the scenario engine's own arithmetic."""
    start = ctx.window_start()
    windows = scenario._benchmark_windows(ctx.conn, start)
    out: dict[str, dict] = {}
    for base in ctx.instances:
        limits = scenario._limits(ctx.db, ctx.registry, base)
        bins = scenario._t0_bins(
            ctx.events_log, base, start, ctx.now, 1.0, windows.get(base, [])
        )
        peak = scenario._refined_peak(
            ctx.events_log, start, windows, bins, base, (), []
        )
        verdict = scenario._judge(peak["eps_1s"] if peak else None, limits)
        pressure_seconds = 0
        if limits and limits.get("sustained_eps"):
            pressure_seconds = sum(
                1 for count in bins.values() if count >= limits["sustained_eps"]
            )
        out[base] = {
            "limits": limits,
            "bins": bins,
            "peak": peak,
            "verdict": verdict,
            "pressure_seconds": pressure_seconds,
            "windows": windows,
        }
    return out


def _candidate_subjects(ctx: DetectorContext, base: str, bins: Counter) -> list[dict]:
    """Subjects (devices or groups) ranked by how many commands they receive
    inside the busiest recorded seconds: the load the peak is made of."""
    start = ctx.window_start()
    device_names = {
        d["friendly_name"] for d in ctx.devices(base) if d.get("friendly_name")
    }
    group_names = {
        g["friendly_name"] for g in ctx.groups(base) if g.get("friendly_name")
    }
    counts: Counter = Counter()
    for index, _ in bins.most_common(CANDIDATE_BINS):
        lo = start + index - 0.5
        hi = start + index + 1.5
        for row in ctx.conn.execute(
            "SELECT target, COUNT(*) AS n FROM chains "
            "WHERE instance = ? AND opened_at >= ? AND opened_at < ? "
            "GROUP BY target",
            (base, lo, hi),
        ):
            counts[row["target"]] += row["n"]
    candidates = []
    for target, count in counts.most_common():
        if target in group_names:
            candidates.append({"kind": "group", "subject": target, "commands": count})
        elif target in device_names:
            candidates.append({"kind": "device", "subject": target, "commands": count})
        if len(candidates) >= MAX_CANDIDATE_SUBJECTS:
            break
    return candidates


def _destinations(scan: dict[str, dict], source: str) -> list[str]:
    """Instances with measured limits and burst headroom, most headroom
    first: where relocated load has room to land."""
    ranked = []
    for base, entry in scan.items():
        if base == source or entry["verdict"] not in ACCEPTABLE_VERDICTS:
            continue
        limits = entry["limits"]
        if not limits or not limits.get("sustained_eps"):
            continue
        peak_eps = entry["peak"]["eps_1s"] if entry["peak"] else 0.0
        ranked.append((peak_eps / limits["sustained_eps"], base))
    ranked.sort()
    return [base for _, base in ranked[:MAX_DESTINATIONS_TRIED]]


def _price(ctx: DetectorContext, moves: list[dict]) -> dict:
    return scenario.price_scenario(
        ctx.events_log,
        ctx.db,
        ctx.registry,
        ctx.pricing,
        ctx.topology_latest or (lambda base: {}),
        moves,
        window_seconds=int(ctx.lookback_seconds),
        clock=lambda: ctx.now,
    )


def _proposal(
    ctx: DetectorContext, source: str, scan: dict[str, dict]
) -> tuple[list[dict], dict] | None:
    """Smallest move set whose recomposed after-peaks clear the source and
    keep every destination acceptable, or None when no candidate does."""
    candidates = _candidate_subjects(ctx, source, scan[source]["bins"])
    if not candidates:
        return None
    for dest in _destinations(scan, source):
        moves: list[dict] = []
        for candidate in candidates[:MAX_MOVES_PER_PROPOSAL]:
            moves.append(
                {
                    "kind": candidate["kind"],
                    "subject": candidate["subject"],
                    "from_instance": source,
                    "to_instance": dest,
                }
            )
            try:
                report = _price(ctx, moves)
            except scenario.ScenarioError:
                moves.pop()
                continue
            source_verdict = report["instances"][source]["burst"]["verdict"]
            dest_verdict = report["instances"][dest]["burst"]["verdict"]
            if (
                source_verdict in ACCEPTABLE_VERDICTS
                and dest_verdict in ACCEPTABLE_VERDICTS
            ):
                return moves, report
    return None


def _describe_move(move: dict, report: dict) -> str:
    if move["kind"] == "group":
        entry = next(
            (
                m
                for m in report["moves"]
                if m["kind"] == "group" and m["subject"] == move["subject"]
            ),
            None,
        )
        members = len(entry["members"]) if entry else 0
        noun = "1 device" if members == 1 else f"{members} devices"
        return f"the group {move['subject']} ({noun})"
    return move["subject"]


def _finding(
    ctx: DetectorContext,
    source: str,
    scan_entry: dict,
    moves: list[dict],
    report: dict,
) -> Finding:
    dest = moves[0]["to_instance"]
    limits = scan_entry["limits"]
    peak = scan_entry["peak"]
    source_after = report["instances"][source]
    dest_after = report["instances"][dest]
    after_peak = source_after["burst"]["after_peak_1s"]
    dest_peak = dest_after["burst"]["after_peak_1s"]
    fleet_delta = sum(
        entry["steady"]["after_us_per_s"] - entry["steady"]["before_us_per_s"]
        for entry in report["instances"].values()
    )
    saving_us = max(0.0, round(-fleet_delta, 3))

    subjects = " and ".join(_describe_move(move, report) for move in moves)
    sentences = [
        f"Recorded commands on {source} peak at {peak['eps_1s']:.0f}/s inside a "
        f"single second; the measured sustained limit is "
        f"{limits['sustained_eps']:.1f}/s.",
        f"Moving {subjects} to {dest} would bring the recomposed peak to "
        f"{after_peak['eps_1s'] if after_peak else 0:.0f}/s and leave {dest} at "
        f"{dest_peak['eps_1s'] if dest_peak else 0:.0f}/s against its "
        f"{dest_after['limits']['sustained_eps']:.1f}/s limit.",
    ]
    if fleet_delta > 0.5:
        sentences.append(
            f"The moves add about {fleet_delta:.0f} µs/s of steady airtime across "
            "the fleet (router census and amplification shifts); the gain is "
            "burst relief, not airtime."
        )
    elif saving_us > 0.5:
        sentences.append(
            f"The moves also free about {saving_us:.0f} µs/s of steady airtime "
            "across the fleet."
        )
    if scan_entry["pressure_seconds"] < MIN_PRESSURE_SECONDS:
        sentences.append(
            "This pressure was recorded in fewer than "
            f"{MIN_PRESSURE_SECONDS} distinct seconds of the last 24 h; it may "
            "be a rare event."
        )
    stale = bool(limits.get("stale_environment")) or bool(
        (dest_after["limits"] or {}).get("stale_environment")
    )
    if stale:
        sentences.append(
            "A capacity limit involved was calibrated under a different "
            "Zigbee2MQTT or firmware version; consider recalibrating."
        )
    sentences.append(
        "Whether the moved devices can reach the destination coordinator by "
        "radio is unknown from recorded data."
    )

    confidence = "medium"
    if stale or scan_entry["pressure_seconds"] < MIN_PRESSURE_SECONDS:
        confidence = "low"

    evidence: list[dict] = [
        {
            "kind": "burst_pressure",
            "instance": source,
            "peak_eps": peak["eps_1s"],
            "peak_at": peak["at"],
            "verdict": scan_entry["verdict"],
            "pressured_seconds": scan_entry["pressure_seconds"],
        },
        {
            "kind": "capacity_limit",
            "instance": source,
            "mode": limits.get("mode"),
            "eps": limits["sustained_eps"],
            "ceiling_eps": limits.get("ceiling_eps"),
            "measured_at": limits.get("measured_at"),
            "stale_environment": bool(limits.get("stale_environment")),
        },
        {
            "kind": "scenario",
            "moves": [
                {
                    "kind": move["kind"],
                    "subject": move["subject"],
                    "from_instance": move["from_instance"],
                    "to_instance": move["to_instance"],
                }
                for move in moves
            ],
            "source_peak_after_eps": after_peak["eps_1s"] if after_peak else 0.0,
            "source_verdict_after": source_after["burst"]["verdict"],
            "destination": dest,
            "dest_peak_after_eps": dest_peak["eps_1s"] if dest_peak else 0.0,
            "dest_verdict_after": dest_after["burst"]["verdict"],
            "fleet_steady_delta_us_per_s": round(fleet_delta, 3),
            "second_order_us_per_s": {
                base: entry["steady"].get("second_order_us_per_s")
                for base, entry in report["instances"].items()
                if entry["steady"].get("second_order_us_per_s") is not None
            },
        },
    ]
    for move_entry in report["moves"]:
        radio = move_entry.get("radio")
        if radio is not None:
            evidence.append(
                {
                    "kind": "radio",
                    "subject": move_entry["subject"],
                    "best_observed_link_lqi": radio.get("best_observed_link_lqi"),
                    "destination_channel": radio.get("destination_channel"),
                    "status": radio.get("status"),
                }
            )

    return Finding(
        detector=NAME,
        instance=source,
        subject=source,
        finding=" ".join(sentences),
        action={
            "kind": "rebalance",
            "moves": moves,
            "window_seconds": int(ctx.lookback_seconds),
        },
        saving={
            "us_per_s": saving_us,
            "pct_of_budget": round(saving_us / CHANNEL_BUDGET_US_PER_S * 100.0, 4),
            "basis": SAVING_BASIS,
            "provenance": "modeled",
        },
        confidence=confidence,
        evidence=evidence,
        fingerprint={
            "peak_eps": round(peak["eps_1s"], 1),
            "sustained_eps": limits["sustained_eps"],
            "after_eps": round(after_peak["eps_1s"], 1) if after_peak else 0.0,
            "moves": ",".join(f"{m['kind']}:{m['subject']}" for m in moves),
            "destination": dest,
            "pressured_seconds": scan_entry["pressure_seconds"],
        },
    )


def detect(ctx: DetectorContext) -> list[Finding]:
    if ctx.events_log is None or ctx.db is None or ctx.registry is None:
        return []
    scan = _pressure_scan(ctx)
    findings: list[Finding] = []
    for source in sorted(scan):
        entry = scan[source]
        if entry["verdict"] not in PRESSURED_VERDICTS or entry["peak"] is None:
            continue
        proposal = _proposal(ctx, source, scan)
        if proposal is None:
            continue
        moves, report = proposal
        findings.append(_finding(ctx, source, entry, moves, report))
    return findings
