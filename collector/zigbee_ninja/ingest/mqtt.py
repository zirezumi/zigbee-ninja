"""T0 MQTT ingest: full-firehose subscription with reconnect/backoff.

The ingest task is cancellation-driven: the Engine cancels it on shutdown or
broker reconfiguration, and the async context manager closes the client cleanly.
Handler exceptions do not propagate: a bug in a downstream consumer must never
kill the firehose. They are logged and counted, and the counters reach
/api/health, because the alternative has already cost us. This handler swallowed
every probe heartbeat write for 29 hours while `handler_errors` (then read by
nothing at all) was the only evidence it was happening.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiomqtt

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 30

# A downstream bug can fail on every message, so log the first one and then
# thin out: a thousand identical tracebacks an hour bury the log without
# adding anything the counter does not already say.
_HANDLER_ERROR_LOG_EVERY = 500

# Z2M discovery/registry topics, subscribed BEFORE the "#" firehose. On a broker
# with a large retained set (a Home Assistant broker holds hundreds of retained
# `homeassistant/.../config` messages), a "#"-only subscribe floods the client
# with retained traffic and the broker can drop late-sorting `z2m-*/bridge/*`
# retained messages from the per-client queue: leaving discovery empty. A
# dedicated up-front subscribe delivers the (small) retained bridge set first,
# so discovery never depends on surviving the flood. Both single- and two-level
# base topics are covered (e.g. `z2m-1/bridge/*` and `home/z2m/bridge/*`).
DISCOVERY_TOPICS = ("+/bridge/#", "+/+/bridge/#")


@dataclass
class BrokerConfig:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> BrokerConfig:
        return cls(
            host=data["host"],
            port=int(data.get("port") or 1883),
            username=data.get("username") or None,
            password=data.get("password") or None,
        )

    def public_dict(self) -> dict:
        return {"host": self.host, "port": self.port, "username": self.username}

    def client(self, identifier: str = "zigbee-ninja") -> aiomqtt.Client:
        return aiomqtt.Client(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            identifier=identifier,
        )


async def test_connection(config: BrokerConfig, timeout: float = 5.0) -> str | None:
    """Try to connect; return None on success or a human-readable error."""

    async def _probe() -> None:
        async with config.client(identifier="zigbee-ninja-test"):
            pass

    try:
        await asyncio.wait_for(_probe(), timeout)
        return None
    except TimeoutError:
        return f"Timed out connecting to {config.host}:{config.port}"
    except aiomqtt.MqttError as exc:
        return str(exc) or exc.__class__.__name__
    except OSError as exc:
        return str(exc)


class MqttIngest:
    def __init__(self, config: BrokerConfig, on_message: Callable[[str, bytes], None]):
        self._config = config
        self._on_message = on_message
        self._client: aiomqtt.Client | None = None
        self.status: dict = {"state": "disconnected", "error": None, "connected_since": None}
        self.handler_errors = 0
        self.last_handler_error: dict | None = None

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        client = self._client
        if client is None or self.status["state"] != "connected":
            raise RuntimeError("MQTT broker is not connected")
        await client.publish(topic, payload, qos=0, retain=retain)

    @staticmethod
    async def _subscribe(client) -> None:
        """Discovery topics first, then the firehose (see DISCOVERY_TOPICS)."""
        for topic in DISCOVERY_TOPICS:
            await client.subscribe(topic)
        await client.subscribe([("#", 0), ("$SYS/#", 0)])

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
                async with self._config.client() as client:
                    await self._subscribe(client)
                    self._client = client
                    self._set_status("connected")
                    backoff = 1.0
                    async for message in client.messages:
                        try:
                            self._on_message(str(message.topic), bytes(message.payload or b""))
                        except Exception as exc:
                            self.handler_errors += 1
                            self.last_handler_error = {
                                "at": time.time(),
                                "topic": str(message.topic),
                                "error": f"{exc.__class__.__name__}: {exc}",
                            }
                            if (
                                self.handler_errors == 1
                                or self.handler_errors % _HANDLER_ERROR_LOG_EVERY == 0
                            ):
                                logger.exception(
                                    "message handler failed on %s (%d so far)",
                                    message.topic,
                                    self.handler_errors,
                                )
            except aiomqtt.MqttError as exc:
                self._set_status("error", str(exc) or exc.__class__.__name__)
            except OSError as exc:
                self._set_status("error", str(exc))
            finally:
                self._client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
