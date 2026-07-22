"""Route depth per device, for the §10 unicast hop term.

§10 prices a unicast at `hops × (frame + ACK + IFS) × (1 + retry_rate)` and
sources hop counts from topology snapshots. This module derives them from the
reduced networkmap graph (`ingest.topology.graph`): a breadth-first search
outward from the coordinator over neighbor-table adjacency.

**What this measures, honestly.** Neighbor adjacency is who can *hear* whom,
not which path a frame actually takes, so BFS depth is the shortest route the
radio topology permits: a **lower bound** on the real hop count. It is the same
posture §10 already takes on TX PSDU reconstruction, and it errs in the safe
direction for the recommendation engine, which uses unicast as the proposed
alternative: under-counting hops under-prices unicast, so a saving computed
this way is never inflated by the hop term.

Nodes the snapshot cannot place (absent from the map, or partitioned from the
coordinator in the neighbor graph) get no entry; callers fall back to
`airtime.DEFAULT_UNKNOWN_HOPS` and tag the result accordingly.

A refinement worth taking later: raw links carry ACTIVE routing-table rows
naming their next hop, which is real path data rather than adjacency. It is
unusable as the primary source today because it is incomplete on the fleet
(some device firmware answers Mgmt_Lqi but not Mgmt_Rtg, so its rows are
simply missing), but it can sharpen depths where present.
"""

from __future__ import annotations

from collections import deque

COORDINATOR_TYPE = "Coordinator"

PROVENANCE_TOPOLOGY = "inferred (topology adjacency, shortest path)"
PROVENANCE_DEFAULT = "modeled (no topology snapshot)"


def depths_by_ieee(graph: dict, coordinator_ieee: str | None = None) -> dict[str, int]:
    """BFS hop depth from the coordinator, keyed by IEEE address.

    `coordinator_ieee` is the registry's authoritative value; when it is absent
    or not present in this graph, the search falls back to the node the map
    itself types as the coordinator. The coordinator maps to 0. An empty dict
    means no root could be established, so nothing can be placed relative to it.
    """
    nodes = graph.get("nodes") or []
    links = graph.get("links") or []

    known = {str(node.get("id")) for node in nodes if node.get("id")}
    root = coordinator_ieee if coordinator_ieee in known else None
    if root is None:
        root = next(
            (
                str(node.get("id"))
                for node in nodes
                if str(node.get("type") or "") == COORDINATOR_TYPE and node.get("id")
            ),
            None,
        )
    if root is None:
        return {}

    adjacency: dict[str, set[str]] = {}
    for link in links:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if not source or not target or source == target:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set()).add(source)

    depths = {root: 0}
    queue = deque([root])
    while queue:
        node = queue.popleft()
        for peer in adjacency.get(node, ()):
            if peer not in depths:
                depths[peer] = depths[node] + 1
                queue.append(peer)
    return depths


def depths_by_name(graph: dict, coordinator_ieee: str | None = None) -> dict[str, int]:
    """`depths_by_ieee` re-keyed by friendly name, which is what the command
    stream and the registry speak. The coordinator is dropped: it is never a
    unicast target."""
    depths = depths_by_ieee(graph, coordinator_ieee)
    if not depths:
        return {}
    by_name: dict[str, int] = {}
    for node in graph.get("nodes") or []:
        ieee = str(node.get("id") or "")
        depth = depths.get(ieee)
        if depth is None or depth == 0:
            continue
        name = str(node.get("name") or ieee)
        # A duplicate friendly name is pathological but survivable: keep the
        # nearer depth so pricing stays a lower bound.
        if name not in by_name or depth < by_name[name]:
            by_name[name] = depth
    return by_name
