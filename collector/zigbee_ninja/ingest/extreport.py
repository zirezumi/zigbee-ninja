"""External command reports (DESIGN §9.1).

A Zigbee2MQTT extension can send Zigbee frames from inside Zigbee2MQTT, through
the devices' own converters. Those commands never appear as `<target>/set`, so
chains would never see them. An extension that wants them measured publishes one
report per frame on `<base>/zigbee-ninja/ext/command`, BEFORE it hands the frame
over (so the report reaches the broker ahead of the state the frame causes, and
the command is judged against the device's state before it, as a `/set` is):

    {"v": 1, "reporter": "<extension name>", "version": "<its version>",
     "target": "<device or group friendly name>", "payload": {<exactly what it sent>},
     "t": <its clock, seconds>, "ref": "<its own id for the operation>",
     "cause": {"topic": "<the full topic of the MQTT message that made it send>",
               "digest": "<first 12 hex digits of the SHA-1 of that message's bytes>"}}

`ref`, `t` and `cause` are optional. The engine opens a `set` chain per report,
judged by the unchanged no-op detector; `cause` lets it name the chain's client
from the Home Assistant publish of the causing message, through the cause ring.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from ..attribution.chains import correlation_digest

EXT_COMMAND_SUFFIX = "zigbee-ninja/ext/command"
REPORT_VERSION = 1
# How long a causing message is remembered: an extension may queue frames behind a
# per-device lock, so a frame can follow its command by a few seconds.
CAUSE_TTL_SECONDS = 30.0
CAUSE_MAX_ENTRIES = 1024
_REPORTER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{12}$")


@dataclass(frozen=True)
class ExtReport:
    reporter: str
    target: str
    payload: dict
    ref: str | None
    cause_digest: str | None


def parse_report(payload: bytes) -> tuple[ExtReport | None, str | None]:
    """-> (report, None) or (None, the reason it was refused). Never raises."""
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None, "not_json"
    if not isinstance(data, dict):
        return None, "not_object"
    if data.get("v") != REPORT_VERSION:
        return None, "version"
    reporter = data.get("reporter")
    if not isinstance(reporter, str) or not _REPORTER_RE.match(reporter):
        return None, "reporter"
    target = data.get("target")
    if not isinstance(target, str) or not target:
        return None, "target"
    body = data.get("payload")
    if not isinstance(body, dict) or not body:
        return None, "payload"
    ref = data.get("ref")
    cause = data.get("cause")
    digest = None
    if isinstance(cause, dict) and isinstance(cause.get("digest"), str):
        if _DIGEST_RE.match(cause["digest"]):
            digest = cause["digest"]
    return ExtReport(reporter, target, body, ref if isinstance(ref, str) else None, digest), None


def raw_digest(payload: bytes) -> str:
    """The digest a report's `cause.digest` carries: of the bytes as published."""
    return hashlib.sha1(payload).hexdigest()[:12]


class CauseRing:
    """Recently seen non-entity messages, by the SHA-1 of their raw bytes.

    Fed with every message on a base topic that is neither a command nor an
    entity's state (an extension's own command topic among them). A report's
    `cause.digest` looks its causing message up here, which yields the topic,
    the separator-agnostic correlation digest an HA publish is matched by, and
    when it arrived. Bounded in age and size, so a quiet extension costs nothing
    and a busy one cannot grow it.
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str], tuple[float, str, str]] = OrderedDict()

    def note(self, base: str, topic: str, payload: bytes) -> None:
        now = self._clock()
        self._prune(now)
        key = (base, raw_digest(payload))
        self._entries.pop(key, None)
        self._entries[key] = (now, topic, correlation_digest(payload))
        while len(self._entries) > CAUSE_MAX_ENTRIES:
            self._entries.popitem(last=False)

    def lookup(self, base: str, digest: str) -> tuple[float, str, str] | None:
        """-> (received at, topic, correlation digest) or None."""
        self._prune(self._clock())
        return self._entries.get((base, digest))

    def _prune(self, now: float) -> None:
        cutoff = now - CAUSE_TTL_SECONDS
        while self._entries:
            key, (ts, _, _) = next(iter(self._entries.items()))
            if ts >= cutoff:
                break
            del self._entries[key]

    def __len__(self) -> int:
        return len(self._entries)
