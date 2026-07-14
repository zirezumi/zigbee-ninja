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


def _json_or_none(payload: bytes):
    try:
        return json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None


class Registry:
    def __init__(self):
        self._instances: dict[str, dict] = {}
        self._devices: dict[str, list[dict]] = {}
        self._groups: dict[str, list[dict]] = {}

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
        serial = (data.get("config") or {}).get("serial") or {}
        instance = self._instance(base)
        instance.update(
            version=data.get("version"),
            channel=network.get("channel"),
            pan_id=network.get("panID"),
            adapter_port=serial.get("port"),
            coordinator_type=coordinator.get("type"),
            coordinator_ieee=meta.get("ieee_address") or coordinator.get("ieee_address"),
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
            devices.append(
                {
                    "ieee_address": entry.get("ieee_address"),
                    "friendly_name": entry.get("friendly_name"),
                    "type": device_type,
                    "power_source": entry.get("power_source"),
                    "vendor": definition.get("vendor"),
                    "model": definition.get("model"),
                }
            )
        self._devices[base] = devices
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
            groups.append(
                {
                    "id": entry.get("id"),
                    "friendly_name": entry.get("friendly_name"),
                    "member_count": len(entry.get("members") or []),
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
