"""T1 probe ingest: parse extension telemetry, track latency and probe health.

Latency here is the Z2M-boundary command→device-response time: both timestamps
come from the probe's own clock, so no cross-host alignment is needed. This is
the queue+radio round trip (DESIGN.md §10 latency SLIs), a tier above the T0
command→state-echo measure.
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

    def on_device_message(self, instance: str, name: str, probe_ts: float) -> None:
        self._latest_ts[instance] = max(self._latest_ts.get(instance, 0.0), probe_ts)
        pending = self._pending.get((instance, name))
        if not pending:
            return
        while pending:
            opened = pending[0]
            if probe_ts - opened > MATCH_WINDOW_SECONDS:
                pending.popleft()
                continue
            pending.popleft()
            samples = self._samples.setdefault(instance, deque(maxlen=MAX_SAMPLES))
            samples.append((probe_ts, (probe_ts - opened) * 1000.0))
            return

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
    ):
        self._clock = clock
        self._on_heartbeat = on_heartbeat or (lambda _base, _hb: None)
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
            elif kind == "dm" and len(event) >= 3 and isinstance(event[2], str):
                self.latency.on_device_message(base, event[2], float(ts))

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
