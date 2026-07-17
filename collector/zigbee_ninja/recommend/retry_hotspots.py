"""Retry-cost hotspots (V2_PROPOSAL.md §V2-5 detector 6).

Each coordinator measures its own per-hop MAC retry rate passively (the
harvested mac_tx_unicast_retry / mac_tx_unicast_success counter windows,
§10). A chronically elevated rate gets an "investigate placement/route"
finding that names the likeliest culprits from the stored topology
snapshot: weak links on the coordinator's own hop (exactly where the
measured retries happen) and heavily-routed relays riding weak links
(retries beyond this counter's visibility, offered as context).

Low confidence by construction: radio weather varies, the counter sees
only the coordinator's own transmissions, and a snapshot is one moment
of a living mesh. The finding is a place to look, never a verdict.
"""

from __future__ import annotations

from ..capacity import ledger
from ..capacity.airtime import CHANNEL_BUDGET_US_PER_S
from ..capacity.scenario import _chain_rows
from ..ingest import topology
from .context import DetectorContext
from .store import Finding

NAME = "retry_hotspots"

# A per-hop retry rate this high, sustained through the EWMA, is chronic
# rather than weather. The counter floor (fifty successes per window)
# already guards tiny samples before a rate exists at all.
RETRY_RATE_THRESHOLD = 0.05
# The topology view's own weak-link floor.
WEAK_LQI = 80
# A node relaying at least this many known routes is a hotspot when its
# links are weak.
HEAVY_ROUTES = 3
TOP_SUSPECTS = 5


def _unicast_us_per_s(ctx: DetectorContext, base: str) -> float:
    """Modeled steady unicast spend, priced retry-free: the base the
    measured retry rate multiplies."""
    rows = _chain_rows(ctx.conn, base, ctx.window_start())
    total = sum(
        row["n"]
        * ledger.price_chain(
            verb=row["verb"],
            group_target=False,
            n_routers=0,
            echo_count=0,
            retry_rate=0.0,
        ).tx_us
        for row in rows
        if not ctx.is_group(base, row["target"])
    )
    return total / ctx.lookback_seconds


def _suspects(ctx: DetectorContext, base: str) -> tuple[list[dict], list[dict], bool]:
    """(coordinator-hop weak links, weak heavy relays, snapshot_present)."""
    entry = (ctx.topology_latest(base) if ctx.topology_latest else None) or {}
    raw = entry.get("raw")
    if not raw:
        return [], [], False
    reduced = topology.graph(raw)
    nodes = {node["id"]: node for node in reduced["nodes"]}
    coordinator = (ctx.instance_info.get(base) or {}).get("coordinator_ieee")
    coordinator_weak: list[dict] = []
    weak_by_node: dict[str, int] = {}
    for edge in reduced["links"]:
        lqi = edge.get("lqi")
        if lqi is None or lqi >= WEAK_LQI:
            continue
        for end, other in ((edge["source"], edge["target"]), (edge["target"], edge["source"])):
            weak_by_node[end] = min(weak_by_node.get(end, 255), lqi)
            if other == coordinator:
                coordinator_weak.append(
                    {"device": nodes[end]["name"], "lqi": lqi}
                )
    coordinator_weak.sort(key=lambda item: item["lqi"])
    # The coordinator itself is trivially route-heavy (it terminates every
    # route) and is the measuring node; only other routers are relays worth
    # naming.
    relays = sorted(
        (
            {
                "device": node["name"],
                "routes_via": node["routes_via"],
                "weakest_lqi": weak_by_node[node_id],
            }
            for node_id, node in nodes.items()
            if node["routes_via"] >= HEAVY_ROUTES
            and node_id in weak_by_node
            and node_id != coordinator
        ),
        key=lambda item: (-item["routes_via"], item["weakest_lqi"]),
    )
    return coordinator_weak[:TOP_SUSPECTS], relays[:TOP_SUSPECTS], True


def detect(ctx: DetectorContext) -> list[Finding]:
    findings: list[Finding] = []
    for base in ctx.instances:
        _avg_tx, retry_rate = ctx.pricing(base)
        if retry_rate is None or retry_rate < RETRY_RATE_THRESHOLD:
            continue
        unicast_us = _unicast_us_per_s(ctx, base)
        overhead_us = unicast_us * retry_rate
        coordinator_weak, relays, snapshot = _suspects(ctx, base)

        sentences = [
            f"{base}'s coordinator retries about {retry_rate * 100:.0f}% of its "
            "unicast transmissions, measured from its own counters."
        ]
        if coordinator_weak:
            worst = coordinator_weak[0]
            sentences.append(
                f"Its direct link to {worst['device']} reads link quality "
                f"{worst['lqi']}, under the healthy floor of {WEAK_LQI}; the "
                "measured retries happen exactly on links like this one."
            )
        elif snapshot:
            sentences.append(
                "The stored network map shows no weak link on the coordinator's "
                "own hops, so the retries likely track interference or load "
                "rather than a fixed placement problem."
            )
        else:
            sentences.append(
                "No topology snapshot is stored for this coordinator; pull one "
                "from the Topology view to name likely links."
            )
        if relays:
            top = relays[0]
            sentences.append(
                f"Beyond the coordinator's own hop, {top['device']} relays "
                f"{top['routes_via']} known routes over a link reading "
                f"{top['weakest_lqi']}; retries there are invisible to this "
                "counter but cost the same air."
            )
        sentences.append(
            f"The measured rate adds about {overhead_us:.0f} µs/s of retry "
            "airtime over a clean hop. Radio weather varies; treat this as a "
            "place to look, not a verdict."
        )

        suspects = list(
            dict.fromkeys(
                [item["device"] for item in coordinator_weak]
                + [item["device"] for item in relays]
            )
        )
        findings.append(
            Finding(
                detector=NAME,
                instance=base,
                subject=base,
                finding=" ".join(sentences),
                action={
                    "kind": "investigate",
                    "instance": base,
                    "retry_rate": round(retry_rate, 4),
                    "suspects": suspects,
                },
                saving={
                    "us_per_s": round(overhead_us, 3),
                    "pct_of_budget": round(
                        overhead_us / CHANNEL_BUDGET_US_PER_S * 100.0, 4
                    ),
                    "basis": (
                        "recorded unicast spend times the measured per-hop retry "
                        "rate; an upper bound on what cleaner links could recover"
                    ),
                    "provenance": "modeled",
                },
                confidence="low",
                evidence=[
                    {
                        "kind": "retry_rate",
                        "instance": base,
                        "rate": round(retry_rate, 4),
                        "source": "coordinator mac_tx_unicast counters, EWMA",
                    },
                    {
                        "kind": "weak_links",
                        "coordinator_hop": coordinator_weak,
                        "heavy_relays": relays,
                        "snapshot_present": snapshot,
                        "weak_lqi_floor": WEAK_LQI,
                    },
                ],
                fingerprint={
                    "retry_rate": round(retry_rate, 3),
                    "overhead_us_per_s": round(overhead_us, 1),
                    "suspects": ",".join(suspects),
                },
            )
        )
    return findings
