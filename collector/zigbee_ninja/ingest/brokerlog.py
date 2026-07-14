"""T0.5: Mosquitto broker-log parsing for publish→client attribution.

With `log_dest topic` and debug logging enabled on the broker, Mosquitto
republishes its own log onto $SYS/broker/log/#; per-PUBLISH lines carry the
publishing client id. Format is tolerant-parsed and the feature degrades to
client-anonymous when lines don't match (DESIGN.md paragraph 4, T0.5).

Live verification of the exact format/overhead on the target broker is an M2
deploy-time step; the regex below matches the Mosquitto 2.x debug format:
  1720000000: Received PUBLISH from ha-core (d0, q0, r0, m0, 'z2m-1/lamp/set', ... (42 bytes))
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
