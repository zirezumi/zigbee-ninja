"""Wires broker config → MQTT ingest → registry + rate tracker; owns the tasks."""

from __future__ import annotations

import asyncio
import time

from ..store.config import ConfigStore
from ..store.db import Database
from .mqtt import BrokerConfig, MqttIngest
from .rates import GLOBAL, ROLLUP_SECONDS, RateTracker, classify
from .registry import Registry

ROLLUP_RETENTION_SECONDS = 14 * 24 * 3600  # 10s tier keeps two weeks (DESIGN.md §12)


class Engine:
    def __init__(self, db: Database, config: ConfigStore):
        self._db = db
        self._config = config
        self.registry = Registry()
        self.rates = RateTracker()
        self._ingest: MqttIngest | None = None
        self._ingest_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None

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
        self.registry.handle(topic, payload)
        base = self.registry.base_for(topic)
        if base is not None:
            kind = classify(topic, base)
            self.rates.record(base, kind)
        else:
            kind = "other"
        self.rates.record(GLOBAL, kind)

    def ingest_status(self) -> dict:
        if self._ingest is None:
            return {"state": "unconfigured", "error": None, "connected_since": None}
        return dict(self._ingest.status)

    # -- rollups (retention v0) ----------------------------------------------

    def flush_rollups(self) -> int:
        rows = self.rates.drain_completed_windows()
        if rows:
            conn = self._db.connect()
            conn.executemany(
                "INSERT OR REPLACE INTO series_10s (ts, instance, kind, count) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            cutoff = int(time.time()) - ROLLUP_RETENTION_SECONDS
            conn.execute("DELETE FROM series_10s WHERE ts < ?", (cutoff,))
            conn.commit()
        return len(rows)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(ROLLUP_SECONDS)
            try:
                self.flush_rollups()
            except Exception:
                # Never let a storage hiccup kill the loop; next tick retries.
                pass
