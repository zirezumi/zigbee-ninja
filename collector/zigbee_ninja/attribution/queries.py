"""Read-side queries for the attribution explorer (DESIGN.md paragraph 13, view 3)."""

from __future__ import annotations

import time

from ..store.db import Database
from .chains import parse_key_digests

TOP_LIMIT = 15


def summary(db: Database, seconds: int) -> dict:
    conn = db.connect()
    since = time.time() - seconds
    since_bucket = int(since)

    classes: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT instance, klass, SUM(count) AS total FROM attribution_10s "
        "WHERE ts >= ? GROUP BY instance, klass",
        (since_bucket,),
    ):
        classes.setdefault(row["instance"], {})[row["klass"]] = row["total"]

    targets = [
        {
            "instance": row["instance"],
            "target": row["target"],
            "commands": row["commands"],
            "redundant": row["redundant"],
            "avg_first_echo_ms": row["avg_echo"],
        }
        for row in conn.execute(
            "SELECT instance, target, COUNT(*) AS commands, "
            "SUM(redundant) AS redundant, AVG(first_echo_ms) AS avg_echo "
            "FROM chains WHERE opened_at >= ? "
            "GROUP BY instance, target ORDER BY commands DESC LIMIT ?",
            (since, TOP_LIMIT),
        )
    ]

    clients = [
        {"client": row["client"] or "(unattributed)", "commands": row["commands"]}
        for row in conn.execute(
            "SELECT client, COUNT(*) AS commands FROM chains WHERE opened_at >= ? "
            "GROUP BY client ORDER BY commands DESC LIMIT ?",
            (since, TOP_LIMIT),
        )
    ]

    totals = conn.execute(
        "SELECT COUNT(*) AS chains, SUM(redundant) AS redundant, "
        "AVG(first_echo_ms) AS avg_echo FROM chains WHERE opened_at >= ?",
        (since,),
    ).fetchone()

    return {
        "window_seconds": seconds,
        "classes": classes,
        "top_targets": targets,
        "top_clients": clients,
        "totals": {
            "chains": totals["chains"] or 0,
            "redundant": totals["redundant"] or 0,
            "avg_first_echo_ms": totals["avg_echo"],
        },
    }


def redundant(db: Database, seconds: int) -> list[dict]:
    conn = db.connect()
    since = time.time() - seconds
    return [
        {
            "instance": row["instance"],
            "target": row["target"],
            "count": row["count"],
            "client": row["client"] or "(unattributed)",
        }
        for row in conn.execute(
            "SELECT instance, target, client, COUNT(*) AS count FROM chains "
            "WHERE redundant = 1 AND opened_at >= ? "
            "GROUP BY instance, target, client ORDER BY count DESC LIMIT ?",
            (since, TOP_LIMIT),
        )
    ]


def conflicts(db: Database, seconds: int, window: float = 2.0) -> dict:
    """Commands that DISAGREE about a parameter, not merely repeat one.

    A duplicate wastes airtime. A conflict decides a device's state by arrival
    order instead of by intent, so it is the failure a redundancy report cannot
    see: both look like "different payload to the same target" once the payload
    is reduced to a single digest. Comparing per-key digests separates them.

    A pair counts as conflicting when, inside `window` seconds on one target,
    two commands set the SAME key to DIFFERENT values. Two commands touching
    disjoint keys are a normal multi-key sequence and are ignored, which is what
    makes this different from the whole-payload view: one writer setting
    brightness then colour then a config byte registers three "different
    payloads" and zero conflicts.

    `same_commander` conflicts are a read-then-write race inside one automation;
    `cross_commander` ones are two owners fighting over a parameter. Both are
    reported, split, because they call for different fixes.

    Skew-aware, for the same reason the redundancy test is: chain timestamps are
    processing time, so a stall can pull two unrelated commands into apparent
    proximity. A pair only counts when the gap plus the doubt still fits the
    window.
    """
    conn = db.connect()
    since = time.time() - seconds
    rows = conn.execute(
        "SELECT instance, target, client, opened_at, clock_skew_ms, payload_keys "
        "FROM chains WHERE opened_at >= ? AND verb = 'set' AND payload_keys IS NOT NULL "
        "ORDER BY instance, target, opened_at",
        (since,),
    ).fetchall()

    by_target: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        by_target.setdefault((row["instance"], row["target"]), []).append(dict(row))

    found: dict[tuple, dict] = {}
    pairs_examined = 0
    skew_skipped = 0
    for (instance, target), chains in by_target.items():
        for index, first in enumerate(chains):
            first_keys = parse_key_digests(first["payload_keys"])
            if not first_keys:
                continue
            for second in chains[index + 1 :]:
                gap = second["opened_at"] - first["opened_at"]
                if gap > window:
                    break
                pairs_examined += 1
                doubt = max(
                    first["clock_skew_ms"] or 0.0, second["clock_skew_ms"] or 0.0
                ) / 1000.0
                if gap + doubt > window:
                    skew_skipped += 1
                    continue
                second_keys = parse_key_digests(second["payload_keys"])
                disputed = sorted(
                    key
                    for key in first_keys.keys() & second_keys.keys()
                    if first_keys[key] != second_keys[key]
                )
                if not disputed:
                    continue
                a = first["client"] or "(unattributed)"
                b = second["client"] or "(unattributed)"
                for key in disputed:
                    entry = found.setdefault(
                        (instance, target, a, b, key),
                        {
                            "instance": instance,
                            "target": target,
                            "key": key,
                            "first_client": a,
                            "second_client": b,
                            "same_commander": a == b,
                            "count": 0,
                            "min_gap_ms": None,
                        },
                    )
                    entry["count"] += 1
                    gap_ms = round(gap * 1000.0, 1)
                    if entry["min_gap_ms"] is None or gap_ms < entry["min_gap_ms"]:
                        entry["min_gap_ms"] = gap_ms

    ranked = sorted(found.values(), key=lambda e: -e["count"])
    return {
        "window_seconds": window,
        "pairs_examined": pairs_examined,
        "pairs_skipped_for_clock_skew": skew_skipped,
        "chains_considered": len(rows),
        "conflicts": ranked[:TOP_LIMIT],
        "total_conflicting_pairs": sum(e["count"] for e in ranked),
        "cross_commander_pairs": sum(
            e["count"] for e in ranked if not e["same_commander"]
        ),
    }
