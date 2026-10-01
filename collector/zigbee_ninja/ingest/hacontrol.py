"""Home Assistant integration: per-automation attribution (DESIGN.md §7.4).

With a user-supplied long-lived token, a read-only WebSocket client subscribes
to `automation_triggered`, `script_started`, and `call_service` events. An
`mqtt.publish` service call carries its target topic and payload; its context id
(or parent context) resolves to the automation/script run that fired it. That
upgrades a chain's commander from "(unattributed)" to the actual automation
name: the broker-safe replacement for T0.5 on brokers whose topic log can't
carry per-PUBLISH client lines.

Correlation is by topic AND payload fingerprint. Topic alone cannot separate two
constructs writing different parameters to one device inside the window, and
picking the most recent candidate silently credited the wrong one; see
`name_for` and `ChainTracker.attribute_client`, the two places a command can be
named depending on whether the wire or the HA event arrives first.

HaAttribution is pure logic (fed event dicts; fully unit-testable); HaLink owns
the connection loop with the same cancellation-driven lifecycle as MqttIngest.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass

import websockets

from ..attribution.chains import (
    AMBIGUOUS_COMMANDER,
    canonical_json,
    command_digest,
    correlation_digest,
)
from .registry import entity_slug

CONTEXT_TTL_SECONDS = 600.0
CORRELATION_TOLERANCE_SECONDS = 3.0
MAX_BACKOFF_SECONDS = 30
SUBSCRIBED_EVENTS = ("automation_triggered", "script_started", "call_service")

# Entity service calls that publish through an MQTT entity rather than
# `mqtt.publish`, and the `state` each one must have sent. A person's call on a
# Home Assistant light group arrives as one of these per member, carrying the
# person's context, and was filed "(unattributed)" because no publish event
# names it (14 commands in one 2026-09-29 05:33 group call, measured).
ENTITY_DOMAINS = ("light",)
ENTITY_SERVICES = {"turn_on": "ON", "turn_off": "OFF", "toggle": None}


def payload_fingerprint(payload: object) -> str | None:
    """`correlation_digest` of the bytes HA puts on the wire for this payload.

    Separator-agnostic, because the event stream cannot say which separators
    the wire bytes used:

    * A template rendering to a mapping arrives here as a native dict, because
      Home Assistant un-stringifies the rendered text. The wire carries that
      TEXT, which `| tojson` writes with spaces and `| to_json` without, so the
      dict is digested over `canonical_json` and the wire side over the same.
      The spaced form alone was pinned against 1032 live publishes and matched
      every one of them, because none of those used `to_json`; the publishes
      that did were never attributed.
    * A string payload is published verbatim, and digests the same way.

    None means the payload could not be rendered to bytes (absent, or a shape
    this does not model). That degrades the command to ambiguous rather than
    guessing, which is the safe direction: a confidently wrong commander is
    worse than an honest unknown.
    """
    if payload is None:
        return None
    if isinstance(payload, bytes):
        return correlation_digest(payload)
    if isinstance(payload, str):
        return correlation_digest(payload.encode("utf-8"))
    if isinstance(payload, (dict, list)):
        try:
            return command_digest(canonical_json(payload))
        except (TypeError, ValueError):
            return None
    try:
        return correlation_digest(json.dumps(payload).encode("utf-8"))
    except (TypeError, ValueError):
        return None


@dataclass
class HaConfig:
    url: str  # http(s)://host:8123
    token: str

    @classmethod
    def from_dict(cls, data: dict) -> HaConfig:
        return cls(url=data["url"].rstrip("/"), token=data["token"])

    def public_dict(self) -> dict:
        return {"url": self.url}

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.url.startswith("https") else "ws"
        host = self.url.split("://", 1)[1]
        return f"{scheme}://{host}/api/websocket"


class HaAttribution:
    """Context-id → automation/script name resolution + topic correlation."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        loop_skew_ms: Callable[[], float] | None = None,
        on_entity_call: Callable[[str, str, str | None], None] | None = None,
    ):
        self._clock = clock
        # Both the remembered publish and the command it should name are
        # stamped on the event loop, so a stall delays them unequally and can
        # push a genuine pair outside a fixed tolerance. See name_for.
        self._loop_skew_ms = loop_skew_ms or (lambda: 0.0)
        # (entity slug, commander, implied state) for each entity named by an
        # ENTITY_DOMAINS service call, so the engine can backfill a chain the
        # wire opened first. See entity_name_for for the other order.
        self._on_entity_call = on_entity_call
        self._context_names: dict[str, tuple[float, str]] = {}
        # (stamped_at, topic, payload fingerprint or None, commander).
        self._recent: deque[tuple[float, str, str | None, str]] = deque(maxlen=2048)
        # (stamped_at, entity slug, commander, implied state or None).
        self._recent_entities: deque[tuple[float, str, str, str | None]] = deque(maxlen=2048)
        self.counters = {
            "events": 0,
            "publishes": 0,
            "named": 0,
            "ambiguous": 0,
            "backfilled": 0,
            "backfill_unmatched": 0,
            "entity_calls": 0,
            "entity_named": 0,
        }

    def _remember_context(self, context_id: str | None, name: str) -> None:
        if not context_id:
            return
        # Delete before inserting so the entry moves to the END. Dicts preserve
        # insertion order but a plain re-assignment keeps the ORIGINAL position,
        # and _prune relies on position matching timestamp: without this, a
        # refreshed context would sit near the front carrying a new timestamp
        # and stop the prune early, stranding everything behind it. Refreshes
        # are real, not hypothetical: a script called by an automation reports
        # `script_started` under the CALLER's context id.
        self._context_names.pop(context_id, None)
        self._context_names[context_id] = (self._clock(), name)

    def context_count(self) -> int:
        """Live entries in the context table, for /api/ha.

        Bounded only by CONTEXT_TTL_SECONDS times the rate at which automations
        and scripts start, so on a busy installation it is large by design. It
        is worth watching: it is walked by every garbage collection, and until
        2026-08-16 it was walked by every HA event as well."""
        return len(self._context_names)

    def _resolve(self, context: dict) -> str:
        for key in ("id", "parent_id"):
            entry = self._context_names.get(context.get(key) or "")
            if entry is not None:
                return entry[1]
        if context.get("user_id"):
            return "user (UI/API)"
        return "ha (unresolved context)"

    def handle_event(self, event: dict) -> tuple[str, str, str | None] | None:
        """Feed one HA event dict.

        Returns (topic, commander, payload fingerprint) for mqtt publishes, so
        the caller can name the chain those exact bytes opened.
        """
        self.counters["events"] += 1
        self._prune()
        event_type = event.get("event_type")
        data = event.get("data") or {}
        context = event.get("context") or {}

        if event_type == "automation_triggered":
            name = data.get("name") or data.get("entity_id") or "automation"
            self._remember_context(context.get("id"), f"automation: {name}")
            return None
        if event_type == "script_started":
            name = data.get("name") or data.get("entity_id") or "script"
            self._remember_context(context.get("id"), f"script: {name}")
            return None
        if (
            event_type == "call_service"
            and data.get("domain") == "mqtt"
            and data.get("service") == "publish"
        ):
            service_data = data.get("service_data") or {}
            topic = service_data.get("topic")
            if not isinstance(topic, str):
                return None
            self.counters["publishes"] += 1
            commander = self._resolve(context)
            if not commander.startswith("ha ("):
                self.counters["named"] += 1
            fingerprint = payload_fingerprint(service_data.get("payload"))
            self._recent.append((self._clock(), topic, fingerprint, commander))
            return topic, commander, fingerprint
        if (
            event_type == "call_service"
            and data.get("domain") in ENTITY_DOMAINS
            and data.get("service") in ENTITY_SERVICES
        ):
            self._note_entity_call(data, context)
        return None

    def _note_entity_call(self, data: dict, context: dict) -> None:
        entity_ids = (data.get("service_data") or {}).get("entity_id")
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if not isinstance(entity_ids, list):
            return  # area or device targets: nothing to match on
        commander = self._resolve(context)
        want_state = ENTITY_SERVICES[data["service"]]
        now = self._clock()
        prefix = f"{data['domain']}."
        for entity_id in entity_ids:
            if not isinstance(entity_id, str) or not entity_id.startswith(prefix):
                continue
            slug = entity_id[len(prefix) :]
            self.counters["entity_calls"] += 1
            self._recent_entities.append((now, slug, commander, want_state))
            if self._on_entity_call is not None:
                try:
                    self._on_entity_call(slug, commander, want_state)
                except Exception:  # noqa: BLE001 - never kill the link
                    pass

    def entity_name_for(self, target: str, payload: bytes) -> str | None:
        """Commander of a recent entity service call on `target`, at wire time.

        For the order where the HA event arrives before the command reaches the
        broker. A candidate whose implied state the payload contradicts is not
        one; two distinct commanders left over are AMBIGUOUS_COMMANDER.
        """
        if not self._recent_entities:
            return None
        slug = entity_slug(target)
        now = self._clock()
        tolerance = CORRELATION_TOLERANCE_SECONDS + max(self._loop_skew_ms(), 0.0) / 1000.0
        candidates = []
        for ts, seen_slug, commander, want_state in reversed(self._recent_entities):
            if now - ts > tolerance:
                break
            if seen_slug == slug:
                candidates.append((commander, want_state))
        if not candidates:
            return None
        sent = None
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict) and isinstance(parsed.get("state"), str):
                sent = parsed["state"].upper()
        except (ValueError, UnicodeDecodeError):
            pass
        names = {
            commander
            for commander, want_state in candidates
            if want_state is None or sent is None or sent == want_state
        }
        if not names:
            return None
        self.counters["entity_named"] += 1
        if len(names) > 1:
            self.counters["ambiguous"] += 1
            return AMBIGUOUS_COMMANDER
        return next(iter(names))

    def name_for(self, topic: str, digest: str | None = None) -> str | None:
        """HA-side publisher of these exact bytes on `topic`, within the window.

        Topic alone does not identify a publisher. Two scripts writing different
        parameters to one device inside the window are both candidates, and
        returning the most recent of them credited whichever HA publish happened
        to be recorded last: on this installation that swapped a fill-bar
        intensity write with an indicator-timeout write on every render. So a
        candidate must match the payload as well, and when the payload cannot
        pick a single one the answer is AMBIGUOUS_COMMANDER rather than a guess.

        The window widens by the loop lag in flight. Both sides of the
        correlation are stamped on the event loop, so a stall stretches the
        apparent distance between a publish and the command it explains; with a
        fixed 3 s bound, any stall longer than that permanently dropped the
        commander name and the publish fell to "(unattributed)" forever. The
        extra slack only ever admits an older candidate for the SAME topic, and
        a healthy loop (sub-millisecond lag) leaves the bound untouched.
        """
        now = self._clock()
        tolerance = CORRELATION_TOLERANCE_SECONDS + max(self._loop_skew_ms(), 0.0) / 1000.0
        exact: set[str] = set()
        unverifiable = False
        for ts, seen_topic, seen_digest, name in reversed(self._recent):
            if now - ts > tolerance:
                break
            if seen_topic != topic:
                continue
            if seen_digest is None:
                # An HA publish to this topic whose bytes could not be rendered.
                # It cannot be confirmed as this command, and it cannot be ruled
                # out either, so it taints the window rather than being ignored.
                unverifiable = True
            elif digest is not None and seen_digest == digest:
                exact.add(name)
        if len(exact) == 1 and not unverifiable:
            return next(iter(exact))
        if exact or unverifiable:
            self.counters["ambiguous"] += 1
            return AMBIGUOUS_COMMANDER
        return None

    def note_backfill(self, matched: bool) -> None:
        """Record whether an HA publish found the chain its bytes opened.

        The correlation depends on reproducing the wire bytes exactly, and if
        that fidelity ever breaks the symptom is silent: nothing is misnamed,
        matches simply stop landing. A persistently climbing `backfill_unmatched`
        against a flat `backfilled` is what that looks like, and is worth more
        than inspecting a build. Some misses are normal: the HA event can arrive
        before the command reaches the broker, in which case `name_for` names it
        at wire time instead.
        """
        self.counters["backfilled" if matched else "backfill_unmatched"] += 1

    def _prune(self) -> int:
        """Drop contexts past their TTL. Returns how many entries were examined.

        Called on EVERY event, so its cost is the per-event cost. It used to
        scan the whole table (`[k for k, (ts, _) in ... if ts < cutoff]`), which
        is O(n) per event and therefore O(n^2) across a burst. That was the
        top loop-stall source on the reference deployment, found 2026-08-16 once
        `ha_event` carried a span: 36,015 ms of loop time over 18,545 calls,
        **1.942 ms each**, every one of them far under the 100 ms slow bar and
        so individually invisible. At a measured 15 ns per entry that is on the
        order of 10^5 live contexts being walked per event, and the table is
        large by construction: entries live CONTEXT_TTL_SECONDS and a busy
        installation starts automations and scripts continuously.

        Insertion order is timestamp order (see _remember_context, which moves a
        refreshed entry to the end to keep it so), therefore the expired entries
        are a PREFIX and the scan can stop at the first live one. That makes the
        work proportional to what actually expired rather than to the table
        size: amortised O(1) per event, with identical retention.

        The returned count is what makes the prefix property testable without a
        timing assertion, which would be flaky and would not say WHY it passed.
        It is INCREMENTED IN THE LOOP rather than derived from the results
        afterwards, and that is not a style choice: a derived count
        (`len(stale)` plus one for the entry that stopped it) returns exactly 1
        for a full scan of an all-live table too, so the assertion built on it
        certified nothing. Caught by the mutation probe, which is what that
        probe is for.
        """
        cutoff = self._clock() - CONTEXT_TTL_SECONDS
        stale: list[str] = []
        examined = 0
        for key, (ts, _) in self._context_names.items():
            examined += 1
            if ts >= cutoff:
                break
            stale.append(key)
        for key in stale:
            del self._context_names[key]
        return examined


async def test_ha(config: HaConfig, timeout: float = 6.0) -> str | None:
    """Connect + authenticate; None on success, else a human-readable error."""

    async def _probe() -> str | None:
        async with websockets.connect(config.ws_url, max_size=2**22) as ws:
            first = json.loads(await ws.recv())
            if first.get("type") != "auth_required":
                return f"Unexpected handshake: {first.get('type')}"
            await ws.send(json.dumps({"type": "auth", "access_token": config.token}))
            verdict = json.loads(await ws.recv())
            if verdict.get("type") == "auth_ok":
                return None
            return verdict.get("message") or "Authentication rejected"

    try:
        return await asyncio.wait_for(_probe(), timeout)
    except TimeoutError:
        return f"Timed out connecting to {config.ws_url}"
    except OSError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - websocket handshake errors vary
        return str(exc) or exc.__class__.__name__


class HaLink:
    def __init__(
        self,
        config: HaConfig,
        attribution: HaAttribution,
        on_publish: Callable[[str, str, str | None], None],
        activity=None,
    ):
        self._config = config
        self._attribution = attribution
        self._on_publish = on_publish
        # Optional so this class stays constructible without an Engine. When
        # absent the span is a nullcontext and nothing is measured.
        self._activity = activity
        self.status: dict = {"state": "disconnected", "error": None, "connected_since": None}

    def _set_status(self, state: str, error: str | None = None) -> None:
        self.status = {
            "state": state,
            "error": error,
            "connected_since": time.time() if state == "connected" else None,
        }

    async def run(self) -> None:
        backoff = 1.0
        while True:
            self._set_status("connecting")
            try:
                async with websockets.connect(self._config.ws_url, max_size=2**22) as ws:
                    first = json.loads(await ws.recv())
                    if first.get("type") != "auth_required":
                        raise ConnectionError("unexpected HA handshake")
                    await ws.send(
                        json.dumps({"type": "auth", "access_token": self._config.token})
                    )
                    verdict = json.loads(await ws.recv())
                    if verdict.get("type") != "auth_ok":
                        self._set_status(
                            "error", verdict.get("message") or "authentication rejected"
                        )
                        await asyncio.sleep(MAX_BACKOFF_SECONDS)  # bad token: back off hard
                        continue
                    for index, event_type in enumerate(SUBSCRIBED_EVENTS, start=1):
                        await ws.send(
                            json.dumps(
                                {
                                    "id": index,
                                    "type": "subscribe_events",
                                    "event_type": event_type,
                                }
                            )
                        )
                    self._set_status("connected")
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            continue
                        if message.get("type") != "event":
                            continue
                        # Every subscribed HA event runs handle_event on the
                        # event loop, and a busy house fires them in bursts, so
                        # this is on-loop work that used to be invisible: no
                        # single event is slow enough to reach the slow ring,
                        # but a burst of them still holds the loop.
                        with (
                            self._activity.span("ha_event")
                            if self._activity
                            else nullcontext()
                        ):
                            result = self._attribution.handle_event(
                                message.get("event") or {}
                            )
                            if result is not None:
                                try:
                                    self._on_publish(*result)
                                except Exception:  # noqa: BLE001 - never kill the link
                                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport error
                self._set_status("error", str(exc) or exc.__class__.__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
