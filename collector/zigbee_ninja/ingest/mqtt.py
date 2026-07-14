"""T0 MQTT ingest: full-firehose subscription with reconnect/backoff.

The ingest task is cancellation-driven: the Engine cancels it on shutdown or
broker reconfiguration, and the async context manager closes the client cleanly.
Handler exceptions are swallowed (with a status note) — a bug in a downstream
consumer must never kill the firehose.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiomqtt

MAX_BACKOFF_SECONDS = 30


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

    async def publish(self, topic: str, payload: str) -> None:
        client = self._client
        if client is None or self.status["state"] != "connected":
            raise RuntimeError("MQTT broker is not connected")
        await client.publish(topic, payload, qos=0)

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
                    # "#" does not match $-prefixed topics, so $SYS needs its own
                    # subscription (used by T0.5 broker-log attribution in M2).
                    await client.subscribe([("#", 0), ("$SYS/#", 0)])
                    self._client = client
                    self._set_status("connected")
                    backoff = 1.0
                    async for message in client.messages:
                        try:
                            self._on_message(str(message.topic), bytes(message.payload or b""))
                        except Exception:
                            self.handler_errors += 1
            except aiomqtt.MqttError as exc:
                self._set_status("error", str(exc) or exc.__class__.__name__)
            except OSError as exc:
                self._set_status("error", str(exc))
            finally:
                self._client = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
