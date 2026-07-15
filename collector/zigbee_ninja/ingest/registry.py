"""Z2M instance/device/group registry built from retained bridge topics.

DESIGN.md paragraph 5: every Zigbee2MQTT instance announces itself on
`<base>/bridge/info`; devices/groups/state arrive on sibling retained topics.
Base topics may contain slashes, so parsing is suffix-anchored.
"""

from __future__ import annotations

import json
import time

_SUFFIX_HANDLERS = {
    "/bridge/info": "_on_info",
    "/bridge/devices": "_on_devices",
    "/bridge/groups": "_on_groups",
    "/bridge/state": "_on_state",
}

# Published properties whose values move on their own (metering, environment).
# Reading such a device republishes them — the §11 preview warns about the
# resulting state churn in downstream controllers.
_MEASUREMENT_HINTS = (
    "power",
    "energy",
    "current",
    "voltage",
    "temperature",
    "humidity",
    "illuminance",
    "pressure",
    "co2",
    "pm25",
)
# ...but config enums are not measurements even when they share a stem
# (power_on_behavior, power_outage_memory, current_heating_setpoint).
_MEASUREMENT_EXCLUDED_SUFFIXES = ("_behavior", "_memory", "_mode", "_type", "_setpoint")

_ACCESS_PUBLISHED = 1
_ACCESS_GETTABLE = 4


def _is_measurement(prop: str) -> bool:
    if prop.endswith(_MEASUREMENT_EXCLUDED_SUFFIXES):
        return False
    return any(
        prop == hint or prop.startswith(hint + "_") or prop.endswith("_" + hint)
        for hint in _MEASUREMENT_HINTS
    )


def _json_or_none(payload: bytes):
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None


def _walk_exposes(exposes):
    """Yield leaf expose entries, flattening composite features (light, switch…)."""
    for item in exposes or []:
        if not isinstance(item, dict):
            continue
        if item.get("features"):
            yield from _walk_exposes(item["features"])
        elif item.get("property"):
            yield item


def _expose_capabilities(definition) -> tuple[str | None, list[str]]:
    """(preferred gettable property, published measurement-ish properties).

    Access is Zigbee2MQTT's exposes bitfield: 1 published, 2 settable, 4
    gettable via `<base>/<name>/get {"<property>": ""}`.
    """
    leaves = [
        leaf
        for leaf in _walk_exposes((definition or {}).get("exposes"))
        if isinstance(leaf.get("access"), int)
    ]
    gettable = [leaf["property"] for leaf in leaves if leaf["access"] & _ACCESS_GETTABLE]
    get_attribute = "state" if "state" in gettable else (gettable[0] if gettable else None)
    measurements = sorted(
        {
            str(leaf["property"])
            for leaf in leaves
            if leaf["access"] & _ACCESS_PUBLISHED and _is_measurement(str(leaf["property"]))
        }
    )
    return get_attribute, measurements


def _binding_count(entry: dict) -> int:
    endpoints = entry.get("endpoints")
    if not isinstance(endpoints, dict):
        return 0
    return sum(
        len(endpoint.get("bindings") or [])
        for endpoint in endpoints.values()
        if isinstance(endpoint, dict)
    )


class Registry:
    def __init__(self):
        self._instances: dict[str, dict] = {}
        self._devices: dict[str, list[dict]] = {}
        self._groups: dict[str, list[dict]] = {}
        self._ieee_to_name: dict[str, dict[str, str]] = {}

    def _instance(self, base: str) -> dict:
        return self._instances.setdefault(
            base,
            {
                "base_topic": base,
                "online": None,
                "version": None,
                "channel": None,
                "pan_id": None,
                "adapter_port": None,
                "coordinator_type": None,
                "coordinator_ieee": None,
                "coordinator_revision": None,
                "discovery_prefix": None,
                "device_count": 0,
                "router_count": 0,
                "end_device_count": 0,
                "group_count": 0,
                "last_info_at": None,
            },
        )

    def handle(self, topic: str, payload: bytes) -> bool:
        """Route a bridge topic into the registry. Returns True if it was one."""
        for suffix, handler_name in _SUFFIX_HANDLERS.items():
            if topic.endswith(suffix):
                base = topic[: -len(suffix)]
                if base and "+" not in base and "#" not in base:
                    getattr(self, handler_name)(base, payload)
                    return True
        return False

    def base_for(self, topic: str) -> str | None:
        """Longest known base topic that owns this topic, if any."""
        best: str | None = None
        for base in self._instances:
            if topic.startswith(base + "/") and (best is None or len(base) > len(best)):
                best = base
        return best

    def _on_info(self, base: str, payload: bytes) -> None:
        data = _json_or_none(payload)
        if not isinstance(data, dict):
            return
        network = data.get("network") or {}
        coordinator = data.get("coordinator") or {}
        meta = coordinator.get("meta") or {}
        config = data.get("config") or {}
        serial = config.get("serial") or {}
        # The HA discovery prefix rides bridge/info (DESIGN.md §5): a dict in
        # Z2M 2.x ({enabled, discovery_topic, ...}), a bare bool historically.
        ha_config = config.get("homeassistant")
        if isinstance(ha_config, dict):
            discovery_prefix = ha_config.get("discovery_topic") or None
        elif ha_config is True:
            discovery_prefix = "homeassistant"
        else:
            discovery_prefix = None
        instance = self._instance(base)
        instance.update(
            version=data.get("version"),
            channel=network.get("channel"),
            pan_id=network.get("panID"),
            adapter_port=serial.get("port"),
            coordinator_type=coordinator.get("type"),
            coordinator_ieee=meta.get("ieee_address") or coordinator.get("ieee_address"),
            coordinator_revision=meta.get("revision"),
            discovery_prefix=discovery_prefix,
            last_info_at=time.time(),
        )

    def _on_devices(self, base: str, payload: bytes) -> None:
        data = _json_or_none(payload)
        if not isinstance(data, list):
            return
        devices = []
        routers = end_devices = 0
        for entry in data:
            if not isinstance(entry, dict):
                continue
            device_type = entry.get("type")
            if device_type == "Router":
                routers += 1
            elif device_type == "EndDevice":
                end_devices += 1
            definition = entry.get("definition") or {}
            get_attribute, measurements = _expose_capabilities(definition)
            devices.append(
                {
                    "ieee_address": entry.get("ieee_address"),
                    "friendly_name": entry.get("friendly_name"),
                    "type": device_type,
                    "power_source": entry.get("power_source"),
                    "vendor": definition.get("vendor"),
                    "model": definition.get("model"),
                    "network_address": entry.get("network_address"),
                    "get_attribute": get_attribute,
                    "published_measurements": measurements,
                    "binding_count": _binding_count(entry),
                }
            )
        self._devices[base] = devices
        self._ieee_to_name[base] = {
            device["ieee_address"]: device["friendly_name"]
            for device in devices
            if device.get("ieee_address") and device.get("friendly_name")
        }
        instance = self._instance(base)
        instance.update(
            device_count=len(devices),
            router_count=routers,
            end_device_count=end_devices,
        )

    def _on_groups(self, base: str, payload: bytes) -> None:
        data = _json_or_none(payload)
        if not isinstance(data, list):
            return
        groups = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            members = entry.get("members") or []
            groups.append(
                {
                    "id": entry.get("id"),
                    "friendly_name": entry.get("friendly_name"),
                    "member_count": len(members),
                    "member_ieee": [
                        member.get("ieee_address")
                        for member in members
                        if isinstance(member, dict) and member.get("ieee_address")
                    ],
                }
            )
        self._groups[base] = groups
        self._instance(base)["group_count"] = len(groups)

    def _on_state(self, base: str, payload: bytes) -> None:
        data = _json_or_none(payload)
        if isinstance(data, dict):
            state = data.get("state")
        else:
            state = payload.decode(errors="replace").strip()
        if state in ("online", "offline"):
            self._instance(base)["online"] = state == "online"

    def snapshot(self) -> list[dict]:
        return [dict(self._instances[base]) for base in sorted(self._instances)]

    def devices(self, base: str) -> list[dict]:
        return list(self._devices.get(base, []))

    def groups(self, base: str) -> list[dict]:
        return list(self._groups.get(base, []))

    def router_count_for(self, base: str) -> int:
        """Router census for the mesh-amplification model (0 until discovered)."""
        instance = self._instances.get(base)
        return int(instance.get("router_count") or 0) if instance else 0

    def discovery_prefix_for(self, base: str) -> str | None:
        """HA discovery prefix announced by the instance, if any."""
        instance = self._instances.get(base)
        return instance.get("discovery_prefix") if instance else None

    def instance_for_endpoint(self, ip: str, port: int) -> str | None:
        """Base topic whose coordinator adapter is at tcp://ip:port (for T2 flows)."""
        needle = f"{ip}:{port}"
        for base, instance in self._instances.items():
            adapter = instance.get("adapter_port") or ""
            if adapter.startswith("tcp://") and adapter[len("tcp://") :] == needle:
                return base
        return None

    def group_members(self, base: str, group_name: str) -> list[str]:
        """Friendly names of a group's member devices; [] for non-group targets."""
        ieee_map = self._ieee_to_name.get(base, {})
        for group in self._groups.get(base, []):
            if group.get("friendly_name") == group_name:
                return [
                    ieee_map[ieee]
                    for ieee in group.get("member_ieee", [])
                    if ieee in ieee_map
                ]
        return []
