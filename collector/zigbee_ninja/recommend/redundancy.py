"""Redundant-command costing (V2_PROPOSAL.md §V2-5 detector 1).

The chain tracker already marks a set command whose payload matches the
previous command to the same target inside the redundancy window; those
duplicates change nothing on the device and are the cheapest utilization
win an automation author can act on. This detector groups the marked
chains by commander and prices them exactly as the ledger did (TX shape
from the registry, echoes as last-hop reports), so the saving is the
recorded duplicates' own cost summed, in the same currency as Top
spenders.
"""

from __future__ import annotations

from ..attribution.chains import REDUNDANT_WINDOW
from ..capacity import airtime, ledger
from .context import DetectorContext
from .store import Finding

NAME = "redundancy"

SAVING_FLOOR_US_PER_S = 10.0
TOP_TARGETS = 5
UNATTRIBUTED = "(unattributed)"


def detect(ctx: DetectorContext) -> list[Finding]:
    rows = ctx.conn.execute(
        "SELECT instance, target, verb, opened_at, client, echo_count FROM chains "
        "WHERE opened_at >= ? AND redundant = 1 ORDER BY instance, opened_at",
        (ctx.window_start(),),
    ).fetchall()

    grouped: dict[tuple[str, str], dict] = {}
    pricing_cache: dict[str, tuple[float | None, float | None, int]] = {}
    for row in rows:
        instance = row["instance"]
        if instance not in pricing_cache:
            avg_tx, retry_rate = ctx.pricing(instance)
            pricing_cache[instance] = (avg_tx, retry_rate, ctx.router_count_for(instance))
        avg_tx, retry_rate, routers = pricing_cache[instance]
        price = ledger.price_chain(
            verb=row["verb"],
            group_target=ctx.is_group(instance, row["target"]),
            n_routers=routers,
            echo_count=row["echo_count"],
            avg_tx=avg_tx,
            retry_rate=retry_rate,
        )
        commander = row["client"] or UNATTRIBUTED
        entry = grouped.setdefault(
            (instance, commander),
            {"duplicates": 0, "saved_us": 0.0, "targets": {}, "last_at": 0.0},
        )
        entry["duplicates"] += 1
        entry["saved_us"] += price.total_us
        entry["targets"][row["target"]] = entry["targets"].get(row["target"], 0) + 1
        entry["last_at"] = max(entry["last_at"], row["opened_at"])

    findings: list[Finding] = []
    for (instance, commander), entry in grouped.items():
        us_per_s = entry["saved_us"] / ctx.lookback_seconds
        if us_per_s < SAVING_FLOOR_US_PER_S:
            continue
        top_targets = sorted(
            entry["targets"].items(), key=lambda item: item[1], reverse=True
        )[:TOP_TARGETS]
        pct_of_budget = us_per_s / airtime.CHANNEL_BUDGET_US_PER_S * 100.0
        findings.append(
            Finding(
                detector=NAME,
                instance=instance,
                subject=commander,
                finding=(
                    f"{commander} resent an identical command to the same target "
                    f"within {REDUNDANT_WINDOW:.0f} s, {entry['duplicates']} times in "
                    f"the last 24 h on {instance}: about {us_per_s:.0f} µs/s of "
                    f"airtime. The duplicates change nothing on the devices and can "
                    f"be dropped at the source."
                ),
                action={
                    "kind": "dedupe",
                    "commander": commander,
                    "instance": instance,
                    "targets": [target for target, _count in top_targets],
                },
                saving={
                    "us_per_s": round(us_per_s, 1),
                    "pct_of_budget": round(pct_of_budget, 4),
                    "basis": (
                        f"replayed {entry['duplicates']} recorded duplicate commands "
                        f"from the last 24 h at their ledger prices"
                    ),
                    "provenance": "modeled",
                },
                confidence="high",
                evidence=[
                    {
                        "kind": "duplicates",
                        "instance": instance,
                        "count": entry["duplicates"],
                        "top_targets": [
                            {"target": target, "count": count}
                            for target, count in top_targets
                        ],
                        "last_at": round(entry["last_at"], 3),
                    }
                ],
                fingerprint={
                    "us_per_s": round(us_per_s, 1),
                    "duplicates": entry["duplicates"],
                },
            )
        )
    return findings
