"""Read-side queries for the attribution explorer (DESIGN.md paragraph 13, view 3)."""

from __future__ import annotations

import time

from ..store.db import Database

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
