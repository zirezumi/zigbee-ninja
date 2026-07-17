"""Scenario engine: price what-if moves between coordinators (§V2-11).

A scenario is a list of moves: a device to another instance, or a whole
group (its members travel with it). Pricing honors the physics the ledger
already prices, on recorded traffic only; nothing here transmits:

1. Router census shifts reprice every existing groupcast on both meshes;
   that second-order term reports separately per mesh.
2. Autonomous reporting moves with the device at its recorded rate.
3. Recorded chains targeting moved subjects re-cost on the destination's
   census and measured avg_tx/retry rate.
4. Moving a subset of a group's members breaks the group: both resolutions
   are priced (per-device unicasts to the movers, or a new destination
   group), and the aggregate uses the move's requested resolution.
5. The verdict is the burst overlay, not steady rates: the identity-bearing
   T0 command stream (only T0 carries device identity; wire frames do not)
   recomposes onto the per-scenario meshes, and sliding 1 s / 10 s command
   peaks are judged against each mesh's measured sustained limit and hard
   ceiling. The judged currency is commands per second: that is what the
   calibration limits measure and what the envelope machinery counts;
   device reports relocate in the steady term instead (T0 state events
   also include Z2M-synthetic group states that never touch the mesh).
   The measured wire before-peak shows alongside so the fidelity
   difference stays visible.
6. Instances sharing a channel pool one airtime budget.
7. Radio feasibility is an explicit unknown on every cross-mesh move: the
   engine surfaces the device's best observed link LQI and the destination
   channel as context only, never a green check.

Every number carries provenance and a basis; the currency matches Top
spenders: comparable estimates, not meter readings.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable

from ..store.db import Database
from ..store.events import RawEventLog
from . import headroom, ledger
from .airtime import CHANNEL_BUDGET_US_PER_S
from .envelope import (
    _benchmark_windows,
    _hard_ceiling,
    _sliding_peak,
    _span_excluded,
    _wire_peaks,
)

DEFAULT_WINDOW_SECONDS = 24 * 3600
MAX_WINDOW_SECONDS = 48 * 3600  # chains and the raw event store keep 48 h
MAX_MOVES = 50
COMMAND_KINDS = ("command",)
PEAK_REFINE_BINS = 8
NEAR_SUSTAINED_FRACTION = 0.8

RESOLUTION_UNICASTS = "unicasts"
RESOLUTION_NEW_GROUP = "new_group"
RESOLUTIONS = (RESOLUTION_UNICASTS, RESOLUTION_NEW_GROUP)

STEADY_PROVENANCE = (
    "modeled (recorded chains and reports repriced at current parameters)"
)
SECOND_ORDER_PROVENANCE = "modeled (stayer groupcast load scaled by the census ratio)"
OVERLAY_PROVENANCE = "modeled (T0 stream recomposition)"
RADIO_PROVENANCE = "modeled (recorded data cannot show radio reach)"
BASIS_NOTE = "comparable estimates, not meter readings"
REPORT_NOTE = (
    "report cost carries no retry term in the §10 RX model; the recorded rate relocates"
)


class ScenarioError(ValueError):
    """Invalid scenario input: unknown subject or instance, bad move shape."""


# -- resolution ---------------------------------------------------------------------


def _validate_moves(moves: list[dict], bases: set[str]) -> None:
    if not moves:
        raise ScenarioError("a scenario needs at least one move")
    if len(moves) > MAX_MOVES:
        raise ScenarioError(f"at most {MAX_MOVES} moves per scenario")
    for move in moves:
        kind = move.get("kind")
        if kind not in ("device", "group"):
            raise ScenarioError(f"move kind must be device or group, not {kind!r}")
        for field in ("subject", "from_instance", "to_instance"):
            if not move.get(field):
                raise ScenarioError(f"move is missing {field}")
        if move["from_instance"] == move["to_instance"]:
            raise ScenarioError(
                f"{move['subject']}: from_instance and to_instance are the same"
            )
        for field in ("from_instance", "to_instance"):
            if move[field] not in bases:
                raise ScenarioError(f"unknown instance {move[field]!r}")
        resolution = move.get("group_resolution")
        if resolution is not None and resolution not in RESOLUTIONS:
            raise ScenarioError(
                f"group_resolution must be one of {RESOLUTIONS}, not {resolution!r}"
            )


def _device_index(registry, base: str) -> dict[str, dict]:
    return {
        device["friendly_name"]: device
        for device in registry.devices(base)
        if device.get("friendly_name")
    }


def _resolve(registry, moves: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """Resolve moves into per-device travel plans and moved-group records.

    Returns (moved_devices keyed by (instance, name) flattened to
    "instance/name", moved_groups)."""
    moved_devices: dict[str, dict] = {}
    moved_groups: list[dict] = []

    def add_device(device: dict, move: dict, via_group: str | None) -> None:
        key = f"{move['from_instance']}/{device['friendly_name']}"
        existing = moved_devices.get(key)
        if existing is not None:
            if existing["to_instance"] != move["to_instance"]:
                raise ScenarioError(
                    f"{device['friendly_name']} is moved to two different instances"
                )
            return
        moved_devices[key] = {
            "name": device["friendly_name"],
            "ieee": device.get("ieee_address"),
            "router": device.get("type") == "Router",
            "from_instance": move["from_instance"],
            "to_instance": move["to_instance"],
            "via_group": via_group,
            "group_resolution": move.get("group_resolution") or RESOLUTION_UNICASTS,
        }

    for move in moves:
        source = move["from_instance"]
        if move["kind"] == "device":
            device = _device_index(registry, source).get(move["subject"])
            if device is None:
                raise ScenarioError(
                    f"device {move['subject']!r} is not on {source}"
                )
            add_device(device, move, via_group=None)
        else:
            group = next(
                (
                    g
                    for g in registry.groups(source)
                    if g.get("friendly_name") == move["subject"]
                ),
                None,
            )
            if group is None:
                raise ScenarioError(f"group {move['subject']!r} is not on {source}")
            by_ieee = {
                d.get("ieee_address"): d for d in registry.devices(source)
            }
            members = [
                by_ieee[ieee] for ieee in group.get("member_ieee") or [] if ieee in by_ieee
            ]
            moved_groups.append(
                {
                    "name": move["subject"],
                    "from_instance": source,
                    "to_instance": move["to_instance"],
                    "members": [m["friendly_name"] for m in members],
                }
            )
            for member in members:
                add_device(member, move, via_group=move["subject"])

    return moved_devices, moved_groups


def _find_splits(
    registry, moved_devices: dict[str, dict], moved_group_names: set[tuple[str, str]]
) -> list[dict]:
    """Groups left behind that lose members: each is a split to model both
    ways (§V2-11 item 4). A group moved whole in the same scenario is not a
    split."""
    splits: list[dict] = []
    by_instance: dict[str, dict[str, dict]] = {}
    for plan in moved_devices.values():
        by_instance.setdefault(plan["from_instance"], {})[plan["name"]] = plan
    for instance, movers in by_instance.items():
        ieee_to_name = {
            d.get("ieee_address"): d["friendly_name"]
            for d in registry.devices(instance)
            if d.get("friendly_name")
        }
        mover_names = set(movers)
        for group in registry.groups(instance):
            group_name = group.get("friendly_name")
            if not group_name or (instance, group_name) in moved_group_names:
                continue
            member_names = {
                ieee_to_name[ieee]
                for ieee in group.get("member_ieee") or []
                if ieee in ieee_to_name
            }
            hit = sorted(member_names & mover_names)
            if hit:
                splits.append(
                    {
                        "group": group_name,
                        "instance": instance,
                        "movers": hit,
                        "stayers": len(member_names) - len(hit),
                        "member_count": len(member_names),
                        "resolution": movers[hit[0]]["group_resolution"],
                    }
                )
    return splits


# -- recorded traffic ---------------------------------------------------------------


def _chain_rows(conn, instance: str, start: float) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT target, verb, COUNT(*) AS n, COALESCE(SUM(echo_count), 0) AS echoes "
            "FROM chains WHERE instance = ? AND opened_at >= ? GROUP BY target, verb",
            (instance, start),
        )
    ]


def _autonomous_rates(conn, instance: str, days: list[str], seconds: float) -> dict[str, float]:
    """Recorded device-initiated publishes per second over the ledger days
    covering the window."""
    placeholders = ",".join("?" * len(days))
    return {
        row["device"]: row["publishes"] / seconds
        for row in conn.execute(
            f"SELECT device, SUM(publishes) AS publishes FROM ledger_device_daily "
            f"WHERE instance = ? AND day IN ({placeholders}) GROUP BY device",
            (instance, *days),
        )
    }


def _chain_cost_us(
    row: dict,
    *,
    group_target: bool,
    n_routers: int,
    avg_tx: float | None,
    retry_rate: float | None,
) -> float:
    """Total modeled airtime for one aggregated (target, verb) chain row:
    the command frames plus every recorded echo as one report arrival."""
    price = ledger.price_chain(
        verb=row["verb"],
        group_target=group_target,
        n_routers=n_routers,
        echo_count=0,
        avg_tx=avg_tx,
        retry_rate=retry_rate,
    )
    return row["n"] * price.tx_us + row["echoes"] * ledger.autonomous_publish_cost_us()


# -- burst overlay ------------------------------------------------------------------


def _subject_topics(names: list[str]) -> tuple[str, ...]:
    """T0 command topics that carry a subject's identity."""
    out: list[str] = []
    for name in names:
        out.extend((f"{name}/set", f"{name}/get"))
    return tuple(out)


def _t0_bins(
    events_log: RawEventLog,
    instance: str,
    start: float,
    end: float,
    bucket_s: float,
    windows: list[tuple[float, float]],
    targets: tuple[str, ...] | None = None,
) -> Counter:
    bins: Counter = Counter()
    for index, count in events_log.rate_bins(
        instance,
        start,
        end,
        bucket_s,
        source="mqtt",
        kinds=COMMAND_KINDS,
        targets=targets,
    ):
        bin_start = start + index * bucket_s
        if not _span_excluded(bin_start, bin_start + bucket_s, windows):
            bins[index] = count
    return bins


def _refined_peak(
    events_log: RawEventLog,
    start: float,
    windows_by_instance: dict[str, list[tuple[float, float]]],
    bins: Counter,
    home: str,
    out_targets: tuple[str, ...],
    inbound: list[tuple[str, tuple[str, ...]]],
) -> dict | None:
    """Exact sliding 1 s peak of the recomposed stream around its busiest
    fixed bins. ``inbound`` lists (source_instance, topics) whose events
    re-home onto ``home``."""
    if not bins:
        return None
    best_eps, best_at = 0.0, None
    for index, count in sorted(bins.items(), key=lambda item: item[1], reverse=True)[
        :PEAK_REFINE_BINS
    ]:
        window_start = start + index

        def times(
            instance: str,
            targets: tuple[str, ...] | None,
            lo: float = window_start - 1.0,
            hi: float = window_start + 2.0,
        ) -> list[float]:
            return [
                ts
                for ts in events_log.event_times(
                    instance,
                    lo,
                    hi,
                    source="mqtt",
                    kinds=COMMAND_KINDS,
                    targets=targets,
                )
                if not _span_excluded(ts, ts, windows_by_instance.get(instance, []))
            ]

        stream = Counter(times(home, None))
        if out_targets:
            stream.subtract(Counter(times(home, out_targets)))
        for source, topics in inbound:
            stream.update(times(source, topics))
        merged = sorted(ts for ts, n in stream.items() for _ in range(max(n, 0)))
        peak, at = _sliding_peak(merged)
        peak = max(peak, float(count))
        if peak > best_eps:
            best_eps, best_at = peak, at if at else window_start
    if best_at is None:
        return None
    return {"eps_1s": best_eps, "at": round(best_at, 3)}


def _judge(peak_eps: float | None, limits: dict | None) -> str:
    if peak_eps is None:
        return "no_traffic"
    if not limits or not limits.get("sustained_eps"):
        return "no_limits"
    ceiling = limits.get("ceiling_eps")
    if ceiling and peak_eps > ceiling:
        return "above_ceiling"
    sustained = limits["sustained_eps"]
    if peak_eps > sustained:
        return "above_sustained"
    if peak_eps >= NEAR_SUSTAINED_FRACTION * sustained:
        return "near_sustained"
    return "ok"


# -- context ------------------------------------------------------------------------


def _limits(db: Database, registry, base: str) -> dict | None:
    modes = headroom.latest_knees(db).get(base, {})
    sustained = modes.get("spread") or modes.get("single")
    ceiling = _hard_ceiling(db.connect(), base)
    if sustained is None and ceiling is None:
        return None
    out: dict = {}
    if sustained is not None:
        current = next(
            (
                info.get("version")
                for info in registry.snapshot()
                if info.get("base_topic") == base
            ),
            None,
        )
        measured_env = (sustained.get("environment") or {}).get("z2m_version")
        out.update(
            {
                "sustained_eps": sustained["eps"],
                "sustained_kind": sustained["kind"],
                "mode": sustained["mode"],
                "measured_at": sustained["measured_at"],
                "stale_environment": bool(
                    measured_env and current and measured_env != current
                ),
            }
        )
    if ceiling is not None:
        out["ceiling_eps"] = ceiling
    return out


def _best_link_lqi(topology_entry: dict, ieee: str | None) -> int | None:
    """Best LQI on any observed link touching the device in the latest
    stored network map; context only, never a feasibility verdict."""
    if not ieee:
        return None
    best: int | None = None
    for link in (topology_entry.get("raw") or {}).get("links") or []:
        source = str(
            link.get("sourceIeeeAddr") or (link.get("source") or {}).get("ieeeAddr") or ""
        )
        target = str(
            link.get("targetIeeeAddr") or (link.get("target") or {}).get("ieeeAddr") or ""
        )
        if ieee not in (source, target):
            continue
        lqi = link.get("lqi", link.get("linkquality"))
        if isinstance(lqi, (int, float)) and (best is None or lqi > best):
            best = int(lqi)
    return best


# -- lane context -------------------------------------------------------------------


def context_summary(
    events_log: RawEventLog,
    db: Database,
    registry,
    pricing_params: Callable[[str], tuple[float | None, float | None]],
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Everything the Rebalance view's lanes need before any move is staged:
    per instance the steady spend, the recorded command peak in the judged
    currency with its verdict, the measured limits, and every device and
    group with its recorded spend as a cost badge. The arithmetic is the
    same the pricing path uses, so a staged scenario's before-numbers always
    match the lanes."""
    now = clock()
    window_seconds = max(600, min(int(window_seconds), MAX_WINDOW_SECONDS))
    start = now - window_seconds
    conn = db.connect()
    snapshot = registry.snapshot()
    bases = {info["base_topic"] for info in snapshot}
    channels = {info["base_topic"]: info.get("channel") for info in snapshot}
    windows = _benchmark_windows(conn, start)
    days = ledger._days_covering(start, now)
    recording_since = ledger._recording_since(conn)
    ledger_seconds = max(1.0, now - max(start, recording_since or start))
    report_us = ledger.autonomous_publish_cost_us()

    instances: dict[str, dict] = {}
    for base in sorted(bases):
        n_routers = registry.router_count_for(base)
        avg_tx, retry_rate = pricing_params(base)
        rows = _chain_rows(conn, base, start)
        autonomous = _autonomous_rates(conn, base, days, ledger_seconds)

        target_us: dict[str, float] = {}
        for row in rows:
            cost = _chain_cost_us(
                row,
                group_target=registry.is_group(base, row["target"]),
                n_routers=n_routers,
                avg_tx=avg_tx,
                retry_rate=retry_rate,
            ) / window_seconds
            target_us[row["target"]] = target_us.get(row["target"], 0.0) + cost
        steady = sum(target_us.values()) + sum(autonomous.values()) * report_us

        ieee_to_name = {
            d.get("ieee_address"): d["friendly_name"]
            for d in registry.devices(base)
            if d.get("friendly_name")
        }
        memberships: dict[str, list[str]] = {}
        groups: list[dict] = []
        for group in registry.groups(base):
            group_name = group.get("friendly_name")
            if not group_name:
                continue
            members = [
                ieee_to_name[ieee]
                for ieee in group.get("member_ieee") or []
                if ieee in ieee_to_name
            ]
            for member in members:
                memberships.setdefault(member, []).append(group_name)
            groups.append(
                {
                    "name": group_name,
                    "id": group.get("id"),
                    "members": members,
                    "us_per_s": round(target_us.get(group_name, 0.0), 3),
                }
            )
        devices = [
            {
                "name": device["friendly_name"],
                "ieee": device.get("ieee_address"),
                "router": device.get("type") == "Router",
                "us_per_s": round(
                    target_us.get(device["friendly_name"], 0.0)
                    + autonomous.get(device["friendly_name"], 0.0) * report_us,
                    3,
                ),
                "groups": memberships.get(device["friendly_name"], []),
            }
            for device in registry.devices(base)
            if device.get("friendly_name")
        ]

        bins = _t0_bins(events_log, base, start, now, 1.0, windows.get(base, []))
        peak = _refined_peak(events_log, start, windows, bins, base, (), [])
        limits = _limits(db, registry, base)
        instances[base] = {
            "channel": channels.get(base),
            "steady": {
                "us_per_s": round(steady, 3),
                "pct_of_budget": round(steady / CHANNEL_BUDGET_US_PER_S * 100.0, 4),
                "provenance": STEADY_PROVENANCE,
            },
            "burst": {
                "peak_1s": peak,
                "verdict": _judge(peak["eps_1s"] if peak else None, limits),
                "provenance": OVERLAY_PROVENANCE,
            },
            "limits": limits,
            "census": {"routers": n_routers},
            "devices": devices,
            "groups": groups,
        }

    return {
        "window_seconds": window_seconds,
        "basis": {"note": BASIS_NOTE},
        "instances": instances,
    }


# -- the engine ---------------------------------------------------------------------


def price_scenario(
    events_log: RawEventLog,
    db: Database,
    registry,
    pricing_params: Callable[[str], tuple[float | None, float | None]],
    topology_latest: Callable[[str], dict],
    moves: list[dict],
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    clock: Callable[[], float] = time.time,
) -> dict:
    now = clock()
    window_seconds = max(600, min(int(window_seconds), MAX_WINDOW_SECONDS))
    start = now - window_seconds
    conn = db.connect()

    snapshot = registry.snapshot()
    bases = {info["base_topic"] for info in snapshot}
    channels = {
        info["base_topic"]: info.get("channel")
        for info in snapshot
    }
    _validate_moves(moves, bases)
    moved_devices, moved_groups = _resolve(registry, moves)
    moved_group_keys = {(g["from_instance"], g["name"]) for g in moved_groups}
    splits = _find_splits(registry, moved_devices, moved_group_keys)

    # Census before and after: routers shift it, end devices do not.
    routers_before = {base: registry.router_count_for(base) for base in bases}
    routers_after = dict(routers_before)
    for plan in moved_devices.values():
        if plan["router"]:
            routers_after[plan["from_instance"]] -= 1
            routers_after[plan["to_instance"]] += 1

    params = {base: pricing_params(base) for base in bases}
    days = ledger._days_covering(start, now)
    recording_since = ledger._recording_since(conn)
    ledger_seconds = max(1.0, now - max(start, recording_since or start))
    windows = _benchmark_windows(conn, start)

    chain_rows = {base: _chain_rows(conn, base, start) for base in bases}
    autonomous = {
        base: _autonomous_rates(conn, base, days, ledger_seconds) for base in bases
    }

    def chain_cost(base: str, row: dict, *, n_routers: int, to_base: str | None = None) -> float:
        """Modeled µs for one aggregated chain row, priced with the given
        census and the parameters of the mesh it runs on."""
        avg_tx, retry_rate = params[to_base or base]
        return _chain_cost_us(
            row,
            group_target=registry.is_group(base, row["target"]),
            n_routers=n_routers,
            avg_tx=avg_tx,
            retry_rate=retry_rate,
        )

    # Steady before-state per instance, in the deltas' own arithmetic.
    steady_before: dict[str, float] = {}
    groupcast_before: dict[str, float] = {}
    for base in bases:
        total = sum(
            chain_cost(base, row, n_routers=routers_before[base])
            for row in chain_rows[base]
        ) / window_seconds
        total += sum(autonomous[base].values()) * ledger.autonomous_publish_cost_us()
        steady_before[base] = total
        groupcast_before[base] = sum(
            chain_cost(base, row, n_routers=routers_before[base])
            for row in chain_rows[base]
            if registry.is_group(base, row["target"])
        ) / window_seconds

    deltas: dict[str, float] = dict.fromkeys(bases, 0.0)
    move_reports: list[dict] = []

    # Device and group traffic relocation (§V2-11 items 2 and 3).
    moved_names_by_source: dict[str, list[str]] = {}
    for plan in moved_devices.values():
        moved_names_by_source.setdefault(plan["from_instance"], []).append(plan["name"])

    def relocate_rows(source: str, dest: str, rows: list[dict], group: bool) -> dict:
        before_us = sum(
            chain_cost(source, row, n_routers=routers_before[source]) for row in rows
        )
        after_us = sum(
            chain_cost(source, row, n_routers=routers_after[dest], to_base=dest)
            for row in rows
        )
        return {
            "chains_per_s": round(sum(row["n"] for row in rows) / window_seconds, 4),
            "before_us_per_s": round(before_us / window_seconds, 3),
            "after_us_per_s": round(after_us / window_seconds, 3),
            "provenance": STEADY_PROVENANCE,
            "grouped": group,
        }

    reported_groups = set()
    for plan in moved_devices.values():
        source, dest = plan["from_instance"], plan["to_instance"]
        rows = [r for r in chain_rows[source] if r["target"] == plan["name"]]
        commands = relocate_rows(source, dest, rows, group=False)
        publish_rate = autonomous[source].get(plan["name"], 0.0)
        reports_us = publish_rate * ledger.autonomous_publish_cost_us()
        deltas[source] -= commands["before_us_per_s"] + reports_us
        deltas[dest] += commands["after_us_per_s"] + reports_us

        entry = topology_latest(source) or {}
        move_reports.append(
            {
                "kind": "device",
                "subject": plan["name"],
                "from_instance": source,
                "to_instance": dest,
                "router": plan["router"],
                "via_group": plan["via_group"],
                "commands": commands,
                "reports": {
                    "publishes_per_s": round(publish_rate, 4),
                    "us_per_s": round(reports_us, 3),
                    "note": REPORT_NOTE,
                },
                "radio": {
                    "status": "unknown",
                    "best_observed_link_lqi": _best_link_lqi(entry, plan["ieee"]),
                    "destination_channel": channels.get(dest),
                    "provenance": RADIO_PROVENANCE,
                },
            }
        )

    for group in moved_groups:
        source, dest = group["from_instance"], group["to_instance"]
        rows = [r for r in chain_rows[source] if r["target"] == group["name"]]
        commands = relocate_rows(source, dest, rows, group=True)
        deltas[source] -= commands["before_us_per_s"]
        deltas[dest] += commands["after_us_per_s"]
        reported_groups.add((source, group["name"]))
        move_reports.append(
            {
                "kind": "group",
                "subject": group["name"],
                "from_instance": source,
                "to_instance": dest,
                "members": group["members"],
                "commands": commands,
            }
        )

    # Group splits: both resolutions priced; the requested one lands in the
    # aggregate (§V2-11 item 4).
    split_reports: list[dict] = []
    for split in splits:
        source = split["instance"]
        movers = split["movers"]
        dest = moved_devices[f"{source}/{movers[0]}"]["to_instance"]
        rows = [r for r in chain_rows[source] if r["target"] == split["group"]]
        chain_rate = sum(row["n"] for row in rows) / window_seconds
        echo_rate = sum(row["echoes"] for row in rows) / window_seconds
        # Echoes attribute per member evenly: the recorded aggregate cannot
        # say which member echoed.
        mover_share = len(movers) / max(split["member_count"], 1)
        mover_echo_us = echo_rate * mover_share * ledger.autonomous_publish_cost_us()
        avg_tx_dest, retry_dest = params[dest]
        unicast_us = chain_rate * len(movers) * ledger.price_chain(
            verb="set",
            group_target=False,
            n_routers=0,
            echo_count=0,
            retry_rate=retry_dest,
        ).tx_us
        new_group_us = chain_rate * ledger.price_chain(
            verb="set",
            group_target=True,
            n_routers=routers_after[dest],
            echo_count=0,
            avg_tx=avg_tx_dest,
        ).tx_us
        resolutions = {
            RESOLUTION_UNICASTS: round(unicast_us + mover_echo_us, 3),
            RESOLUTION_NEW_GROUP: round(new_group_us + mover_echo_us, 3),
        }
        applied = split["resolution"]
        deltas[dest] += resolutions[applied]
        deltas[source] -= mover_echo_us
        split_reports.append(
            {
                "group": split["group"],
                "instance": source,
                "to_instance": dest,
                "movers": movers,
                "stayers": split["stayers"],
                "applied_resolution": applied,
                "added_us_per_s": resolutions,
                "note": (
                    "moving members out of a group breaks it; both resolutions "
                    "are priced and the requested one is applied to the totals"
                ),
                "provenance": STEADY_PROVENANCE,
            }
        )

    # Second-order term: existing groupcasts repriced by the census shift on
    # every mesh whose router count changed (§V2-11 item 1). Moved groups'
    # own traffic already relocated above, so only stayers scale.
    second_order: dict[str, float] = {}
    for base in bases:
        if routers_after[base] == routers_before[base]:
            continue
        moved_gc = sum(
            chain_cost(base, row, n_routers=routers_before[base])
            for row in chain_rows[base]
            if (base, row["target"]) in reported_groups
        ) / window_seconds
        stayers = max(groupcast_before[base] - moved_gc, 0.0)
        ratio = (1 + routers_after[base]) / max(1 + routers_before[base], 1)
        term = stayers * (ratio - 1.0)
        second_order[base] = round(term, 3)
        deltas[base] += term

    # Burst overlay (§V2-11 item 5): recomposed T0 peaks judged per mesh.
    inbound_by_dest: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    out_topics: dict[str, tuple[str, ...]] = {}
    for source, names in moved_names_by_source.items():
        group_names = [g["name"] for g in moved_groups if g["from_instance"] == source]
        topics = _subject_topics(sorted(set(names)) + group_names)
        out_topics[source] = topics
        by_dest: dict[str, list[str]] = {}
        for name in names:
            by_dest.setdefault(
                moved_devices[f"{source}/{name}"]["to_instance"], []
            ).append(name)
        for group in moved_groups:
            if group["from_instance"] == source:
                by_dest.setdefault(group["to_instance"], []).append(group["name"])
        for dest, subjects in by_dest.items():
            inbound_by_dest.setdefault(dest, []).append(
                (source, _subject_topics(sorted(set(subjects))))
            )

    instances: dict[str, dict] = {}
    for base in sorted(bases):
        base_windows = windows.get(base, [])
        before_bins = _t0_bins(events_log, base, start, now, 1.0, base_windows)
        after_bins = Counter(before_bins)
        if base in out_topics:
            after_bins.subtract(
                _t0_bins(events_log, base, start, now, 1.0, base_windows, out_topics[base])
            )
        for source, topics in inbound_by_dest.get(base, []):
            after_bins.update(
                _t0_bins(
                    events_log, source, start, now, 1.0, windows.get(source, []), topics
                )
            )
        after_bins = Counter({k: v for k, v in after_bins.items() if v > 0})

        touched = base in out_topics or base in inbound_by_dest
        before_peak = _refined_peak(
            events_log, start, windows, before_bins, base, (), []
        )
        after_peak = (
            _refined_peak(
                events_log,
                start,
                windows,
                after_bins,
                base,
                out_topics.get(base, ()),
                inbound_by_dest.get(base, []),
            )
            if touched
            else before_peak
        )

        def ten_second_peak(bins: Counter) -> float | None:
            if not bins:
                return None
            by_ten: Counter = Counter()
            for index, count in bins.items():
                by_ten[index // 10] += count
            return round(max(by_ten.values()) / 10.0, 2)

        limits = _limits(db, registry, base)
        wire = _wire_peaks(events_log, base, start, now, base_windows)
        wire_before = (wire or {}).get("peak")
        instances[base] = {
            "steady": {
                "before_us_per_s": round(steady_before[base], 3),
                "after_us_per_s": round(steady_before[base] + deltas[base], 3),
                "before_pct_of_budget": round(
                    steady_before[base] / CHANNEL_BUDGET_US_PER_S * 100.0, 4
                ),
                "after_pct_of_budget": round(
                    (steady_before[base] + deltas[base]) / CHANNEL_BUDGET_US_PER_S * 100.0,
                    4,
                ),
                "second_order_us_per_s": second_order.get(base),
                "provenance": STEADY_PROVENANCE,
            },
            "census": {
                "routers_before": routers_before[base],
                "routers_after": routers_after[base],
            },
            "burst": {
                "before_peak_1s": before_peak,
                "after_peak_1s": after_peak,
                "before_peak_10s_eps": ten_second_peak(before_bins),
                "after_peak_10s_eps": ten_second_peak(after_bins),
                # The measured wire TX peak over the same window: the
                # fidelity reference the modeled T0 recomposition sits
                # beside, absent without tap coverage.
                "wire_before_peak_1s": wire_before,
                "verdict": _judge(
                    after_peak["eps_1s"] if after_peak else None, limits
                ),
                "provenance": OVERLAY_PROVENANCE,
            },
            "limits": limits,
            "touched": touched,
        }

    # Channel pooling (§V2-11 item 6): instances sharing a channel draw one
    # budget; scenarios move devices, not channels, so pools reflect reality.
    by_channel: dict[int, list[str]] = {}
    for base in bases:
        channel = channels.get(base)
        if isinstance(channel, int):
            by_channel.setdefault(channel, []).append(base)
    pools = [
        {
            "channel": channel,
            "instances": sorted(members),
            "combined_after_us_per_s": round(
                sum(steady_before[b] + deltas[b] for b in members), 3
            ),
            "combined_after_pct_of_budget": round(
                sum(steady_before[b] + deltas[b] for b in members)
                / CHANNEL_BUDGET_US_PER_S
                * 100.0,
                4,
            ),
        }
        for channel, members in sorted(by_channel.items())
        if len(members) > 1
    ]

    return {
        "window_seconds": window_seconds,
        "basis": {
            "chains_window_seconds": window_seconds,
            "ledger_days": days,
            "note": BASIS_NOTE,
        },
        "moves": move_reports,
        "splits": split_reports,
        "instances": instances,
        "channel_pools": pools,
    }
