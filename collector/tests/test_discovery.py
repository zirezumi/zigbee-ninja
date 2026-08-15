"""HA MQTT-discovery publisher: configs, states, revoke cleanup (DESIGN.md §14)."""

import asyncio
import json

import pytest

from zigbee_ninja.ha_discovery import DiscoveryPublisher
from zigbee_ninja.store.config import ConfigStore
from zigbee_ninja.store.db import Database


class Harness:
    def __init__(self, tmp_path):
        self.db = Database(tmp_path)
        self.config = ConfigStore(self.db)
        self.granted: list[str] = []
        self.metrics: dict[str, dict] = {}
        self.prefix: str | None = "homeassistant"
        self.published: list[tuple[str, str, bool]] = []
        self.fail_publishes = False
        self.publisher = DiscoveryPublisher(
            self.config,
            publish=self._publish,
            granted_bases=lambda: list(self.granted),
            discovery_prefix=lambda base: self.prefix,
            metrics=lambda base: dict(self.metrics.get(base, {})),
            version="test-1",
        )

    async def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if self.fail_publishes:
            raise RuntimeError("MQTT broker is not connected")
        self.published.append((topic, payload, retain))

    def cycle(self) -> None:
        asyncio.run(self.publisher.publish_cycle())

    def topics(self) -> list[str]:
        return [topic for topic, _, _ in self.published]


@pytest.fixture()
def harness(tmp_path):
    h = Harness(tmp_path)
    h.granted = ["z2m-1"]
    h.metrics["z2m-1"] = {
        "budget_pct": 0.73,
        "knee_utilization_pct": 3.2,
        "wire_p95_ms": 81.5,
        "msg_rate": 5.4,
        "recommendations_open": 2,
        "alerts": [],
        "severity": None,
    }
    return h


def test_the_discovery_cycle_writes_nothing_on_the_loop_thread(harness):
    """publish_cycle runs as an event-loop task, and both of its config
    mutations go through Database.write().

    A write issued on the loop thread waits on the WAL lock behind a 5 s
    busy_timeout, and one that raises leaves that connection holding a stale
    snapshot for the life of the process: the 2026-07-28/29 wedge, 29 hours of
    silently dropped writes (DESIGN.md §12).

    This stayed invisible for so long because both writes are RARE. The set
    only fires when a tile's topic set actually changes and the delete only on
    a revoke, so `loop_thread_writes` reads 0 on a steady fleet whether the bug
    is present or not. The assertions below are therefore paired with a check
    that each write really happened: a version of this test that never
    triggered them would pass just as happily against the broken code."""
    # asyncio.run drives the loop on THIS thread, so this is the loop thread.
    harness.db.mark_loop_thread()

    # 1. First cycle for a newly granted tile: records the topic set (the set).
    harness.db.loop_thread_writes = 0
    harness.cycle()
    assert harness.config.get("discovery_topics:z2m-1"), "the set never fired"
    assert harness.db.loop_thread_writes == 0, (
        f"discovery wrote on the loop thread: {harness.db.last_loop_thread_write}"
    )

    # 2. Revoke the grant: cleanup deletes the record (the delete).
    harness.granted = []
    harness.db.loop_thread_writes = 0
    harness.cycle()
    assert harness.config.get("discovery_topics:z2m-1") is None, (
        "the delete never fired"
    )
    assert harness.db.loop_thread_writes == 0, (
        f"revoke cleanup wrote on the loop thread: {harness.db.last_loop_thread_write}"
    )


def test_the_loop_thread_guard_sees_a_discovery_write(harness):
    """The companion that stops the test above from being vacuous.

    It proves the counter is armed on this harness and that a config mutation
    made inline on the loop thread really is counted, so a green run of the
    test above means the writes moved rather than meaning the guard was never
    watching."""
    harness.db.mark_loop_thread()
    harness.db.loop_thread_writes = 0
    harness.config.set("discovery_topics:probe", ["x"])
    assert harness.db.loop_thread_writes == 1
    assert harness.db.last_loop_thread_write


def test_cycle_publishes_retained_configs_and_states(harness):
    harness.cycle()

    configs = [
        (topic, payload)
        for topic, payload, retain in harness.published
        if topic.endswith("/config")
    ]
    assert {topic for topic, _ in configs} == {
        "homeassistant/sensor/zigbee_ninja_z2m-1/budget_pct/config",
        "homeassistant/sensor/zigbee_ninja_z2m-1/knee_utilization_pct/config",
        "homeassistant/sensor/zigbee_ninja_z2m-1/wire_p95_ms/config",
        "homeassistant/sensor/zigbee_ninja_z2m-1/msg_rate/config",
        "homeassistant/sensor/zigbee_ninja_z2m-1/recommendations_open/config",
        "homeassistant/binary_sensor/zigbee_ninja_z2m-1/alert_active/config",
    }
    assert all(retain for _, _, retain in harness.published)

    recommendations_config = json.loads(dict(configs)[
        "homeassistant/sensor/zigbee_ninja_z2m-1/recommendations_open/config"
    ])
    assert "unit_of_measurement" not in recommendations_config  # a bare count

    budget_config = json.loads(dict(configs)[
        "homeassistant/sensor/zigbee_ninja_z2m-1/budget_pct/config"
    ])
    assert budget_config["state_topic"] == "zigbee-ninja/z2m-1/budget_pct"
    assert budget_config["device"]["identifiers"] == ["zigbee_ninja_z2m-1"]
    assert budget_config["expire_after"] == 180
    assert budget_config["origin"]["name"] == "zigbee-ninja"

    states = {topic: payload for topic, payload, _ in harness.published}
    assert states["zigbee-ninja/z2m-1/budget_pct"] == "0.73"
    assert states["zigbee-ninja/z2m-1/msg_rate"] == "5.4"
    assert states["zigbee-ninja/z2m-1/recommendations_open"] == "2"
    assert states["zigbee-ninja/z2m-1/alert_active"] == "OFF"
    attributes = json.loads(states["zigbee-ninja/z2m-1/alert_attributes"])
    assert attributes == {"alerts": [], "count": 0, "severity": None}


def test_configs_publish_once_states_every_cycle(harness):
    harness.cycle()
    first = len(harness.published)
    harness.cycle()
    second_batch = harness.published[first:]
    assert all(not topic.endswith("/config") for topic, _, _ in second_batch)
    assert any(topic == "zigbee-ninja/z2m-1/budget_pct" for topic, _, _ in second_batch)


def test_missing_metrics_skip_their_state_topic(harness):
    harness.metrics["z2m-1"]["knee_utilization_pct"] = None
    harness.cycle()
    assert "zigbee-ninja/z2m-1/knee_utilization_pct" not in harness.topics()
    # ...but the topic is still recorded for revoke cleanup.
    recorded = harness.config.get("discovery_topics:z2m-1")
    assert "zigbee-ninja/z2m-1/knee_utilization_pct" in recorded


def test_alert_state_reflects_active_alerts(harness):
    harness.metrics["z2m-1"]["alerts"] = ["Wire-tap agent disconnected"]
    harness.metrics["z2m-1"]["severity"] = "warning"
    harness.cycle()
    states = {topic: payload for topic, payload, _ in harness.published}
    assert states["zigbee-ninja/z2m-1/alert_active"] == "ON"
    attributes = json.loads(states["zigbee-ninja/z2m-1/alert_attributes"])
    assert attributes["alerts"] == ["Wire-tap agent disconnected"]
    assert attributes["severity"] == "warning"


def test_revoke_cleanup_deletes_every_claimed_topic(harness):
    harness.cycle()
    recorded = set(harness.config.get("discovery_topics:z2m-1"))
    harness.published.clear()

    asyncio.run(harness.publisher.revoke_cleanup("z2m-1"))
    deletions = {(topic, payload, retain) for topic, payload, retain in harness.published}
    assert {topic for topic, _, _ in deletions} == recorded
    assert all(payload == "" and retain for _, payload, retain in deletions)
    assert harness.config.get("discovery_topics:z2m-1") is None


def test_cycle_sweep_cleans_up_revoked_base(harness):
    harness.cycle()
    harness.granted = []  # grant dropped while broker was unavailable
    harness.published.clear()

    harness.cycle()
    assert all(payload == "" for _, payload, _ in harness.published)
    assert harness.config.get("discovery_topics:z2m-1") is None

    harness.published.clear()
    harness.cycle()
    assert harness.published == []  # nothing granted, nothing to clean


def test_failed_cleanup_keeps_bookkeeping_for_retry(harness):
    harness.cycle()
    harness.granted = []
    harness.published.clear()
    harness.fail_publishes = True

    with pytest.raises(RuntimeError):
        asyncio.run(harness.publisher.publish_cycle())
    assert harness.config.get("discovery_topics:z2m-1") is not None

    harness.fail_publishes = False
    harness.cycle()
    assert harness.config.get("discovery_topics:z2m-1") is None


def test_default_prefix_when_instance_has_none(harness):
    harness.prefix = None
    harness.cycle()
    assert any(
        topic.startswith("homeassistant/sensor/") for topic in harness.topics()
    )


def test_custom_prefix_honored(harness):
    harness.prefix = "custom-ha"
    harness.cycle()
    assert any(topic.startswith("custom-ha/sensor/") for topic in harness.topics())
