"""Home Assistant MQTT-discovery publisher tile (DESIGN.md §14).

A *standing* publisher on the shared broker: unlike a per-run calibration;
so it is a per-instance grant tile: nothing publishes until the user grants
the instance in Footprint, and revoking deletes every retained topic the tile
ever claimed (empty retained payloads), keeping the footprint contract (P2).

When granted for an instance, the publisher emits retained discovery configs
(one HA device per coordinator, entities carrying an `origin` block), then
refreshes state topics on a fixed cadence: headline capacity metrics (channel
budget %, capacity utilization %, wire p95 latency, MQTT message rate) as sensors
and the active-alert state as a `problem` binary_sensor whose attributes list
the alert names. Sensors carry `expire_after`, so a dead collector reads
*unavailable* in HA rather than forever-fresh: no availability topic and no
LWT dependency. Everything rides the engine's own MQTT connection, so the
traffic self-attributes (P4).

Retained-topic bookkeeping lives in settings (`discovery_topics:<base>`): a
revoke that races a broker outage is finished later by the publish loop's
cleanup sweep, which deletes retained topics for any base that lost its grant.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable

from .store.config import ConfigStore

PUBLISH_INTERVAL_SECONDS = 45.0
EXPIRE_AFTER_SECONDS = 180
STATE_ROOT = "zigbee-ninja"
DEFAULT_PREFIX = "homeassistant"
REPO_URL = "https://github.com/zirezumi/zigbee-ninja"
_TOPICS_KEY_PREFIX = "discovery_topics:"

SENSORS = (
    {
        "key": "budget_pct",
        "name": "Channel budget used",
        "unit": "%",
        "icon": "mdi:radio-tower",
    },
    {
        "key": "knee_utilization_pct",
        "name": "Capacity utilization",
        "unit": "%",
        "icon": "mdi:speedometer",
    },
    {
        "key": "wire_p95_ms",
        "name": "Wire p95 latency",
        "unit": "ms",
        "icon": "mdi:timer-outline",
    },
    {
        "key": "msg_rate",
        "name": "MQTT message rate",
        "unit": "msg/s",
        "icon": "mdi:swap-horizontal",
    },
)

Publish = Callable[..., Awaitable[None]]  # (topic, payload, retain=False)


def _slug(base: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", base)


class DiscoveryPublisher:
    def __init__(
        self,
        config: ConfigStore,
        publish: Publish,
        granted_bases: Callable[[], list[str]],
        discovery_prefix: Callable[[str], str | None],
        metrics: Callable[[str], dict],
        version: str,
    ):
        self._config = config
        self._publish = publish
        self._granted_bases = granted_bases
        self._discovery_prefix = discovery_prefix
        self._metrics = metrics
        self._version = version
        # Config payloads republish once per boot per instance (and again on
        # payload change, e.g. a version bump): not every cycle.
        self._configs_published: dict[str, str] = {}

    # -- topic/payload construction ---------------------------------------------

    def _topics_key(self, base: str) -> str:
        return _TOPICS_KEY_PREFIX + base

    def _state_topic(self, base: str, key: str) -> str:
        return f"{STATE_ROOT}/{base}/{key}"

    def _config_payloads(self, base: str) -> dict[str, str]:
        prefix = self._discovery_prefix(base) or DEFAULT_PREFIX
        slug = _slug(base)
        device = {
            "identifiers": [f"zigbee_ninja_{slug}"],
            "name": f"zigbee-ninja {base}",
            "manufacturer": "zigbee-ninja",
            "sw_version": self._version,
        }
        origin = {"name": "zigbee-ninja", "sw": self._version, "url": REPO_URL}
        payloads: dict[str, str] = {}
        for sensor in SENSORS:
            topic = f"{prefix}/sensor/zigbee_ninja_{slug}/{sensor['key']}/config"
            payloads[topic] = json.dumps(
                {
                    "name": sensor["name"],
                    "unique_id": f"zigbee_ninja_{slug}_{sensor['key']}",
                    "state_topic": self._state_topic(base, sensor["key"]),
                    "unit_of_measurement": sensor["unit"],
                    "state_class": "measurement",
                    "icon": sensor["icon"],
                    "expire_after": EXPIRE_AFTER_SECONDS,
                    "device": device,
                    "origin": origin,
                },
                sort_keys=True,
            )
        alert_topic = f"{prefix}/binary_sensor/zigbee_ninja_{slug}/alert_active/config"
        payloads[alert_topic] = json.dumps(
            {
                "name": "Alert active",
                "unique_id": f"zigbee_ninja_{slug}_alert_active",
                "state_topic": self._state_topic(base, "alert_active"),
                "device_class": "problem",
                "expire_after": EXPIRE_AFTER_SECONDS,
                "json_attributes_topic": self._state_topic(base, "alert_attributes"),
                "device": device,
                "origin": origin,
            },
            sort_keys=True,
        )
        return payloads

    def _state_payloads(self, base: str) -> dict[str, str]:
        metrics = self._metrics(base)
        payloads: dict[str, str] = {}
        for sensor in SENSORS:
            value = metrics.get(sensor["key"])
            if value is None:
                continue  # absent topic + expire_after read as unknown, honestly
            payloads[self._state_topic(base, sensor["key"])] = str(value)
        alerts = metrics.get("alerts") or []
        payloads[self._state_topic(base, "alert_active")] = "ON" if alerts else "OFF"
        payloads[self._state_topic(base, "alert_attributes")] = json.dumps(
            {
                "alerts": alerts,
                "severity": metrics.get("severity"),
                "count": len(alerts),
            },
            sort_keys=True,
        )
        return payloads

    def _all_topics(self, base: str) -> list[str]:
        """Every retained topic this tile may ever claim for the instance:
        recorded up front so a revoke clears entities whose states were
        skipped (no data yet) in every cycle so far."""
        topics = set(self._config_payloads(base))
        topics.update(self._state_topic(base, sensor["key"]) for sensor in SENSORS)
        topics.add(self._state_topic(base, "alert_active"))
        topics.add(self._state_topic(base, "alert_attributes"))
        return sorted(topics)

    # -- publishing ---------------------------------------------------------------

    async def _publish_instance(self, base: str) -> None:
        configs = self._config_payloads(base)
        signature = json.dumps(configs, sort_keys=True)
        recorded = self._config.get(self._topics_key(base)) or []
        all_topics = self._all_topics(base)
        if recorded != all_topics:
            self._config.set(self._topics_key(base), all_topics)
        if self._configs_published.get(base) != signature:
            for topic, payload in configs.items():
                await self._publish(topic, payload, retain=True)
            self._configs_published[base] = signature
        for topic, payload in self._state_payloads(base).items():
            await self._publish(topic, payload, retain=True)

    async def cleanup_revoked(self) -> None:
        """Delete retained topics for every base that lost its grant."""
        granted = set(self._granted_bases())
        for key in list(self._config.all()):
            if not key.startswith(_TOPICS_KEY_PREFIX):
                continue
            base = key[len(_TOPICS_KEY_PREFIX) :]
            if base not in granted:
                await self.revoke_cleanup(base)

    async def revoke_cleanup(self, base: str) -> None:
        """Publish empty retained payloads for everything this tile claimed."""
        self._configs_published.pop(base, None)
        topics = self._config.get(self._topics_key(base)) or []
        for topic in topics:
            await self._publish(topic, "", retain=True)
        self._config.delete(self._topics_key(base))

    async def publish_cycle(self) -> None:
        await self.cleanup_revoked()
        for base in sorted(set(self._granted_bases())):
            await self._publish_instance(base)
