"""Wires broker config → MQTT ingest → registry, rates, and attribution."""

from __future__ import annotations

import asyncio
import time

from ..attribution.chains import ChainTracker, parse_command
from ..store.config import ConfigStore
from ..store.db import Database
from ..tiles import TileManager
from .brokerlog import LOG_TOPIC_PREFIX, BrokerLogCorrelator
from .mqtt import BrokerConfig, MqttIngest
from .probe import ProbeIngest
from .rates import GLOBAL, ROLLUP_SECONDS, RateTracker, classify
from .registry import Registry

ROLLUP_RETENTION_SECONDS = 14 * 24 * 3600  # 10s tiers keep two weeks (DESIGN.md §12)
CHAIN_RETENTION_SECONDS = 48 * 3600  # chain detail keeps 48h (DESIGN.md §12)


class Engine:
    def __init__(self, db: Database, config: ConfigStore):
        self._db = db
        self._config = config
        self.registry = Registry()
        self.rates = RateTracker()
        self.class_rates = RateTracker()
        self.brokerlog = BrokerLogCorrelator()
        self.chains = ChainTracker(resolve_members=self._resolve_members)
        self.probes = ProbeIngest(
            resolve_members=self._resolve_members, on_heartbeat=self._on_probe_heartbeat
        )
        self.tiles = TileManager(db, publisher=self.publish)
        self._ingest: MqttIngest | None = None
        self._ingest_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None

    def _resolve_members(self, instance: str, target: str) -> list[str]:
        return self.registry.group_members(instance, target)

    def _on_probe_heartbeat(self, base: str, heartbeat: dict) -> None:
        self.tiles.on_heartbeat(base, heartbeat)

    async def publish(self, topic: str, payload: str) -> None:
        """Publish on the ingest connection; self-attributed (DESIGN.md P4)."""
        if self._ingest is None:
            raise RuntimeError("Broker is not configured")
        await self._ingest.publish(topic, payload)
        base = self.registry.base_for(topic)
        self.class_rates.record(base or GLOBAL, "self")

    # -- lifecycle -----------------------------------------------------------

    def broker_config(self) -> BrokerConfig | None:
        data = self._config.get("broker")
        return BrokerConfig.from_dict(data) if data else None

    async def start(self) -> None:
        self._flush_task = asyncio.create_task(self._flush_loop())
        await self.restart_ingest()

    async def stop(self) -> None:
        for task in (self._ingest_task, self._flush_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ingest_task = None
        self._flush_task = None

    async def restart_ingest(self) -> None:
        if self._ingest_task is not None:
            self._ingest_task.cancel()
            try:
                await self._ingest_task
            except asyncio.CancelledError:
                pass
            self._ingest_task = None
            self._ingest = None
        config = self.broker_config()
        if config is not None:
            self._ingest = MqttIngest(config, self.on_message)
            self._ingest_task = asyncio.create_task(self._ingest.run())

    async def apply_broker_config(self, data: dict) -> None:
        # TODO(DESIGN.md §15): encrypt secrets at rest before the M6 hardening pass.
        self._config.set("broker", data)
        await self.restart_ingest()

    # -- data path -----------------------------------------------------------

    def on_message(self, topic: str, payload: bytes) -> None:
        if topic.startswith(LOG_TOPIC_PREFIX):
            parsed = self.brokerlog.on_log(payload)
            if parsed is not None:
                client, published_topic = parsed
                self._attribute_from_log(client, published_topic)
            return

        self.registry.handle(topic, payload)
        base = self.registry.base_for(topic)
        if base is None:
            self.rates.record(GLOBAL, "other")
            return

        kind = classify(topic, base)
        self.rates.record(base, kind)
        self.rates.record(GLOBAL, kind)

        suffix = topic[len(base) + 1 :]
        if kind == "probe":
            self.probes.handle(base, suffix, payload)
            return
        if kind == "bridge" and suffix.startswith("bridge/response/extension/"):
            action = suffix.rsplit("/", 1)[-1]
            self.tiles.on_bridge_response(base, action, payload)
            return
        if kind == "command":
            command = parse_command(suffix)
            if command is not None:
                target, verb = command
                client = self.brokerlog.client_for(topic)
                self.chains.on_command(base, target, verb, payload, client=client)
                self.class_rates.record(base, "commanded")
        elif kind == "state":
            klass = self.chains.on_state(base, suffix)
            self.class_rates.record(base, klass)

    def _attribute_from_log(self, client: str, published_topic: str) -> None:
        base = self.registry.base_for(published_topic)
        if base is None:
            return
        command = parse_command(published_topic[len(base) + 1 :])
        if command is not None:
            self.chains.attribute_client(base, command[0], client)

    def ingest_status(self) -> dict:
        if self._ingest is None:
            return {"state": "unconfigured", "error": None, "connected_since": None}
        return dict(self._ingest.status)

    # -- rollups & persistence -------------------------------------------------

    def flush_rollups(self) -> int:
        conn = self._db.connect()
        now = int(time.time())
        written = 0

        rows = self.rates.drain_completed_windows()
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO series_10s (ts, instance, kind, count) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "DELETE FROM series_10s WHERE ts < ?", (now - ROLLUP_RETENTION_SECONDS,)
            )
            written += len(rows)

        class_rows = self.class_rates.drain_completed_windows()
        if class_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO attribution_10s (ts, instance, klass, count) "
                "VALUES (?, ?, ?, ?)",
                class_rows,
            )
            conn.execute(
                "DELETE FROM attribution_10s WHERE ts < ?", (now - ROLLUP_RETENTION_SECONDS,)
            )
            written += len(class_rows)

        finalized = self.chains.drain_finalized()
        if finalized:
            conn.executemany(
                "INSERT INTO chains (instance, target, verb, opened_at, client, "
                "payload_size, echo_count, first_echo_ms, redundant) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        chain.instance,
                        chain.target,
                        chain.verb,
                        chain.opened_at,
                        chain.client,
                        chain.payload_size,
                        chain.echoes,
                        chain.first_echo_ms,
                        int(chain.redundant),
                    )
                    for chain in finalized
                ],
            )
            conn.execute(
                "DELETE FROM chains WHERE opened_at < ?", (now - CHAIN_RETENTION_SECONDS,)
            )
            written += len(finalized)

        if written:
            conn.commit()
        return written

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(ROLLUP_SECONDS)
            try:
                self.flush_rollups()
            except Exception:
                # Never let a storage hiccup kill the loop; next tick retries.
                pass
