"""Groupcast economics (V2_PROPOSAL.md §V2-5 detector 2).

Three directions, all from recorded chains priced in the ledger's currency
(the same fixed ZCL byte estimates, this mesh's router census, and the
measured avg_tx / MAC retry rate when counter windows have produced them):

(a) **Fan-out collapse**: near-simultaneous unicasts carrying an identical
    payload to several devices. If one amplified groupcast would cost less
    on this mesh, recommend retargeting to an existing group with exactly
    that membership, or creating one. If an identical group command already
    rides alongside the unicasts, the unicasts are double delivery and the
    recommendation is to drop them.
(b) **Amplification losers**: groups so small or so router-heavy a mesh
    that per-member unicasts beat the broadcast amplification. Individual
    commands arrive sequentially, so members stop changing in the same
    instant; the finding says so and the confidence stays medium.
(c) **Co-fired groups**: a group whose commands nearly always arrive
    alongside an identical command to another group containing all of its
    members; one of the two commands is redundant traffic.

Payload identity comes from the chains' persisted payload digest, so these
findings only see traffic recorded after that column landed.
"""

from __future__ import annotations

import hashlib

from ..capacity import airtime, ledger
from .context import DetectorContext
from .store import Finding

NAME = "groupcast_economics"

FANOUT_SPAN_SECONDS = 3.0
MIN_FANOUT_TARGETS = 4
MIN_OCCURRENCES = 3
SAVING_FLOOR_US_PER_S = 10.0
# (b) fires only when unicasts undercut the groupcast by at least this share.
SAVING_MARGIN = 0.3
COFIRE_WINDOW_SECONDS = 1.0
COFIRE_FRACTION = 0.9
MAJORITY_FRACTION = 0.6
UNATTRIBUTED = "(unattributed)"
MULTIPLE_COMMANDERS = "(multiple commanders)"


def _label(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    top, top_count = max(counts.items(), key=lambda item: item[1])
    return top if top_count / total >= MAJORITY_FRACTION else MULTIPLE_COMMANDERS


def _prices(ctx: DetectorContext, instance: str) -> dict:
    avg_tx, retry_rate = ctx.pricing(instance)
    routers = ctx.router_count_for(instance)
    return {
        "unicast_us": airtime.unicast_airtime_us(
            ledger.ZCL_SET_BYTES, retry_rate=retry_rate or 0.0
        ),
        "groupcast_us": airtime.groupcast_airtime_us(
            ledger.ZCL_SET_BYTES, routers, avg_tx=avg_tx or airtime.DEFAULT_AVG_TX
        ),
        "routers": routers,
        "avg_tx": round(avg_tx, 3) if avg_tx is not None else airtime.DEFAULT_AVG_TX,
        "avg_tx_measured": avg_tx is not None,
    }


def _rates(saved_us: float, seconds: float) -> dict:
    us_per_s = saved_us / seconds
    return {
        "us_per_s": round(us_per_s, 1),
        "pct_of_budget": round(us_per_s / airtime.CHANNEL_BUDGET_US_PER_S * 100.0, 4),
    }


def _pricing_evidence(prices: dict) -> dict:
    return {
        "kind": "pricing",
        "unicast_us": round(prices["unicast_us"], 1),
        "groupcast_us": round(prices["groupcast_us"], 1),
        "routers": prices["routers"],
        "avg_tx": prices["avg_tx"],
        "avg_tx_measured": prices["avg_tx_measured"],
    }


def detect(ctx: DetectorContext) -> list[Finding]:
    rows = ctx.conn.execute(
        "SELECT instance, target, opened_at, client, payload_digest FROM chains "
        "WHERE opened_at >= ? AND verb = 'set' AND payload_digest IS NOT NULL "
        "ORDER BY instance, opened_at",
        (ctx.window_start(),),
    ).fetchall()
    by_instance: dict[str, list] = {}
    for row in rows:
        by_instance.setdefault(row["instance"], []).append(row)

    findings: list[Finding] = []
    for instance, commands in by_instance.items():
        group_commands = [
            row for row in commands if ctx.is_group(instance, row["target"])
        ]
        device_commands = [
            row for row in commands if not ctx.is_group(instance, row["target"])
        ]
        prices = _prices(ctx, instance)
        findings.extend(
            _fanouts(ctx, instance, device_commands, group_commands, prices)
        )
        findings.extend(_amplification_losers(ctx, instance, group_commands, prices))
        findings.extend(_cofired_groups(ctx, instance, group_commands, prices))
    return findings


# -- (a) fan-out collapse ------------------------------------------------------------


def _fanouts(
    ctx: DetectorContext,
    instance: str,
    device_commands: list,
    group_commands: list,
    prices: dict,
) -> list[Finding]:
    by_digest: dict[str, list] = {}
    for row in device_commands:
        by_digest.setdefault(row["payload_digest"], []).append(row)
    group_times: dict[str, list] = {}
    for row in group_commands:
        group_times.setdefault(row["payload_digest"], []).append(row)

    # One occurrence = a cluster of same-payload unicasts to several devices
    # inside the span. Occurrences aggregate by (commander, target set).
    occurrences: dict[tuple[str, frozenset], list[dict]] = {}
    for digest, cluster_rows in by_digest.items():
        cluster: list = []
        for row in cluster_rows + [None]:
            if cluster and (
                row is None or row["opened_at"] - cluster[0]["opened_at"] > FANOUT_SPAN_SECONDS
            ):
                targets = {entry["target"] for entry in cluster}
                if len(targets) >= MIN_FANOUT_TARGETS:
                    commanders: dict[str, int] = {}
                    for entry in cluster:
                        name = entry["client"] or UNATTRIBUTED
                        commanders[name] = commanders.get(name, 0) + 1
                    start = cluster[0]["opened_at"]
                    end = cluster[-1]["opened_at"]
                    covered = any(
                        start - COFIRE_WINDOW_SECONDS
                        <= group_row["opened_at"]
                        <= end + COFIRE_WINDOW_SECONDS
                        for group_row in group_times.get(digest, [])
                    )
                    key = (_label(commanders), frozenset(targets))
                    occurrences.setdefault(key, []).append(
                        {"start": start, "end": end, "covered": covered}
                    )
                cluster = []
            if row is not None:
                cluster.append(row)

    findings = []
    for (commander, targets), events in occurrences.items():
        if len(events) < MIN_OCCURRENCES:
            continue
        n = len(targets)
        unicast_sum = n * prices["unicast_us"]
        groupcast = prices["groupcast_us"]
        double_sent = sum(1 for event in events if event["covered"])
        signature = hashlib.sha1(
            "|".join(sorted(targets)).encode()
        ).hexdigest()[:8]
        subject = f"{commander} fan-out [{signature}]"
        sample_targets = sorted(targets)[:10]
        windows = [
            {"kind": "window", "instance": instance, "start": round(e["start"], 3),
             "end": round(e["end"], 3)}
            for e in events[:3]
        ]

        if double_sent / len(events) >= COFIRE_FRACTION:
            saved = _rates(unicast_sum * len(events), ctx.lookback_seconds)
            if saved["us_per_s"] < SAVING_FLOOR_US_PER_S:
                continue
            findings.append(
                Finding(
                    detector=NAME,
                    instance=instance,
                    subject=subject,
                    finding=(
                        f"{commander} sends the same command to {n} devices "
                        f"individually while an identical group command already "
                        f"covers them ({len(events)} times in the last 24 h). The "
                        f"individual commands are double delivery; dropping them "
                        f"saves about {saved['us_per_s']:.0f} µs/s of airtime."
                    ),
                    action={
                        "kind": "dedupe",
                        "commander": commander,
                        "instance": instance,
                        "targets": sample_targets,
                        "drop": "per-device commands",
                    },
                    saving={
                        **saved,
                        "basis": f"replayed {len(events)} recorded fan-outs from the last 24 h",
                        "provenance": "modeled",
                    },
                    confidence="high",
                    evidence=[*windows, _pricing_evidence(prices)],
                    fingerprint={
                        "us_per_s": saved["us_per_s"],
                        "occurrences": len(events),
                        "targets": n,
                    },
                )
            )
            continue

        if groupcast >= unicast_sum:
            continue  # unicasts are already the cheaper shape on this mesh
        saved = _rates((unicast_sum - groupcast) * len(events), ctx.lookback_seconds)
        if saved["us_per_s"] < SAVING_FLOOR_US_PER_S:
            continue
        exact_group = None
        for group in ctx.groups(instance):
            name = group.get("friendly_name")
            if name and set(ctx.group_members(instance, name)) == targets:
                exact_group = name
                break
        pct = (unicast_sum - groupcast) / unicast_sum * 100.0
        if exact_group:
            action = {"kind": "retarget", "commander": commander, "instance": instance,
                      "group": exact_group, "targets": sample_targets}
            how = f"one command to group {exact_group} (exactly these devices)"
            confidence = "high"
        else:
            action = {"kind": "regroup", "commander": commander, "instance": instance,
                      "members": sample_targets}
            how = "one command to a new group with exactly these members"
            confidence = "medium"
        findings.append(
            Finding(
                detector=NAME,
                instance=instance,
                subject=subject,
                finding=(
                    f"{commander} sends the same command to {n} devices individually, "
                    f"{len(events)} times in the last 24 h; {how} would cost about "
                    f"{pct:.0f}% less airtime on this mesh "
                    f"({groupcast / 1000.0:.1f}k vs {unicast_sum / 1000.0:.1f}k µs per burst)."
                ),
                action=action,
                saving={
                    **saved,
                    "basis": f"replayed {len(events)} recorded fan-outs from the last 24 h",
                    "provenance": "modeled",
                },
                confidence=confidence,
                evidence=[*windows, _pricing_evidence(prices)],
                fingerprint={
                    "us_per_s": saved["us_per_s"],
                    "occurrences": len(events),
                    "targets": n,
                },
            )
        )
    return findings


# -- (b) groups that lose to unicast --------------------------------------------------


def _amplification_losers(
    ctx: DetectorContext, instance: str, group_commands: list, prices: dict
) -> list[Finding]:
    by_group: dict[str, list] = {}
    for row in group_commands:
        by_group.setdefault(row["target"], []).append(row)

    findings = []
    for group, rows in by_group.items():
        if len(rows) < MIN_OCCURRENCES:
            continue
        members = ctx.group_members(instance, group)
        if not members:
            continue
        unicast_sum = len(members) * prices["unicast_us"]
        groupcast = prices["groupcast_us"]
        if unicast_sum >= groupcast * (1.0 - SAVING_MARGIN):
            continue
        saved = _rates((groupcast - unicast_sum) * len(rows), ctx.lookback_seconds)
        if saved["us_per_s"] < SAVING_FLOOR_US_PER_S:
            continue
        commanders: dict[str, int] = {}
        for row in rows:
            name = row["client"] or UNATTRIBUTED
            commanders[name] = commanders.get(name, 0) + 1
        pct = (groupcast - unicast_sum) / groupcast * 100.0
        findings.append(
            Finding(
                detector=NAME,
                instance=instance,
                subject=f"group {group}",
                finding=(
                    f"Group {group} has {len(members)} members, but every router on "
                    f"this mesh relays a group command ({prices['routers']} routers, "
                    f"about {prices['avg_tx']:.1f} transmissions each), costing about "
                    f"{groupcast / 1000.0:.1f}k µs per command; {len(members)} individual "
                    f"commands would cost about {unicast_sum / 1000.0:.1f}k µs "
                    f"({pct:.0f}% less). {len(rows)} commands in the last 24 h. "
                    f"Individual commands arrive one after another, so the members "
                    f"would no longer change in the same instant."
                ),
                action={
                    "kind": "retarget",
                    "instance": instance,
                    "group": group,
                    "to": "per-member commands",
                    "members": sorted(members)[:10],
                },
                saving={
                    **saved,
                    "basis": f"replayed {len(rows)} recorded group commands from the last 24 h",
                    "provenance": "modeled",
                },
                confidence="medium",
                evidence=[
                    {
                        "kind": "group",
                        "group": group,
                        "members": len(members),
                        "commands": len(rows),
                        "commanders": commanders,
                    },
                    _pricing_evidence(prices),
                ],
                fingerprint={
                    "us_per_s": saved["us_per_s"],
                    "commands": len(rows),
                    "members": len(members),
                    "routers": prices["routers"],
                },
            )
        )
    return findings


# -- (c) co-fired overlapping groups ---------------------------------------------------


def _cofired_groups(
    ctx: DetectorContext, instance: str, group_commands: list, prices: dict
) -> list[Finding]:
    by_group: dict[str, list] = {}
    for row in group_commands:
        by_group.setdefault(row["target"], []).append(row)

    findings = []
    for inner, inner_rows in by_group.items():
        if len(inner_rows) < MIN_OCCURRENCES:
            continue
        inner_members = set(ctx.group_members(instance, inner))
        if not inner_members:
            continue
        for outer, outer_rows in by_group.items():
            if outer == inner:
                continue
            outer_members = set(ctx.group_members(instance, outer))
            if not outer_members or not inner_members <= outer_members:
                continue
            matched = 0
            for row in inner_rows:
                if any(
                    abs(row["opened_at"] - other["opened_at"]) <= COFIRE_WINDOW_SECONDS
                    and row["payload_digest"] == other["payload_digest"]
                    for other in outer_rows
                ):
                    matched += 1
            fraction = matched / len(inner_rows)
            if fraction < COFIRE_FRACTION:
                continue
            saved = _rates(prices["groupcast_us"] * matched, ctx.lookback_seconds)
            if saved["us_per_s"] < SAVING_FLOOR_US_PER_S:
                continue
            findings.append(
                Finding(
                    detector=NAME,
                    instance=instance,
                    subject=f"group {inner} alongside {outer}",
                    finding=(
                        f"{matched} of {len(inner_rows)} commands to group {inner} in "
                        f"the last 24 h arrived alongside an identical command to "
                        f"group {outer}, whose members include all of {inner}'s. One "
                        f"of the two commands is redundant; dropping the {inner} "
                        f"command saves about {saved['us_per_s']:.0f} µs/s."
                    ),
                    action={
                        "kind": "regroup",
                        "instance": instance,
                        "drop_command_to": inner,
                        "covered_by": outer,
                    },
                    saving={
                        **saved,
                        "basis": (
                            f"replayed {matched} recorded co-fired commands from the "
                            f"last 24 h"
                        ),
                        "provenance": "modeled",
                    },
                    confidence="high" if fraction >= 0.95 else "medium",
                    evidence=[
                        {
                            "kind": "cofire",
                            "inner": inner,
                            "outer": outer,
                            "matched": matched,
                            "total": len(inner_rows),
                            "window_s": COFIRE_WINDOW_SECONDS,
                        },
                        _pricing_evidence(prices),
                    ],
                    fingerprint={
                        "us_per_s": saved["us_per_s"],
                        "occurrences": matched,
                        "overlap_pct": round(fraction * 100.0, 1),
                    },
                )
            )
    return findings
