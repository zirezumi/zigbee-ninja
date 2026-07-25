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
    """Commands that set the SAME key to DIFFERENT values inside one window.

    Comparing per-key digests, rather than one whole-payload digest, is what
    makes this answerable at all: reduced to a single digest, "two owners
    disagreed about brightness" and "one owner wrote brightness, then colour,
    then a config byte" are the same observation. Disjoint-key sequences drop
    out here by construction.

    **What this cannot do, and why the split below exists.** A key that
    legitimately oscillates between a few states produces a hit on every
    transition, and telemetry cannot see the difference between a transition and
    a disagreement: both are "same key, new value, soon after the last one".
    Measured on a live fleet, the loudest hits were an LED bar alternating
    56 -> 0 -> 56 and an indicator timeout alternating `Stay Off` -> `3 Seconds`,
    both entirely intentional.

    So each pair is classified:

    * `alternating` -- the second value has already been seen on that key inside
      the query window, so the key is cycling through states it keeps returning
      to. Bracketing sequences and on/off state machines land here.
    * `novel` -- the second value has not been seen on that key in the window.
      A genuinely new decision landing on top of a fresh one, which is the shape
      worth investigating.

    `novel` is the headline. It under-reports by construction: a real
    disagreement between two values that both recur (two owners each repeatedly
    asserting their own brightness) reads as `alternating`. Which is why
    `writer_diversity` is reported alongside: the count of distinct commanders
    touching each key is intent-free evidence of divided ownership, the
    precondition for this class of bug, whether or not a collision was caught.

    `same_commander` pairs are a read-then-write race inside one automation;
    cross-commander pairs are two owners on one parameter. Different fixes, so
    they stay split.

    Skew-aware on the same terms as the redundancy test: chain timestamps are
    processing time, so a stall can pull unrelated commands into apparent
    proximity. A pair counts only when the gap plus the doubt still fits.
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

    # Value history and writer set per (instance, target, key) over the whole
    # window, which is what lets a cycling key be told from a disputed one.
    seen_values: dict[tuple[str, str, str], set[str]] = {}
    writers: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        for key, digest in parse_key_digests(row["payload_keys"]).items():
            ident = (row["instance"], row["target"], key)
            seen_values.setdefault(ident, set()).add(digest)
            writers.setdefault(ident, set()).add(row["client"] or "(unattributed)")

    found: dict[tuple, dict] = {}
    pairs_examined = 0
    skew_skipped = 0
    for (instance, target), chains in by_target.items():
        parsed = [parse_key_digests(c["payload_keys"]) for c in chains]
        # Walk forward over the SECOND element of each pair, so `history` holds
        # exactly what was written before that arrival. Advancing it on the
        # first element instead would freeze it for a whole inner loop and count
        # the same value as unseen repeatedly.
        history: dict[str, set[str]] = {}
        for j, second in enumerate(chains):
            second_keys = parsed[j]
            for i in range(j - 1, -1, -1):
                first = chains[i]
                first_keys = parsed[i]
                gap = second["opened_at"] - first["opened_at"]
                if gap > window:
                    break
                if not first_keys or not second_keys:
                    continue
                pairs_examined += 1
                doubt = max(
                    first["clock_skew_ms"] or 0.0, second["clock_skew_ms"] or 0.0
                ) / 1000.0
                if gap + doubt > window:
                    skew_skipped += 1
                    continue
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
                    # Has this key held the incoming value before now? If so the
                    # key is cycling, not being disputed.
                    kind = (
                        "alternating"
                        if second_keys[key] in history.get(key, set())
                        else "novel"
                    )
                    entry = found.setdefault(
                        (instance, target, a, b, key, kind),
                        {
                            "instance": instance,
                            "target": target,
                            "key": key,
                            "kind": kind,
                            "first_client": a,
                            "second_client": b,
                            "same_commander": a == b,
                            "count": 0,
                            "min_gap_ms": None,
                            "distinct_values_seen": len(
                                seen_values.get((instance, target, key), ())
                            ),
                            "writers": sorted(
                                writers.get((instance, target, key), ())
                            ),
                        },
                    )
                    entry["count"] += 1
                    gap_ms = round(gap * 1000.0, 1)
                    if entry["min_gap_ms"] is None or gap_ms < entry["min_gap_ms"]:
                        entry["min_gap_ms"] = gap_ms
            # Only now does this arrival become part of the past.
            for key, digest in second_keys.items():
                history.setdefault(key, set()).add(digest)

    ranked = sorted(found.values(), key=lambda e: -e["count"])
    novel = [e for e in ranked if e["kind"] == "novel"]

    # Divided ownership: how many distinct commanders touch each key. Intent
    # free, so it stands up where the pair classification cannot: it measures
    # the precondition for a race rather than a caught instance of one.
    diversity = [
        {
            "instance": inst,
            "target": tgt,
            "key": key,
            "writers": sorted(names),
            "writer_count": len(names),
        }
        for (inst, tgt, key), names in writers.items()
        if len(names) > 1
    ]
    diversity.sort(key=lambda e: -e["writer_count"])

    return {
        "window_seconds": window,
        "pairs_examined": pairs_examined,
        "pairs_skipped_for_clock_skew": skew_skipped,
        "chains_considered": len(rows),
        # The headline. `alternating` pairs are excluded: see the docstring for
        # what that trades away.
        "novel_pairs": sum(e["count"] for e in novel),
        "novel_cross_commander_pairs": sum(
            e["count"] for e in novel if not e["same_commander"]
        ),
        "conflicts": novel[:TOP_LIMIT],
        "alternating_pairs": sum(
            e["count"] for e in ranked if e["kind"] == "alternating"
        ),
        "alternating": [e for e in ranked if e["kind"] == "alternating"][:TOP_LIMIT],
        "writer_diversity": diversity[:TOP_LIMIT],
        # Kept so a caller can see the unfiltered total this was reduced from.
        "total_pairs_same_key_different_value": sum(e["count"] for e in ranked),
    }
