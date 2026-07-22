"""Applied-recommendation verification (V2_PROPOSAL.md §V2-6).

Two jobs, both riding the hourly recommendation pass:

1. **Applied auto-detection.** The change journal proves cross-instance
   moves (a ``device_added`` entry annotated ``moved_from``), so an open
   rebalancing finding whose staged moves have all journaled is marked
   applied automatically, with the boundary set to when the registry saw
   the change. Every other action in the shipped roster changes controller
   behavior the registries cannot see (pacing, dedupe, retargeting), so
   those stay manual, per the §V2-10.4 hybrid.

2. **Verdicts.** Each applied row gets before/after windows around its
   boundary and a verdict when the after-window holds enough data:
   improved (state ``verified``), regressed (state ``regressed``), or no
   material change (stays ``applied`` with the receipts attached; the
   check stops at a horizon). Spend detectors verify in the ledger's own
   daily currency over completed UTC days, which compares like hours with
   like by construction; burst detectors verify recomposed command peaks
   against the numbers recorded in the finding's evidence. Verdicts are
   measured deltas with stated windows: no significance theater.
"""

from __future__ import annotations

import json

from ..capacity import ledger, scenario
from .context import DetectorContext
from .store import RecommendationStore

NAME = "verification"

# Spend verdict thresholds: the after-mean against the before-mean.
IMPROVED_RATIO = 0.8
REGRESSED_RATIO = 1.25
# Completed UTC days required on each side of the boundary for a spend
# verdict; the before window reaches back at most BASELINE_DAYS.
MIN_SPEND_DAYS = 2
BASELINE_DAYS = 7
# Burst verdicts need this much recorded time after the boundary.
MIN_PEAK_SECONDS = 24 * 3600.0
PEAK_WINDOW_SECONDS = 24 * 3600.0
# A burst peak within this factor of its recorded before-peak shows no
# relief; verdicts between the bands stay pending until the horizon.
NO_RELIEF_RATIO = 0.95
HORIZON_SECONDS = 14 * 86400.0

SPEND_NOTE = (
    "daily ledger currency over completed UTC days; whole days compare "
    "like hours with like"
)


def _utc_day(ts: float) -> str:
    from ..capacity.ledger import utc_day

    return utc_day(ts)


def _day_pricing_version(row) -> int:
    """The cost model a per-day row was priced under, or MIXED when the day
    accumulated across a model change (vmin != vmax), which makes its µs an
    incomparable quantity."""
    vmin, vmax = row["vmin"], row["vmax"]
    if vmin is None or vmax is None or vmin != vmax:
        return ledger.MIXED_PRICING_VERSION
    return int(vmin)


def _completed_days(conn, sql: str, params: tuple, boundary: float, now: float):
    """(before_days, after_days, before_versions, after_versions): mean µs/day
    from a per-day query, plus the cost-model version each day was priced
    under. Only completed UTC days count: the boundary day and today are
    partial on the wrong side and stay out."""
    boundary_day = _utc_day(boundary)
    today = _utc_day(now)
    earliest = _utc_day(boundary - BASELINE_DAYS * 86400.0)
    before: list[float] = []
    after: list[float] = []
    before_versions: set[int] = set()
    after_versions: set[int] = set()
    for row in conn.execute(sql, params):
        day = row["day"]
        if earliest <= day < boundary_day:
            before.append(row["us"])
            before_versions.add(_day_pricing_version(row))
        elif boundary_day < day < today:
            after.append(row["us"])
            after_versions.add(_day_pricing_version(row))
    return before, after, before_versions, after_versions


def _spend_metric(ctx: DetectorContext, rec: dict, boundary: float):
    """Per-day µs series for the recommendation's subject in the ledger."""
    detector = rec["detector"]
    if detector == "reporting":
        sql = (
            "SELECT day, SUM(autonomous_us) AS us, MIN(pricing_version) AS vmin, "
            "MAX(pricing_version) AS vmax FROM ledger_device_daily "
            "WHERE instance = ? AND device = ? GROUP BY day"
        )
        params = (rec["instance"], rec["subject"])
        label = f"{rec['subject']}'s reporting spend"
    elif detector == "redundancy":
        sql = (
            "SELECT day, SUM(tx_us + rx_us) AS us, MIN(pricing_version) AS vmin, "
            "MAX(pricing_version) AS vmax FROM ledger_daily "
            "WHERE instance = ? AND commander = ? GROUP BY day"
        )
        params = (rec["instance"], rec["subject"])
        label = f"{rec['subject']}'s command spend"
    elif detector == "groupcast_economics":
        sql = (
            "SELECT day, SUM(tx_us + rx_us) AS us, MIN(pricing_version) AS vmin, "
            "MAX(pricing_version) AS vmax FROM ledger_daily "
            "WHERE instance = ? GROUP BY day"
        )
        params = (rec["instance"],)
        label = f"{rec['instance']}'s commanded spend"
    else:
        return None
    before, after, before_versions, after_versions = _completed_days(
        ctx.conn, sql, params, boundary, ctx.now
    )
    return before, after, label, before_versions, after_versions


def _evidence(rec: dict, kind: str) -> dict | None:
    return next((e for e in rec["evidence"] if e.get("kind") == kind), None)


def _burst_after_peak(ctx: DetectorContext, instance: str) -> float | None:
    """Recomposed-currency command peak over the trailing day, the same
    arithmetic the advisor's pressure scan uses."""
    if ctx.events_log is None:
        return None
    start = ctx.now - PEAK_WINDOW_SECONDS
    windows = scenario._benchmark_windows(ctx.conn, start)
    bins = scenario._t0_bins(
        ctx.events_log, instance, start, ctx.now, 1.0, windows.get(instance, [])
    )
    peak = scenario._refined_peak(
        ctx.events_log, start, windows, bins, instance, (), []
    )
    return peak["eps_1s"] if peak else 0.0


def _pacing_after_peak(ctx: DetectorContext, rec: dict, boundary: float) -> float | None:
    """Worst 1 s command peak of the commander's chains since the boundary."""
    times = [
        row["opened_at"]
        for row in ctx.conn.execute(
            "SELECT opened_at FROM chains WHERE instance = ? AND client = ? "
            "AND opened_at >= ? ORDER BY opened_at",
            (rec["instance"], rec["subject"], boundary),
        )
    ]
    if not times:
        return 0.0
    peak, _at = scenario._sliding_peak(times)
    return peak


def _verdict_from_ratio(ratio: float) -> str:
    if ratio <= IMPROVED_RATIO:
        return "improved"
    if ratio >= REGRESSED_RATIO:
        return "regressed"
    return "no_material_change"


def _apply_verdict(
    store: RecommendationStore, rec: dict, verdict: str, receipts: dict, now: float, boundary: float
) -> str:
    """Terminal verdicts transition the row; a pending no-change keeps the
    receipts attached and finalizes at the horizon."""
    if verdict == "improved":
        store.set_verdict(
            rec["id"],
            "verified",
            "verified: the measured after-window improved as predicted",
            receipts,
        )
        return "verified"
    if verdict == "regressed":
        store.set_verdict(
            rec["id"],
            "regressed",
            "regressed: the measured after-window got worse; see the receipts",
            receipts,
        )
        return "regressed"
    if now - boundary >= HORIZON_SECONDS:
        receipts = {**receipts, "finalized": True}
        store.record_verification(rec["id"], receipts)
        return "no_material_change_final"
    store.record_verification(rec["id"], receipts)
    return "pending"


def _verify_spend(
    store: RecommendationStore, ctx: DetectorContext, rec: dict, boundary: float
) -> str:
    metric = _spend_metric(ctx, rec, boundary)
    if metric is None:
        return "unsupported"
    before, after, label, before_versions, after_versions = metric
    # A spend verdict is a comparison of two µs quantities, so it is only
    # meaningful if both sides were priced by the same cost model. When a
    # model change sits between them the ratio measures the re-pricing, not
    # the user's change, and grading it would write a durable verdict off an
    # artefact: REGRESSED_RATIO is 1.25 and a hop-pricing bump moves unicast
    # spend by more than that on its own. Hold at pending instead; the
    # comparison becomes possible again once both sides sit on the new model.
    versions = before_versions | after_versions
    if versions and (len(versions) > 1 or ledger.MIXED_PRICING_VERSION in versions):
        receipts = {
            "verdict": "pending",
            "metric": label,
            "unit": "us_per_day",
            "before_days": len(before),
            "after_days": len(after),
            "note": (
                "the cost model changed inside this comparison window, so "
                "before and after are not the same currency; verification "
                "resumes once both sides are priced the same way"
            ),
            "pricing_versions": sorted(versions),
            "basis": SPEND_NOTE,
            "checked_at": ctx.now,
        }
        store.record_verification(rec["id"], receipts)
        return "pending"
    if len(before) < MIN_SPEND_DAYS or len(after) < MIN_SPEND_DAYS:
        receipts = {
            "verdict": "pending",
            "metric": label,
            "unit": "us_per_day",
            "before_days": len(before),
            "after_days": len(after),
            "needs_days": MIN_SPEND_DAYS,
            "basis": SPEND_NOTE,
            "checked_at": ctx.now,
        }
        store.record_verification(rec["id"], receipts)
        return "pending"
    before_mean = sum(before) / len(before)
    after_mean = sum(after) / len(after)
    ratio = after_mean / before_mean if before_mean > 0 else (0.0 if after_mean == 0 else 99.0)
    verdict = _verdict_from_ratio(ratio)
    receipts = {
        "verdict": verdict,
        "metric": label,
        "unit": "us_per_day",
        "before_us_per_day": round(before_mean, 1),
        "after_us_per_day": round(after_mean, 1),
        "before_days": len(before),
        "after_days": len(after),
        "ratio": round(ratio, 3),
        "basis": SPEND_NOTE,
        "checked_at": ctx.now,
    }
    return _apply_verdict(store, rec, verdict, receipts, ctx.now, boundary)


def _verify_burst(
    store: RecommendationStore, ctx: DetectorContext, rec: dict, boundary: float
) -> str:
    if ctx.now - boundary < MIN_PEAK_SECONDS:
        store.record_verification(
            rec["id"],
            {
                "verdict": "pending",
                "metric": "recorded command peak",
                "note": "waiting for a full day of recorded traffic after the change",
                "checked_at": ctx.now,
            },
        )
        return "pending"

    if rec["detector"] == "rebalancing":
        pressure = _evidence(rec, "burst_pressure")
        limit = _evidence(rec, "capacity_limit")
        if not pressure or not limit:
            return "unsupported"
        before_peak = float(pressure["peak_eps"])
        sustained = float(limit["eps"])
        after_peak = _burst_after_peak(ctx, rec["instance"])
        if after_peak is None:
            return "unsupported"
        if after_peak < sustained:
            verdict = "improved"
        elif after_peak >= before_peak * NO_RELIEF_RATIO:
            verdict = "regressed"
        else:
            verdict = "no_material_change"
        receipts = {
            "verdict": verdict,
            "metric": f"{rec['instance']}'s recorded 1 s command peak",
            "unit": "eps",
            "before_peak_eps": before_peak,
            "after_peak_eps": round(after_peak, 1),
            "sustained_limit_eps": sustained,
            "basis": (
                "recomposed T0 command peak over the trailing day vs the "
                "finding's recorded peak and measured limit"
            ),
            "checked_at": ctx.now,
        }
        return _apply_verdict(store, rec, verdict, receipts, ctx.now, boundary)

    if rec["detector"] == "pacing":
        target_eps = (rec["action"] or {}).get("target_eps")
        windows = [e for e in rec["evidence"] if e.get("kind") == "window"]
        if target_eps is None or not windows:
            return "unsupported"
        before_peak = max(float(w["peak_eps"]) for w in windows)
        after_peak = _pacing_after_peak(ctx, rec, boundary)
        if after_peak is None:
            return "unsupported"
        if after_peak <= float(target_eps) * 1.1:
            verdict = "improved"
        elif after_peak >= before_peak * NO_RELIEF_RATIO:
            verdict = "regressed"
        else:
            verdict = "no_material_change"
        receipts = {
            "verdict": verdict,
            "metric": f"{rec['subject']}'s worst 1 s burst",
            "unit": "eps",
            "before_peak_eps": before_peak,
            "after_peak_eps": round(after_peak, 1),
            "paced_target_eps": target_eps,
            "basis": "worst recorded burst since the change vs the finding's recorded bursts",
            "checked_at": ctx.now,
        }
        return _apply_verdict(store, rec, verdict, receipts, ctx.now, boundary)

    return "unsupported"


def _detect_applied_moves(store: RecommendationStore, ctx: DetectorContext) -> int:
    """Open rebalancing findings whose staged moves have all journaled as
    cross-instance arrivals are applied in the real world; mark them so
    with the journal's own timestamp as the boundary."""
    marked = 0
    for rec in store.open_rows("rebalancing"):
        moves = (rec["action"] or {}).get("moves") or []
        if not moves:
            continue
        boundaries: list[float] = []
        all_matched = True
        for move in moves:
            if move.get("kind") == "group":
                subjects = move.get("members") or []
                if not subjects:
                    all_matched = False
                    break
            else:
                subjects = [move["subject"]]
            for subject in subjects:
                row = ctx.conn.execute(
                    "SELECT ts, detail FROM journal WHERE kind = 'device_added' "
                    "AND instance = ? AND subject = ? AND ts >= ? "
                    "ORDER BY ts DESC LIMIT 1",
                    (move["to_instance"], subject, rec["created_at"]),
                ).fetchone()
                detail = json.loads(row["detail"]) if row else {}
                if row is None or detail.get("moved_from") != move["from_instance"]:
                    all_matched = False
                    break
                boundaries.append(row["ts"])
            if not all_matched:
                break
        if all_matched and boundaries:
            store.mark_applied_auto(
                rec["id"],
                max(boundaries),
                "applied: the change journal saw every staged device arrive "
                "on its destination coordinator",
            )
            marked += 1
    return marked


def run(store: RecommendationStore, ctx: DetectorContext) -> dict:
    """One verification pass: auto-detect applied moves, then drive every
    applied row toward a verdict."""
    auto_applied = _detect_applied_moves(store, ctx)
    outcomes: dict[str, int] = {}
    for rec in store.applied_rows():
        boundary = rec["state_changed_at"] or rec["updated_at"]
        existing = rec.get("verification") or {}
        if existing.get("finalized"):
            outcome = "finalized"
        elif rec["detector"] in ("rebalancing", "pacing"):
            outcome = _verify_burst(store, ctx, rec, boundary)
        else:
            outcome = _verify_spend(store, ctx, rec, boundary)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {"auto_applied": auto_applied, **outcomes}
