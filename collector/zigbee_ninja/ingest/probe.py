"""T1 probe ingest: parse extension telemetry, track latency and probe health.

Latency here is the Z2M-boundary command→state-echo time (both timestamps from
the probe's own clock, no cross-host alignment). It is an APPROXIMATE proxy: a
live trace on this system showed presence dimmers also emit frequent autonomous
reports (illuminance, occupancy, mmWave, OTA) that would mispair with commands
and inflate the figure toward the match-window ceiling. Two guards keep it
honest: pairing is restricted to actuator state-echo clusters (so a sensor/OTA
report is never mistaken for a command response), and a report pairs with the
NEWEST pending command, not the oldest. The authoritative, unambiguous latency
SLI is the wire tier's sendUnicast→messageSentHandler pairing (DESIGN.md §10),
which supersedes this proxy once T2 is live.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import deque
from collections.abc import Callable

from ..attribution.chains import parse_command

EVENTS_SUFFIX = "zigbee-ninja/probe/events"
HEARTBEAT_SUFFIX = "zigbee-ninja/probe/heartbeat"

MATCH_WINDOW_SECONDS = 3.0
SAMPLE_WINDOW_SECONDS = 300.0
MAX_SAMPLES = 1000

# Clusters whose reports are a plausible echo of a light/actuator command. A
# report on any other cluster (sensors, metering, OTA, vendor mmWave) is
# autonomous and must never be paired as a command response. Vendor color/state
# clusters that carry bulb state echoes are included by prefix (manuSpecific*
# is matched separately below to catch Hue/Inovelli state reports).
ACTUATOR_CLUSTERS = frozenset(
    {"genOnOff", "genLevelCtrl", "lightingColorCtrl", "genScenes"}
)
_AUTONOMOUS_MANU_HINTS = ("mmwave", "occup", "ota")


def is_state_echo_cluster(cluster: str) -> bool:
    if cluster in ACTUATOR_CLUSTERS:
        return True
    # Vendor state echoes (e.g. manuSpecificPhilips2 for Hue) count, but vendor
    # sensor/mmWave clusters do not.
    low = cluster.lower()
    if low.startswith("manuspecific"):
        return not any(hint in low for hint in _AUTONOMOUS_MANU_HINTS)
    return False


class LatencyTracker:
    def __init__(self, resolve_members: Callable[[str, str], list[str]] | None = None):
        self._resolve_members = resolve_members or (lambda _instance, _target: [])
        self._pending: dict[tuple[str, str], deque[float]] = {}
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._latest_ts: dict[str, float] = {}

    def on_command(self, instance: str, target: str, probe_ts: float) -> None:
        self._latest_ts[instance] = max(self._latest_ts.get(instance, 0.0), probe_ts)
        self._pending.setdefault((instance, target), deque(maxlen=8)).append(probe_ts)
        for member in self._resolve_members(instance, target):
            self._pending.setdefault((instance, member), deque(maxlen=8)).append(probe_ts)

    def on_device_message(
        self, instance: str, name: str, cluster: str, probe_ts: float
    ) -> None:
        self._latest_ts[instance] = max(self._latest_ts.get(instance, 0.0), probe_ts)
        if not is_state_echo_cluster(cluster):
            return  # autonomous report: never a command response
        pending = self._pending.get((instance, name))
        if not pending:
            return
        while pending and probe_ts - pending[0] > MATCH_WINDOW_SECONDS:
            pending.popleft()  # drop stale commands
        # Newest command at or before this report: the one it most plausibly
        # answers. Consume it and everything older so no command double-counts.
        chosen: float | None = None
        while pending and pending[0] <= probe_ts:
            chosen = pending.popleft()
        if chosen is not None:
            samples = self._samples.setdefault(instance, deque(maxlen=MAX_SAMPLES))
            samples.append((probe_ts, (probe_ts - chosen) * 1000.0))

    def snapshot(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for instance, samples in self._samples.items():
            horizon = self._latest_ts.get(instance, 0.0) - SAMPLE_WINDOW_SECONDS
            values = [ms for ts, ms in samples if ts >= horizon]
            if not values:
                continue
            values.sort()
            result[instance] = {
                "count": len(values),
                "p50_ms": round(statistics.median(values), 1),
                "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 1),
            }
        return result


class ProbeIngest:
    def __init__(
        self,
        resolve_members: Callable[[str, str], list[str]] | None = None,
        on_heartbeat: Callable[[str, dict], None] | None = None,
        clock: Callable[[], float] = time.time,
        on_device_seq: Callable[[str, str, int, float], None] | None = None,
    ):
        self._clock = clock
        self._on_heartbeat = on_heartbeat or (lambda _base, _hb: None)
        # on_device_seq(base, device_name, zcl_seq, probe_ts): deviceMessage
        # events that carry a ZCL transaction sequence (probe v0.4+): the T1
        # side of frame fusion (DESIGN.md §8).
        self._on_device_seq = on_device_seq or (lambda _base, _name, _seq, _ts: None)
        self.latency = LatencyTracker(resolve_members)
        self._stats: dict[str, dict] = {}

    def handle(self, base: str, suffix: str, payload: bytes) -> bool:
        if suffix == EVENTS_SUFFIX:
            self._on_events(base, payload)
            return True
        if suffix == HEARTBEAT_SUFFIX:
            self._handle_heartbeat(base, payload)
            return True
        return False

    def _parse(self, payload: bytes) -> dict | None:
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _stat(self, base: str) -> dict:
        return self._stats.setdefault(
            base,
            {
                "last_heartbeat_at": None,
                "last_events_at": None,
                "version": None,
                "enabled": None,
                "hooks": [],
                "counters": {},
                "seq": None,
                "seq_gaps": 0,
                "parse_errors": 0,
            },
        )

    def _on_events(self, base: str, payload: bytes) -> None:
        stat = self._stat(base)
        data = self._parse(payload)
        if data is None or not isinstance(data.get("events"), list):
            stat["parse_errors"] += 1
            return
        stat["last_events_at"] = self._clock()
        seq = data.get("seq")
        if isinstance(seq, int):
            last = stat["seq"]
            if isinstance(last, int) and seq > last + 1:
                stat["seq_gaps"] += seq - last - 1
            stat["seq"] = seq

        for event in data["events"]:
            if not isinstance(event, list) or len(event) < 2:
                continue
            ts, kind = event[0], event[1]
            if not isinstance(ts, (int, float)):
                continue
            if kind == "mi" and len(event) >= 3 and isinstance(event[2], str):
                topic = event[2]
                if topic.startswith(base + "/"):
                    command = parse_command(topic[len(base) + 1 :])
                    if command is not None:
                        self.latency.on_command(base, command[0], float(ts))
            elif kind == "dm" and len(event) >= 4 and isinstance(event[2], str):
                # dm event: [ts, "dm", name, cluster, type, lqi, size] with
                # probe v0.4 appending [zcl_seq, endpoint].
                cluster = event[3] if isinstance(event[3], str) else ""
                self.latency.on_device_message(base, event[2], cluster, float(ts))
                if len(event) >= 8 and isinstance(event[7], (int, float)) and event[7] >= 0:
                    self._on_device_seq(base, event[2], int(event[7]), float(ts))

    def _handle_heartbeat(self, base: str, payload: bytes) -> None:
        stat = self._stat(base)
        data = self._parse(payload)
        if data is None:
            stat["parse_errors"] += 1
            return
        stat["last_heartbeat_at"] = self._clock()
        stat["version"] = data.get("version")
        stat["enabled"] = data.get("enabled")
        if isinstance(data.get("hooks"), list):
            stat["hooks"] = data["hooks"]
        if isinstance(data.get("counters"), dict):
            stat["counters"] = data["counters"]
        self._on_heartbeat(base, data)

    def stats(self) -> dict[str, dict]:
        return {base: dict(stat) for base, stat in self._stats.items()}
