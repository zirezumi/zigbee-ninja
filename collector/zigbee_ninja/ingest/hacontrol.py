"""Home Assistant integration: per-automation attribution (DESIGN.md §7.4).

With a user-supplied long-lived token, a read-only WebSocket client subscribes
to `automation_triggered`, `script_started`, and `call_service` events. An
`mqtt.publish` service call carries its target topic; its context id (or parent
context) resolves to the automation/script run that fired it. That upgrades a
chain's commander from "(unattributed)" to the actual automation name — the
broker-safe replacement for T0.5 on brokers whose topic log can't carry
per-PUBLISH client lines.

HaAttribution is pure logic (fed event dicts; fully unit-testable); HaLink owns
the connection loop with the same cancellation-driven lifecycle as MqttIngest.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import websockets

CONTEXT_TTL_SECONDS = 600.0
CORRELATION_TOLERANCE_SECONDS = 3.0
MAX_BACKOFF_SECONDS = 30
SUBSCRIBED_EVENTS = ("automation_triggered", "script_started", "call_service")


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

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._context_names: dict[str, tuple[float, str]] = {}
        self._recent: deque[tuple[float, str, str]] = deque(maxlen=2048)
        self.counters = {"events": 0, "publishes": 0, "named": 0}

    def _remember_context(self, context_id: str | None, name: str) -> None:
        if context_id:
            self._context_names[context_id] = (self._clock(), name)

    def _resolve(self, context: dict) -> str:
        for key in ("id", "parent_id"):
            entry = self._context_names.get(context.get(key) or "")
            if entry is not None:
                return entry[1]
        if context.get("user_id"):
            return "user (UI/API)"
        return "ha (unresolved context)"

    def handle_event(self, event: dict) -> tuple[str, str] | None:
        """Feed one HA event dict; returns (topic, commander) for mqtt publishes."""
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
            topic = (data.get("service_data") or {}).get("topic")
            if not isinstance(topic, str):
                return None
            self.counters["publishes"] += 1
            commander = self._resolve(context)
            if not commander.startswith("ha ("):
                self.counters["named"] += 1
            self._recent.append((self._clock(), topic, commander))
            return topic, commander
        return None

    def name_for(self, topic: str) -> str | None:
        """Most recent HA-side publisher of `topic` within the tolerance window."""
        now = self._clock()
        for ts, seen_topic, name in reversed(self._recent):
            if now - ts > CORRELATION_TOLERANCE_SECONDS:
                break
            if seen_topic == topic:
                return name
        return None

    def _prune(self) -> None:
        cutoff = self._clock() - CONTEXT_TTL_SECONDS
        stale = [key for key, (ts, _) in self._context_names.items() if ts < cutoff]
        for key in stale:
            del self._context_names[key]


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
        on_publish: Callable[[str, str], None],
    ):
        self._config = config
        self._attribution = attribution
        self._on_publish = on_publish
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
                        result = self._attribution.handle_event(message.get("event") or {})
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
