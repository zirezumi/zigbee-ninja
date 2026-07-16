"""Grant-gated Z2M networkmap pulls → stored topology snapshots (DESIGN.md §6, §10).

A pull publishes `<base>/bridge/request/networkmap {"type": "raw", "routes":
true}` and awaits the response topic. Zigbee2MQTT services it by sweeping the
mesh with Mgmt_Lqi/Mgmt_Rtg requests — real mesh traffic, which is why pulls
sit behind an explicit per-instance grant tile, a per-instance rate limit, and
a one-scan-at-a-time global gate. The publish rides the collector's own broker
connection, so it self-attributes (P4).

Snapshots keep the full raw map (for the graph view) plus a computed summary:
router census, weak links, and per-node degree — the beginnings of the §10
relay-load and hop-expansion inputs.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from ..store.db import Database

MIN_PULL_INTERVAL_SECONDS = 900.0
PULL_TIMEOUT_SECONDS = 180.0
WEAK_LINK_LQI = 80
SNAPSHOTS_KEPT_PER_INSTANCE = 20


class PullRejected(RuntimeError):
    """Pull refused before any mesh traffic was generated."""


def summarize(value: dict) -> dict:
    """Reduce a raw Z2M networkmap to the summary the API and views serve."""
    nodes = value.get("nodes") or []
    links = value.get("links") or []

    def node_name(node: dict) -> str:
        return str(node.get("friendlyName") or node.get("ieeeAddr") or "?")

    names = {
        str(node.get("ieeeAddr")): node_name(node) for node in nodes if node.get("ieeeAddr")
    }
    by_type: dict[str, int] = {}
    failures: list[dict] = []
    for node in nodes:
        kind = str(node.get("type") or "Unknown")
        by_type[kind] = by_type.get(kind, 0) + 1
        if node.get("failed"):
            failures.append(
                {"node": node_name(node), "failed": [str(item) for item in node["failed"]]}
            )

    degrees: dict[str, int] = {}
    weak_links: list[dict] = []
    for link in links:
        source = str(link.get("sourceIeeeAddr") or (link.get("source") or {}).get("ieeeAddr"))
        target = str(link.get("targetIeeeAddr") or (link.get("target") or {}).get("ieeeAddr"))
        lqi = link.get("lqi", link.get("linkquality"))
        for end in (source, target):
            if end in names:
                degrees[end] = degrees.get(end, 0) + 1
        if isinstance(lqi, (int, float)) and lqi < WEAK_LINK_LQI:
            weak_links.append(
                {
                    "source": names.get(source, source),
                    "target": names.get(target, target),
                    "lqi": lqi,
                }
            )
    weak_links.sort(key=lambda row: row["lqi"])

    return {
        "node_count": len(nodes),
        "link_count": len(links),
        "by_type": by_type,
        # A node that answered the LQI query but not e.g. Mgmt_Rtg is present
        # and healthy — several router firmwares simply omit the routing-table
        # ZDO endpoint (live example: Third Reality 3RSP02028BZ plugs). Only a
        # node that failed the LQI query itself counts as possibly unreachable.
        "failed_nodes": [failure["node"] for failure in failures],
        "query_failures": failures,
        "unresponsive_nodes": [
            failure["node"] for failure in failures if "lqi" in failure["failed"]
        ],
        "weak_links": weak_links[:15],
        "top_degree": sorted(
            ({"node": names[ieee], "links": count} for ieee, count in degrees.items()),
            key=lambda row: -row["links"],
        )[:10],
    }


def graph(value: dict) -> dict:
    """Reduce a raw Z2M networkmap to the render-ready graph the view draws.

    A raw link is one neighbor-table row: `target` heard `source` at `lqi`,
    so most pairs appear twice (once per direction) with asymmetric LQIs —
    the graph keeps one edge per pair carrying the *worst* of the two, the
    same pessimistic reading the weak-links table uses. A link's `routes`
    are the reporting node's routing-table rows via that neighbor; every
    ACTIVE row names the next hop that relays for it, so counting ACTIVE
    rows per next-hop node measures how many known paths flow through it
    (`routes_via` — the relay-load input for node sizing).
    """
    nodes = value.get("nodes") or []
    links = value.get("links") or []

    by_nwk: dict[int, str] = {}
    out_nodes: dict[str, dict] = {}
    for node in nodes:
        ieee = str(node.get("ieeeAddr") or "")
        if not ieee:
            continue
        nwk = node.get("networkAddress")
        if isinstance(nwk, int):
            by_nwk[nwk] = ieee
        out_nodes[ieee] = {
            "id": ieee,
            "name": str(node.get("friendlyName") or ieee),
            "type": str(node.get("type") or "Unknown"),
            "failed": bool(node.get("failed")),
            "degree": 0,
            "routes_via": 0,
        }

    edges: dict[tuple[str, str], dict] = {}
    for link in links:
        source = str(
            link.get("sourceIeeeAddr") or (link.get("source") or {}).get("ieeeAddr") or ""
        )
        target = str(
            link.get("targetIeeeAddr") or (link.get("target") or {}).get("ieeeAddr") or ""
        )
        for route in link.get("routes") or []:
            if str(route.get("status") or "").upper() != "ACTIVE":
                continue
            hop_ieee = by_nwk.get(route.get("nextHopAddress"))
            if hop_ieee in out_nodes:
                out_nodes[hop_ieee]["routes_via"] += 1
        if source not in out_nodes or target not in out_nodes or source == target:
            continue
        lqi = link.get("lqi", link.get("linkquality"))
        lqi = int(lqi) if isinstance(lqi, (int, float)) else None
        key = (min(source, target), max(source, target))
        entry = edges.get(key)
        if entry is None:
            edges[key] = {"source": key[0], "target": key[1], "lqi": lqi}
            out_nodes[key[0]]["degree"] += 1
            out_nodes[key[1]]["degree"] += 1
        elif lqi is not None and (entry["lqi"] is None or lqi < entry["lqi"]):
            entry["lqi"] = lqi

    return {"nodes": list(out_nodes.values()), "links": list(edges.values())}


class TopologyPuller:
    def __init__(
        self,
        db: Database,
        publisher: Callable[[str, str], Awaitable[None]],
        granted: Callable[[str], bool],
        clock: Callable[[], float] = time.time,
        timeout: float = PULL_TIMEOUT_SECONDS,
        min_interval: float = MIN_PULL_INTERVAL_SECONDS,
        snapshots_kept: Callable[[], int] | None = None,
    ):
        self._db = db
        self._publish = publisher
        self._granted = granted
        self._clock = clock
        self._timeout = timeout
        self._min_interval = min_interval
        self._snapshots_kept = snapshots_kept or (lambda: SNAPSHOTS_KEPT_PER_INSTANCE)
        self._pending: dict[str, asyncio.Future] = {}
        self._scanning: str | None = None

    # -- intake (engine routes bridge/response/networkmap here) -----------------

    def on_response(self, base: str, payload: bytes) -> None:
        future = self._pending.get(base)
        if future is None or future.done():
            return
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        future.set_result(data)

    # -- pull --------------------------------------------------------------------

    def last_pulled_at(self, base: str) -> float | None:
        row = self._db.connect().execute(
            "SELECT MAX(pulled_at) AS at FROM topology_snapshots WHERE instance = ?",
            (base,),
        ).fetchone()
        return row["at"] if row and row["at"] is not None else None

    async def pull(self, base: str) -> dict:
        if not self._granted(base):
            raise PullRejected("Topology pulls are not granted for this instance")
        if self._scanning is not None:
            raise PullRejected(f"A scan of {self._scanning} is already in flight")
        last = self.last_pulled_at(base)
        now = self._clock()
        if last is not None and now - last < self._min_interval:
            wait = int(self._min_interval - (now - last))
            raise PullRejected(f"Rate limited — next pull allowed in {wait}s")

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[base] = future
        self._scanning = base
        try:
            await self._publish(
                f"{base}/bridge/request/networkmap",
                json.dumps({"type": "raw", "routes": True}),
            )
            response = await asyncio.wait_for(future, self._timeout)
        finally:
            self._pending.pop(base, None)
            self._scanning = None

        if response.get("status") != "ok":
            raise PullRejected(str(response.get("error") or "networkmap request rejected"))
        value = (response.get("data") or {}).get("value") or {}
        summary = summarize(value)
        pulled_at = self._clock()
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO topology_snapshots (instance, pulled_at, node_count, link_count, "
            "summary, raw) VALUES (?, ?, ?, ?, ?, ?)",
            (
                base,
                pulled_at,
                summary["node_count"],
                summary["link_count"],
                json.dumps(summary),
                json.dumps(value),
            ),
        )
        conn.execute(
            "DELETE FROM topology_snapshots WHERE instance = ? AND id NOT IN "
            "(SELECT id FROM topology_snapshots WHERE instance = ? "
            "ORDER BY pulled_at DESC LIMIT ?)",
            (base, base, max(1, int(self._snapshots_kept()))),
        )
        conn.commit()
        return {"instance": base, "pulled_at": pulled_at, **summary}

    # -- read side -----------------------------------------------------------------

    def latest(self, base: str | None = None, include_raw: bool = False) -> dict:
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT instance, pulled_at, node_count, link_count, summary, raw "
            "FROM topology_snapshots WHERE id IN ("
            "SELECT MAX(id) FROM topology_snapshots GROUP BY instance)"
            + (" AND instance = ?" if base else ""),
            (base,) if base else (),
        ).fetchall()
        result = {}
        for row in rows:
            summary = json.loads(row["summary"])
            if "query_failures" not in summary:
                # Snapshot predates the failure-detail split — recompute from
                # the retained raw map instead of serving the stale shape.
                summary = summarize(json.loads(row["raw"]))
            entry = {
                "pulled_at": row["pulled_at"],
                "node_count": row["node_count"],
                "link_count": row["link_count"],
                **summary,
            }
            if include_raw:
                entry["raw"] = json.loads(row["raw"])
            result[row["instance"]] = entry
        return result
