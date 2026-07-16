"""T0.5: Mosquitto broker-log parsing for publish→client attribution.

Per-PUBLISH debug lines carry the publishing client id. This module parses them
(tolerant; degrades to client-anonymous when lines don't match). The regex
matches the Mosquitto 2.x debug format:
  1720000000: Received PUBLISH from ha-core (d0, q0, r0, m0, 'z2m-1/lamp/set', ... (42 bytes))

DELIVERY CAVEAT (verified live on Mosquitto 2.0.22, DESIGN.md §4 T0.5):
`log_dest topic` does NOT publish debug-level lines to `$SYS/broker/log/#`:
only notice/subscribe-class messages reach the topic; the "Received PUBLISH
from …" lines go to stderr/file only. So feeding this parser requires a
broker-side log reader (journal/file tail), NOT a pure-MQTT subscription. The
`on_log` entry point therefore takes raw log-line bytes from whatever source
(topic today only carries non-PUBLISH lines); wiring a broker-side reader: or
preferring the HA-token per-automation path (§7.4): is a deployment choice.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable

LOG_TOPIC_PREFIX = "$SYS/broker/log/"
CORRELATION_TOLERANCE = 2.0

_PUBLISH_RE = re.compile(
    r"Received PUBLISH from (?P<client>\S+) \([^)]*'(?P<topic>[^']+)'"
)


class BrokerLogCorrelator:
    """Keeps a short memory of (topic → client) publish observations."""

    def __init__(self, clock: Callable[[], float] = time.time, memory: int = 4096):
        self._clock = clock
        self._recent: deque[tuple[float, str, str]] = deque(maxlen=memory)
        self.parsed = 0
        self.unparsed = 0

    def on_log(self, payload: bytes) -> tuple[str, str] | None:
        """Parse one log line; returns (client, topic) for PUBLISH lines."""
        try:
            text = payload.decode(errors="replace")
        except Exception:
            self.unparsed += 1
            return None
        match = _PUBLISH_RE.search(text)
        if match is None:
            self.unparsed += 1
            return None
        self.parsed += 1
        client = match.group("client")
        topic = match.group("topic")
        self._recent.append((self._clock(), topic, client))
        return client, topic

    def client_for(self, topic: str, tolerance: float = CORRELATION_TOLERANCE) -> str | None:
        """Most recent client that published `topic` within the tolerance window."""
        now = self._clock()
        for ts, seen_topic, client in reversed(self._recent):
            if now - ts > tolerance:
                break
            if seen_topic == topic:
                return client
        return None
