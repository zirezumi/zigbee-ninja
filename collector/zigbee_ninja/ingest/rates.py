"""Per-instance message-rate counters: in-memory 1s buckets + 10s rollup drain.

Retention v0 (DESIGN.md paragraph 12): the fleet live view reads the 1s window;
a background task drains completed 10s windows into the series_10s table.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable

KINDS = ("command", "state", "bridge", "availability", "probe", "other")
WINDOW_SECONDS = 300
ROLLUP_SECONDS = 10

# Instance key used for whole-broker totals (includes topics matching no instance).
GLOBAL = "*"


def classify(topic: str, base_topic: str) -> str:
    """Coarse message taxonomy for a topic under a known Z2M base topic."""
    suffix = topic[len(base_topic) + 1 :]
    if suffix.startswith("zigbee-ninja/"):
        return "probe"
    if suffix.startswith("bridge/"):
        return "bridge"
    last_segment = suffix.rsplit("/", 1)[-1]
    if last_segment in ("set", "get"):
        return "command"
    if last_segment == "availability":
        return "availability"
    return "state"


class RateTracker:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
        now = int(self._clock())
        self._drained = now - (now % ROLLUP_SECONDS)

    def record(self, instance: str, kind: str) -> None:
        now = int(self._clock())
        cutoff = now - WINDOW_SECONDS
        with self._lock:
            bucket = self._buckets[(instance, kind)]
            bucket[now] = bucket.get(now, 0) + 1
            for stale in [ts for ts in bucket if ts < cutoff]:
                del bucket[stale]

    def snapshot(self) -> dict[str, dict[str, int]]:
        """Per-instance counts for the last complete second, plus a 60s total."""
        last_complete = int(self._clock()) - 1
        result: dict[str, dict[str, int]] = {}
        with self._lock:
            for (instance, kind), bucket in self._buckets.items():
                per_instance = result.setdefault(instance, {"total_60s": 0})
                per_instance[kind] = per_instance.get(kind, 0) + bucket.get(last_complete, 0)
                per_instance["total_60s"] += sum(
                    count for ts, count in bucket.items() if ts > last_complete - 60
                )
        return result

    def drain_completed_windows(self) -> list[tuple[int, str, str, int]]:
        """Rows (window_start, instance, kind, count) for 10s windows now complete.

        Each window is returned exactly once across calls (watermark-based).
        """
        now = int(self._clock())
        current_window = now - (now % ROLLUP_SECONDS)
        rows: list[tuple[int, str, str, int]] = []
        with self._lock:
            for window_start in range(self._drained, current_window, ROLLUP_SECONDS):
                for (instance, kind), bucket in self._buckets.items():
                    count = sum(
                        bucket.get(ts, 0)
                        for ts in range(window_start, window_start + ROLLUP_SECONDS)
                    )
                    if count:
                        rows.append((window_start, instance, kind, count))
            self._drained = current_window
        return rows
