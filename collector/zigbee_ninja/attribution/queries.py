"""Read-side queries for the attribution explorer (DESIGN.md paragraph 13, view 3)."""

from __future__ import annotations

import time

from ..store.db import Database
from .chains import AMBIGUOUS_COMMANDER, parse_key_digests

TOP_LIMIT = 15

# Keys that qualify HOW a command is applied rather than WHAT state it asks for.
# Two commands carrying different ramp times are not in disagreement about the
# device: one says "reach X over 0.2 s", the other "reach Y over 3 s", and the
# ramp is a property of each command, not a setting they are both steering. Left
# in, `transition` swamps the report, since nearly every render varies it.
MODIFIER_KEYS = frozenset({"transition"})


def noops(db: Database, seconds: int) -> dict:
    """No-op publishes: commands asking for state the device already held.

    Complements `redundant`, which is a duplicate test (same bytes, same
    target, inside 5 s) and therefore blind to a command that is unique on the
    wire and still changes nothing. Verdicts are stamped at command time by
    `attribution/noop.py`; this only aggregates them.

    **Read `resolution_coverage` before `counts`.** Coverage is resolved rows
    over rows seen. A low no-op count under low coverage is not an absence of
    no-ops, it is an absence of data, and the two want opposite responses.
    Anyone using this to assert "zero no-ops" has to clear the coverage bar
    first; the verdict logic deliberately refuses to call a partially-known
    payload a no-op, so the count is biased DOWN and cannot flatter the claim.
    """
    conn = db.connect()
    since = time.time() - seconds

    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT noop_verdict AS verdict, COUNT(*) AS n FROM chains "
        "WHERE opened_at >= ? AND verb = 'set' GROUP BY noop_verdict",
        (since,),
    ):
        # NULL means the row predates the detector: distinct from 'unknown',
        # which means the detector ran and could not tell.
        counts[row["verdict"] or "unstamped"] = row["n"]

    stamped = sum(n for verdict, n in counts.items() if verdict != "unstamped")
    resolved = counts.get("noop", 0) + counts.get("changing", 0)

    by_target = [
        {
            "instance": row["instance"],
            "target": row["target"],
            "noops": row["n"],
            "client": row["client"] or "(unattributed)",
        }
        for row in conn.execute(
            "SELECT instance, target, client, COUNT(*) AS n FROM chains "
            "WHERE opened_at >= ? AND noop_verdict = 'noop' "
            "GROUP BY instance, target, client ORDER BY n DESC LIMIT ?",
            (since, TOP_LIMIT),
        )
    ]

    # Which keys drive the no-ops, from the value-free basis string. This is
    # the actionable output: a key that is almost always already-held is a
    # publish site to gate.
    key_counts: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for row in conn.execute(
        "SELECT noop_verdict AS verdict, noop_basis AS basis FROM chains "
        "WHERE opened_at >= ? AND noop_basis IS NOT NULL",
        (since,),
    ):
        for token in row["basis"].split(","):
            if token.startswith("~"):
                reasons[token[1:]] = reasons.get(token[1:], 0) + 1
            elif token.startswith("=") and row["verdict"] == "noop":
                key_counts[token[1:]] = key_counts.get(token[1:], 0) + 1

    return {
        "window_seconds": seconds,
        "counts": counts,
        "resolution_coverage": round(resolved / stamped, 4) if stamped else None,
        "coverage_note": (
            "resolved / stamped. A low noop count under low coverage means no "
            "data, not no no-ops. 'unstamped' rows predate the detector."
        ),
        "noop_keys": sorted(
            ({"key": k, "noops": n} for k, n in key_counts.items()),
            key=lambda row: -row["noops"],
        )[:TOP_LIMIT],
        "unresolved_reasons": reasons,
        "top_noop_targets": by_target,
        "commander_caveat": (
            "client labels come from HA context ids and can cross-attribute "
            "sibling scripts under one parent; verify a construct has a code "
            "path to the key before acting on a per-commander count."
        ),
    }


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
            if key in MODIFIER_KEYS:
                continue
            ident = (row["instance"], row["target"], key)
            seen_values.setdefault(ident, set()).add(digest)
            writer = row["client"] or "(unattributed)"
            # An ambiguous row is one command the collector could not pin on a
            # single publisher. Counting it as its own writer would inflate the
            # divided-ownership measure with the collector's own uncertainty,
            # which is the failure this metric exists to be trusted about.
            if writer != AMBIGUOUS_COMMANDER:
                writers.setdefault(ident, set()).add(writer)

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
                    and key not in MODIFIER_KEYS
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
                            # "Two owners are fighting" is a claim about who,
                            # and an ambiguous side cannot support it. Kept
                            # visible rather than dropped, but excluded from the
                            # cross-commander headline below.
                            "ambiguous_commander": AMBIGUOUS_COMMANDER in (a, b),
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
            e["count"]
            for e in novel
            if not e["same_commander"] and not e["ambiguous_commander"]
        ),
        # Surfaced rather than silently folded away: these are pairs that look
        # cross-commander but rest on a commander the collector could not pin.
        "novel_pairs_with_ambiguous_commander": sum(
            e["count"] for e in novel if e["ambiguous_commander"]
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
