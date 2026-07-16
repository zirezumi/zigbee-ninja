import asyncio
import json

import pytest

from zigbee_ninja.ingest.topology import PullRejected, TopologyPuller, graph, summarize
from zigbee_ninja.store.db import Database

# Synthetic map (sanitized fixtures only: no real IEEE addresses).
RAW_MAP = {
    "nodes": [
        {"ieeeAddr": "0x01", "friendlyName": "coordinator", "type": "Coordinator"},
        {"ieeeAddr": "0x02", "friendlyName": "lamp-a", "type": "Router"},
        # Answered LQI but omitted the routing table: present and healthy,
        # the firmware just lacks the Mgmt_Rtg endpoint (3RSP02028BZ pattern).
        {"ieeeAddr": "0x03", "friendlyName": "lamp-b", "type": "Router",
         "failed": ["routingTable"]},
        {"ieeeAddr": "0x04", "friendlyName": "button", "type": "EndDevice", "failed": ["lqi"]},
    ],
    "links": [
        {"sourceIeeeAddr": "0x02", "targetIeeeAddr": "0x01", "lqi": 210},
        {"sourceIeeeAddr": "0x03", "targetIeeeAddr": "0x01", "lqi": 64},
        {"sourceIeeeAddr": "0x03", "targetIeeeAddr": "0x02", "linkquality": 30},
        {"sourceIeeeAddr": "0x04", "targetIeeeAddr": "0x02", "lqi": 180},
    ],
}


def test_summarize_counts_weak_links_and_degrees():
    summary = summarize(RAW_MAP)
    assert summary["node_count"] == 4
    assert summary["link_count"] == 4
    assert summary["by_type"] == {"Coordinator": 1, "Router": 2, "EndDevice": 1}
    assert summary["failed_nodes"] == ["lamp-b", "button"]
    assert summary["query_failures"] == [
        {"node": "lamp-b", "failed": ["routingTable"]},
        {"node": "button", "failed": ["lqi"]},
    ]
    # Only the LQI no-show counts as possibly unreachable; a missing routing
    # table alone is a firmware omission, not absence.
    assert summary["unresponsive_nodes"] == ["button"]
    # Weak links sorted ascending, resolving names, reading lqi or linkquality.
    assert summary["weak_links"] == [
        {"source": "lamp-b", "target": "lamp-a", "lqi": 30},
        {"source": "lamp-b", "target": "coordinator", "lqi": 64},
    ]
    degrees = {row["node"]: row["links"] for row in summary["top_degree"]}
    assert degrees == {"coordinator": 2, "lamp-a": 3, "lamp-b": 2, "button": 1}


def test_graph_dedupes_directions_and_counts_active_routes():
    raw = {
        "nodes": [
            {"ieeeAddr": "0x01", "friendlyName": "coordinator", "type": "Coordinator",
             "networkAddress": 0},
            {"ieeeAddr": "0x02", "friendlyName": "lamp-a", "type": "Router",
             "networkAddress": 100},
            {"ieeeAddr": "0x03", "friendlyName": "lamp-b", "type": "Router",
             "networkAddress": 200, "failed": ["routingTable"]},
        ],
        "links": [
            # The same physical pair reported from both ends with asymmetric
            # LQIs: one edge survives, carrying the worse reading.
            {"sourceIeeeAddr": "0x02", "targetIeeeAddr": "0x01", "lqi": 210},
            {"source": {"ieeeAddr": "0x01"}, "target": {"ieeeAddr": "0x02"},
             "linkquality": 150},
            # ACTIVE routing rows via lamp-a; the INACTIVE row must not count.
            {"sourceIeeeAddr": "0x03", "targetIeeeAddr": "0x01", "lqi": 90,
             "routes": [
                 {"destinationAddress": 200, "nextHopAddress": 100, "status": "ACTIVE"},
                 {"destinationAddress": 0, "nextHopAddress": 0, "status": "INACTIVE"},
             ]},
            # A link to an unknown node contributes its ACTIVE route but no edge.
            {"sourceIeeeAddr": "0x09", "targetIeeeAddr": "0x01", "lqi": 40,
             "routes": [
                 {"destinationAddress": 300, "nextHopAddress": 100, "status": "ACTIVE"},
             ]},
        ],
    }
    view = graph(raw)
    nodes = {node["id"]: node for node in view["nodes"]}
    assert set(nodes) == {"0x01", "0x02", "0x03"}
    assert nodes["0x02"]["routes_via"] == 2  # both ACTIVE rows point through lamp-a
    assert nodes["0x03"]["failed"] is True
    assert nodes["0x01"]["degree"] == 2 and nodes["0x02"]["degree"] == 1

    links = {(link["source"], link["target"]): link for link in view["links"]}
    assert set(links) == {("0x01", "0x02"), ("0x01", "0x03")}
    assert links[("0x01", "0x02")]["lqi"] == 150  # worst of 210/150
    assert links[("0x01", "0x03")]["lqi"] == 90


class FakePublisher:
    def __init__(self, responder=None):
        self.published: list[tuple[str, str]] = []
        self._responder = responder

    async def __call__(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))
        if self._responder is not None:
            self._responder(topic, payload)


def make_puller(tmp_path, granted=True, responder=None, clock=None):
    db = Database(tmp_path)
    publisher = FakePublisher(responder)
    puller = TopologyPuller(
        db,
        publisher=publisher,
        granted=lambda _base: granted,
        clock=clock or (lambda: 1000.0),
        timeout=0.5,
        min_interval=900.0,
    )
    return puller, publisher


def test_pull_stores_snapshot_and_rate_limits(tmp_path):
    puller_holder = {}

    def responder(topic: str, _payload: str) -> None:
        assert topic == "z2m-test/bridge/request/networkmap"
        puller_holder["puller"].on_response(
            "z2m-test",
            json.dumps({"status": "ok", "data": {"value": RAW_MAP}}).encode(),
        )

    puller, publisher = make_puller(tmp_path, responder=responder)
    puller_holder["puller"] = puller

    result = asyncio.run(puller.pull("z2m-test"))
    assert result["node_count"] == 4
    assert json.loads(publisher.published[0][1]) == {"type": "raw", "routes": True}

    latest = puller.latest()
    assert latest["z2m-test"]["weak_links"][0]["lqi"] == 30
    assert "raw" not in latest["z2m-test"]
    assert puller.latest("z2m-test", include_raw=True)["z2m-test"]["raw"] == RAW_MAP

    # Second pull inside the interval is refused before publishing anything.
    with pytest.raises(PullRejected, match="Rate limited"):
        asyncio.run(puller.pull("z2m-test"))
    assert len(publisher.published) == 1


def test_pull_requires_grant_and_handles_timeout_and_errors(tmp_path):
    puller, publisher = make_puller(tmp_path, granted=False)
    with pytest.raises(PullRejected, match="not granted"):
        asyncio.run(puller.pull("z2m-test"))
    assert publisher.published == []

    # No response → TimeoutError after the (shortened) window; nothing stored.
    puller, _ = make_puller(tmp_path / "t2", granted=True)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(puller.pull("z2m-test"))
    assert puller.latest() == {}

    # An explicit Z2M error surfaces as a rejection.
    holder = {}

    def responder(_topic: str, _payload: str) -> None:
        holder["puller"].on_response(
            "z2m-test", json.dumps({"status": "error", "error": "scan busy"}).encode()
        )

    puller, _ = make_puller(tmp_path / "t3", granted=True, responder=responder)
    holder["puller"] = puller
    with pytest.raises(PullRejected, match="scan busy"):
        asyncio.run(puller.pull("z2m-test"))
