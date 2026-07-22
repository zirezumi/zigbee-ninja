"""Wires broker config → MQTT ingest → registry, rates, and attribution."""

from __future__ import annotations

import asyncio
import gc
import json
import secrets
import threading
import time
from contextlib import contextmanager

from .. import __version__
from ..alerts import GLOBAL_INSTANCE, AlertManager
from ..attribution.chains import Chain, ChainTracker, parse_command
from ..calibration.benchmark import CalibrationManager
from ..capacity import airtime, ledger
from ..capacity import headroom as headroom_model
from ..capacity import hops as hop_model
from ..capacity.headroom import TX_BUCKETS
from ..ha_discovery import PUBLISH_INTERVAL_SECONDS, DiscoveryPublisher
from ..recommend.runner import RecommendationEngine
from ..store.config import ConfigStore
from ..store.db import Database
from ..store.events import RawEventLog
from ..store.secrets import SecretBox, is_encrypted
from ..tiles import (
    CAPABILITY_MQTT_DISCOVERY,
    CAPABILITY_TOPOLOGY,
    CAPABILITY_Z2M_EXTENSION,
    TileManager,
)
from .brokerlog import LOG_TOPIC_PREFIX, BrokerLogCorrelator
from .fusion import FusionTracker
from .hacontrol import HaAttribution, HaConfig, HaLink
from .mqtt import BrokerConfig, MqttIngest
from .probe import ProbeIngest
from .rates import GLOBAL, ROLLUP_SECONDS, RateTracker, classify
from .registry import Registry
from .tap import TapIngest
from .topology import TopologyPuller
from .topology import graph as topology_graph

# Retention defaults (DESIGN.md §12); settings-backed knobs override at runtime.
DEFAULT_ROLLUP_RETENTION_DAYS = 14  # 10s tiers
DEFAULT_CHAIN_RETENTION_HOURS = 48  # chain detail
DEFAULT_TOPOLOGY_SNAPSHOTS = 20  # per instance
JOURNAL_RETENTION_DAYS = 90  # change journal (V2_PROPOSAL.md §V2-3)

LOOP_LAG_SAMPLE_SECONDS = 1.0
LOOP_LAG_WINDOW_SECONDS = 60.0
LOOP_LAG_STALL_MS = 250.0
LOOP_LAG_STALLS_KEPT = 32
ACTIVITY_SLOW_MS = 100.0
ACTIVITY_ENTRIES_KEPT = 64


class LoopLagMonitor:
    """Event-loop scheduling lag: how late a short sleep wakes up.

    Synchronous work on the loop (storage flushes, GC, a busy handler)
    shows up here before it can distort time-sensitive consumers: the
    calibration pacer and echo RTT stamps both live on this loop. The
    worst sample in the last minute feeds the alert evaluator; the last
    few stalls keep their wall-clock timestamps so they can be matched
    against the activity log's record of what was running."""

    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self._samples: list[tuple[float, float]] = []
        self.last_ms = 0.0
        self.ewma_ms: float | None = None
        self.stalls = 0
        self.recent_stalls: list[dict] = []

    def record(self, lag_seconds: float) -> None:
        now = self._clock()
        lag_ms = max(lag_seconds, 0.0) * 1000.0
        self.last_ms = lag_ms
        self.ewma_ms = (
            lag_ms if self.ewma_ms is None else 0.2 * lag_ms + 0.8 * self.ewma_ms
        )
        if lag_ms >= LOOP_LAG_STALL_MS:
            self.stalls += 1
            # Stamped at sampler wake-up: the stall itself lies somewhere in
            # the preceding sample interval plus the lag.
            self.recent_stalls.append(
                {"at": self._wall(), "lag_ms": round(lag_ms, 1)}
            )
            del self.recent_stalls[:-LOOP_LAG_STALLS_KEPT]
        cutoff = now - LOOP_LAG_WINDOW_SECONDS
        self._samples = [(ts, lag) for ts, lag in self._samples if ts >= cutoff]
        self._samples.append((now, lag_ms))

    def max_window_ms(self) -> float:
        return max((lag for _, lag in self._samples), default=0.0)

    def stats(self) -> dict:
        return {
            "last_ms": round(self.last_ms, 1),
            "ewma_ms": round(self.ewma_ms, 1) if self.ewma_ms is not None else None,
            "max_60s_ms": round(self.max_window_ms(), 1),
            "stalls_over_250ms": self.stalls,
            "recent_stalls": list(self.recent_stalls),
        }


class LoopActivityLog:
    """Names what was running when the event loop stalled.

    The lag monitor says when the loop stalled; this log says what held it:
    spans wrap the known synchronous on-loop work (MQTT message handling,
    discovery metric assembly, tile heartbeat writes, fleet snapshot
    assembly, tap decode) and gc callbacks time collection pauses, which
    hold the GIL and so pause the loop from any thread. Per-label totals
    plus a ring of slow entries (wall-clock stamped) feed /api/health,
    where a stall timestamp can be matched to the span that covers it."""

    def __init__(self, clock=time.monotonic, wall=time.time):
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._totals: dict[str, dict] = {}
        self._slow: list[dict] = []
        self._gc_started: dict[int, float] = {}

    @contextmanager
    def span(self, label: str):
        started = self._clock()
        try:
            yield
        finally:
            self.note(label, (self._clock() - started) * 1000.0)

    def note(self, label: str, duration_ms: float) -> None:
        with self._lock:
            entry = self._totals.setdefault(label, {"count": 0, "max_ms": 0.0})
            entry["count"] += 1
            entry["max_ms"] = max(entry["max_ms"], duration_ms)
            if duration_ms >= ACTIVITY_SLOW_MS:
                entry["slow"] = entry.get("slow", 0) + 1
                # Stamped at span end: the span covers [at - ms, at].
                self._slow.append(
                    {"label": label, "at": self._wall(), "ms": round(duration_ms, 1)}
                )
                del self._slow[:-ACTIVITY_ENTRIES_KEPT]

    # GC callbacks run on whichever thread triggered the collection, but a
    # collection pause holds the GIL, so the loop pauses with it either way.
    def install_gc(self) -> None:
        if self._on_gc not in gc.callbacks:
            gc.callbacks.append(self._on_gc)

    def remove_gc(self) -> None:
        if self._on_gc in gc.callbacks:
            gc.callbacks.remove(self._on_gc)

    def _on_gc(self, phase: str, info: dict) -> None:
        generation = info.get("generation", 0)
        if phase == "start":
            self._gc_started[generation] = self._clock()
        elif phase == "stop":
            started = self._gc_started.pop(generation, None)
            if started is not None:
                self.note(f"gc_gen{generation}", (self._clock() - started) * 1000.0)

    def stats(self) -> dict:
        with self._lock:
            return {
                "totals": {
                    label: {
                        "count": entry["count"],
                        "slow": entry.get("slow", 0),
                        "max_ms": round(entry["max_ms"], 1),
                    }
                    for label, entry in sorted(self._totals.items())
                },
                "recent_slow": list(self._slow),
            }


class Engine:
    def __init__(
        self, db: Database, config: ConfigStore, secrets: SecretBox, events: RawEventLog
    ):
        self._db = db
        self._config = config
        self._secrets = secrets
        self.events = events
        self._upgrade_secrets()
        self.registry = Registry(on_change=self._on_registry_change)
        self.rates = RateTracker()
        self.class_rates = RateTracker()
        self.brokerlog = BrokerLogCorrelator()
        self.chains = ChainTracker(resolve_members=self._resolve_members)
        self.fusion = FusionTracker()
        self.probes = ProbeIngest(
            resolve_members=self._resolve_members,
            on_heartbeat=self._on_probe_heartbeat,
            on_device_seq=self._on_probe_device_seq,
        )
        self.tiles = TileManager(db, publisher=self.publish)
        self.tap = TapIngest(
            resolve_instance=self.registry.instance_for_endpoint,
            router_count=self.registry.router_count_for,
            on_event=lambda ts, instance, name, direction, size: self.events.record(
                ts, "wire", instance, name, direction, None, size
            ),
            on_zcl_incoming=self.fusion.on_wire,
        )
        self.topology = TopologyPuller(
            db,
            publisher=self.publish,
            granted=lambda base: self.tiles.is_granted(CAPABILITY_TOPOLOGY, base),
            snapshots_kept=lambda: self.runtime_settings()["retention_topology_snapshots"],
        )
        self.calibration = CalibrationManager(
            db,
            publisher=self.publish,
            devices=self.registry.devices,
            groups=self.registry.groups,
            instances=self.registry.snapshot,
            topology_latest=lambda base: (
                self.topology.latest(base, include_raw=True).get(base) or {}
            ),
            wire_covers=self.tap.wire_covers,
            wire_latency_mark=self.tap.latency.latest_ts,
            wire_latency_since=self.tap.latency.samples_since,
            wire_delivery_totals=self.tap.wire_delivery_totals,
            events_log=self.events,
            registry=self.registry,
            pricing=self.tap.pricing_params,
        )
        self.ha_attr = HaAttribution()
        self.alerts = AlertManager(db, config, provider=self._alert_metrics)
        self.recommendations = RecommendationEngine(
            db,
            registry=self.registry,
            pricing=self.tap.pricing_params,
            events_log=self.events,
            topology_latest=lambda base: (
                self.topology.latest(base, include_raw=True).get(base) or {}
            ),
        )
        self.discovery = DiscoveryPublisher(
            config,
            publish=self.publish,
            granted_bases=self._discovery_granted,
            discovery_prefix=self.registry.discovery_prefix_for,
            metrics=self._discovery_metrics,
            version=__version__,
        )
        self._knees_cache: tuple[float, dict[str, float]] | None = None
        self._cost_metrics_cache: tuple[float, dict] | None = None
        # Cost-ledger accumulators, drained by reference swap on the 10 s
        # flush (V2_PROPOSAL.md §V2-2): autonomous state publishes per
        # (instance, day, device), and zigbee-ninja's own mesh commands per
        # (instance, day, verb, group_target) so self spend stays on the
        # books (DESIGN.md P4).
        self._ledger_autonomous: dict[tuple[str, str, str], int] = {}
        self._ledger_self: dict[tuple[str, str, str, bool], int] = {}
        # Change-journal buffer (V2_PROPOSAL.md §V2-3), drained on the 10 s
        # flush; recent removals let a device_added on another instance be
        # recognized as a move between coordinators.
        self._journal_pending: list[tuple[float, str, str, str, str]] = []
        self._recent_removals: dict[str, tuple[float, str]] = {}
        self.loop_lag = LoopLagMonitor()
        self.loop_activity = LoopActivityLog()
        self._ha_link: HaLink | None = None
        self._ha_task: asyncio.Task | None = None
        self._ingest: MqttIngest | None = None
        self._ingest_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._loop_lag_task: asyncio.Task | None = None
        self._discovery_task: asyncio.Task | None = None

    def tap_token(self) -> str:
        """Long-lived token a ninja-tap agent presents to stream (generated once)."""
        token = self._config.get("tap_token")
        if not token:
            token = secrets.token_urlsafe(24)
            self._config.set("tap_token", token)
        return token

    def _resolve_members(self, instance: str, target: str) -> list[str]:
        return self.registry.group_members(instance, target)

    # Cross-instance moves complete within this window (remove from one
    # coordinator, rejoin on another is minutes to hours of user work).
    MOVE_MATCH_SECONDS = 24 * 3600.0

    def _on_registry_change(
        self, instance: str, kind: str, subject: str, detail: dict
    ) -> None:
        """Buffer a change-journal entry (V2_PROPOSAL.md §V2-3). A device
        added shortly after being removed from a different instance is
        annotated as a move between coordinators."""
        now = time.time()
        ieee = detail.get("ieee")
        if kind == "device_removed" and ieee:
            self._recent_removals[ieee] = (now, instance)
        elif kind == "device_added" and ieee:
            removal = self._recent_removals.pop(ieee, None)
            if removal is not None:
                removed_at, from_instance = removal
                if now - removed_at <= self.MOVE_MATCH_SECONDS and from_instance != instance:
                    detail = {**detail, "moved_from": from_instance}
        stale = [
            key
            for key, (ts, _from) in self._recent_removals.items()
            if now - ts > self.MOVE_MATCH_SECONDS
        ]
        for key in stale:
            del self._recent_removals[key]
        self._journal_pending.append((now, instance, kind, subject, json.dumps(detail)))

    def _on_probe_heartbeat(self, base: str, heartbeat: dict) -> None:
        # A sqlite write on the loop thread: it can wait on the flush
        # worker's transaction, so the activity log times it.
        with self.loop_activity.span("tile_heartbeat_write"):
            self.tiles.on_heartbeat(base, heartbeat)

    def _on_probe_device_seq(
        self, base: str, name: str, zcl_seq: int, probe_ts: float
    ) -> None:
        # The wire sees short addresses; probe events carry friendly names.
        # The registry join is the fusion key's other half (DESIGN.md §8).
        nwk = self.registry.network_address_for(base, name)
        if nwk is not None:
            self.fusion.on_probe(base, nwk, zcl_seq, probe_ts)

    def fusion_view(self) -> dict:
        """Fusion snapshot with unmatched senders resolved to friendly names."""
        view = self.fusion.snapshot()
        for base, entry in view.items():
            names = {
                device.get("network_address"): device.get("friendly_name")
                for device in self.registry.devices(base)
            }
            for row in entry.get("top_unmatched", []):
                row["name"] = names.get(row["nwk"])
        return view

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        """Publish on the ingest connection; self-attributed (DESIGN.md P4)."""
        if self._ingest is None:
            raise RuntimeError("Broker is not configured")
        await self._ingest.publish(topic, payload, retain=retain)
        base = self.registry.base_for(topic)
        self.class_rates.record(base or GLOBAL, "self")
        if base is not None:
            now = time.time()
            suffix = topic[len(base) + 1 :]
            self.events.record(now, "mqtt", base, "self", "out", suffix, len(payload))
            command = parse_command(suffix)
            if command is not None:
                # A mesh command of our own (benchmark reads): priced into the
                # ledger under the self commander at the next flush.
                target, verb = command
                key = (base, ledger.utc_day(now), verb, self.registry.is_group(base, target))
                self._ledger_self[key] = self._ledger_self.get(key, 0) + 1

    # -- lifecycle -----------------------------------------------------------

    def _upgrade_secrets(self) -> None:
        """Encrypt any plaintext secrets stored before §15 landed (idempotent)."""
        for key, field in (("broker", "password"), ("ha", "token")):
            data = self._config.get(key)
            if data and data.get(field) and not is_encrypted(data[field]):
                data = dict(data)
                data[field] = self._secrets.encrypt(data[field])
                self._config.set(key, data)

    def broker_config(self) -> BrokerConfig | None:
        data = self._config.get("broker")
        if not data:
            return None
        data = dict(data)
        data["password"] = self._secrets.decrypt(data.get("password"))
        return BrokerConfig.from_dict(data)

    async def start(self) -> None:
        # Seed-once mark of when ledger recording began: rate denominators
        # divide by recorded time, not by days the ledger never saw.
        if self._config.get("ledger_since") is None:
            self._config.set("ledger_since", time.time())
        self.loop_activity.install_gc()
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        self._loop_lag_task = asyncio.create_task(self._loop_lag_loop())
        await self.restart_ingest()
        await self.restart_ha()

    async def stop(self) -> None:
        self.loop_activity.remove_gc()
        await self.calibration.shutdown()  # abort any active benchmark run
        for task in (
            self._ingest_task,
            self._flush_task,
            self._ha_task,
            self._discovery_task,
            self._loop_lag_task,
        ):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ingest_task = None
        self._flush_task = None
        self._ha_task = None
        self._discovery_task = None
        self._loop_lag_task = None

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
        stored = dict(data)
        if stored.get("password"):
            stored["password"] = self._secrets.encrypt(stored["password"])
        self._config.set("broker", stored)
        await self.restart_ingest()

    # -- HA integration (per-automation attribution) ---------------------------

    def ha_config(self) -> HaConfig | None:
        data = self._config.get("ha")
        if not data:
            return None
        data = dict(data)
        data["token"] = self._secrets.decrypt(data.get("token"))
        if not data["token"]:
            return None  # undecryptable token (key replaced): re-enter in the GUI
        return HaConfig.from_dict(data)

    async def restart_ha(self) -> None:
        if self._ha_task is not None:
            self._ha_task.cancel()
            try:
                await self._ha_task
            except asyncio.CancelledError:
                pass
            self._ha_task = None
            self._ha_link = None
        config = self.ha_config()
        if config is not None:
            self._ha_link = HaLink(config, self.ha_attr, self._on_ha_publish)
            self._ha_task = asyncio.create_task(self._ha_link.run())

    async def apply_ha_config(self, data: dict) -> None:
        stored = dict(data)
        if stored.get("token"):
            stored["token"] = self._secrets.encrypt(stored["token"])
        self._config.set("ha", stored)
        await self.restart_ha()

    def ha_status(self) -> dict:
        if self._ha_link is None:
            return {"state": "unconfigured", "error": None, "connected_since": None}
        return {**self._ha_link.status, "counters": dict(self.ha_attr.counters)}

    def _on_ha_publish(self, topic: str, commander: str) -> None:
        """Backfill: HA told us who published `topic`; name any open chain."""
        base = self.registry.base_for(topic)
        if base is None:
            return
        command = parse_command(topic[len(base) + 1 :])
        if command is not None:
            self.chains.attribute_client(base, command[0], commander)

    # -- data path -----------------------------------------------------------

    def on_message(self, topic: str, payload: bytes) -> None:
        # The MQTT handler is the loop's hottest synchronous stretch; the
        # span lets a stall be attributed to (or ruled out of) it.
        with self.loop_activity.span("mqtt_message"):
            self._handle_message(topic, payload)

    def _handle_message(self, topic: str, payload: bytes) -> None:
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
        self.events.record(time.time(), "mqtt", base, kind, "in", suffix, len(payload))
        if kind == "probe":
            self.probes.handle(base, suffix, payload)
            return
        if kind == "bridge" and suffix.startswith("bridge/response/extension/"):
            action = suffix.rsplit("/", 1)[-1]
            with self.loop_activity.span("tile_bridge_response"):
                self.tiles.on_bridge_response(base, action, payload)
            return
        if kind == "bridge" and suffix == "bridge/response/networkmap":
            # Stores the raw network map: a large sqlite write on the loop.
            with self.loop_activity.span("topology_store_write"):
                self.topology.on_response(base, payload)
            return
        if kind == "bridge" and suffix == "bridge/logging":
            if self.calibration.active:
                self.calibration.on_bridge_log(base, payload)
            return
        if kind == "command":
            command = parse_command(suffix)
            if command is not None:
                target, verb = command
                if self.calibration.owns_command(base, target, verb):
                    # The benchmark's own reads: publish() already accounted
                    # them as `self`; a chain would misattribute them (P4).
                    return
                # HA attribution (automation name) beats broker client-id.
                client = self.ha_attr.name_for(topic) or self.brokerlog.client_for(topic)
                self.chains.on_command(base, target, verb, payload, client=client)
                self.class_rates.record(base, "commanded")
                if self.calibration.active:
                    self.calibration.note_ambient(base, "command")
        elif kind == "state":
            if self.calibration.active and self.calibration.on_state(base, suffix):
                self.class_rates.record(base, "self")  # reply to a benchmark read
                return
            klass = self.chains.on_state(base, suffix)
            self.class_rates.record(base, klass)
            if self.calibration.active:
                self.calibration.note_ambient(base, "state")
            if klass == "autonomous" and not self.registry.is_group(base, suffix):
                # Device-initiated report: the per-device side of the ledger
                # (echoes inside a chain window are priced on the chain).
                # Group state topics are Zigbee2MQTT's synthetic optimistic
                # state, not mesh frames, so they carry no airtime.
                key = (base, ledger.utc_day(time.time()), suffix)
                self._ledger_autonomous[key] = self._ledger_autonomous.get(key, 0) + 1
        elif kind == "availability" and self.calibration.active:
            self.calibration.on_availability(base, suffix, payload)

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

    # -- runtime settings (DESIGN.md §12) ------------------------------------------

    def _setting_int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self._config.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    def runtime_settings(self) -> dict:
        return {
            "retention_rollup_days": self._setting_int(
                "retention_rollup_days", DEFAULT_ROLLUP_RETENTION_DAYS, 1, 365
            ),
            "retention_chains_hours": self._setting_int(
                "retention_chains_hours", DEFAULT_CHAIN_RETENTION_HOURS, 1, 720
            ),
            "retention_topology_snapshots": self._setting_int(
                "retention_topology_snapshots", DEFAULT_TOPOLOGY_SNAPSHOTS, 1, 200
            ),
            "raw_event_quota_mb": self._setting_int("raw_event_quota_mb", 4096, 64, 65536),
            "raw_event_horizon_hours": self._setting_int(
                "raw_event_horizon_hours", 48, 1, 720
            ),
            "client_labels": dict(self._config.get("client_labels") or {}),
        }

    def apply_settings(self, data: dict) -> dict:
        for key in (
            "retention_rollup_days",
            "retention_chains_hours",
            "retention_topology_snapshots",
            "raw_event_quota_mb",
            "raw_event_horizon_hours",
        ):
            if key in data and data[key] is not None:
                try:
                    self._config.set(key, int(data[key]))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be an integer") from exc
        if data.get("client_labels") is not None:
            labels = data["client_labels"]
            if not isinstance(labels, dict):
                raise ValueError("client_labels must be a mapping")
            cleaned = {
                str(client).strip(): str(label).strip()
                for client, label in labels.items()
                if str(client).strip() and str(label).strip()
            }
            self._config.set("client_labels", cleaned)
        return self.runtime_settings()

    # -- HA discovery publisher (DESIGN.md §14) ------------------------------------

    def _discovery_granted(self) -> list[str]:
        return [
            instance["base_topic"]
            for instance in self.registry.snapshot()
            if self.tiles.is_granted(CAPABILITY_MQTT_DISCOVERY, instance["base_topic"])
        ]

    def _discovery_metrics(self, base: str) -> dict:
        """Headline metrics + alert state for one instance's HA entities.

        Runs on the event loop each publish cycle and reads sqlite (knee
        cache refresh, recommendation counts), so the activity log times it.
        """
        with self.loop_activity.span("discovery_metrics"):
            return self._discovery_metrics_inner(base)

    def _discovery_metrics_inner(self, base: str) -> dict:
        samples = self._alert_metrics(
            {"budget_pct", "knee_utilization_pct", "wire_p95_ms"}
        )
        rates = self.rates.snapshot().get(base)
        briefs = [
            alert
            for alert in self.alerts.active_brief()
            if alert["instance"] in (base, GLOBAL_INSTANCE)
        ]
        severity = None
        for level in ("critical", "warning", "info"):
            if any(alert["severity"] == level for alert in briefs):
                severity = level
                break
        return {
            "budget_pct": (samples.get("budget_pct") or {}).get(base),
            "knee_utilization_pct": (samples.get("knee_utilization_pct") or {}).get(base),
            "wire_p95_ms": (samples.get("wire_p95_ms") or {}).get(base),
            "msg_rate": (
                None if rates is None else round(rates.get("total_60s", 0) / 60.0, 2)
            ),
            "recommendations_open": self.recommendations.store.counts()[
                "open_by_instance"
            ].get(base, 0),
            "alerts": [alert["name"] for alert in briefs],
            "severity": severity,
        }

    async def _discovery_loop(self) -> None:
        while True:
            await asyncio.sleep(PUBLISH_INTERVAL_SECONDS)
            try:
                await self.discovery.publish_cycle()
            except Exception:
                # Broker down or reconnecting; the next cycle retries.
                pass

    # -- alert metrics (DESIGN.md §14) -------------------------------------------

    def _alert_knees(self) -> dict[str, float]:
        """Headline knee eps per instance (spread preferred over single, like
        the headroom view), cached ~60 s: knees only change when a
        calibration completes."""
        now = time.time()
        if self._knees_cache is None or now - self._knees_cache[0] > 60.0:
            knees: dict[str, float] = {}
            for instance, modes in headroom_model.latest_knees(self._db).items():
                knee = modes.get("spread") or modes.get("single")
                if knee and knee.get("eps"):
                    knees[instance] = float(knee["eps"])
            self._knees_cache = (now, knees)
        return self._knees_cache[1]

    COST_METRIC_NAMES = frozenset(
        {
            "commander_cost_ratio",
            "device_cost_ratio",
            "commander_cost_us_per_s",
            "instance_cost_us_per_s",
        }
    )

    def _ledger_cost_metrics(self) -> dict:
        """Ledger-derived samples, cached ~60 s: daily rollups move slowly and
        the evaluator ticks every 10 s."""
        now = time.time()
        if self._cost_metrics_cache is None or now - self._cost_metrics_cache[0] > 60.0:
            self._cost_metrics_cache = (now, ledger.cost_metrics(self._db, now))
        return self._cost_metrics_cache[1]

    def _alert_metrics(self, names: set[str]) -> dict[str, dict[str, float | None]]:
        """Samples for the alert evaluator. Global metrics report under '*';
        counter-kind metrics report cumulative totals (the evaluator
        differences ticks). An unconfigured link reports nothing at all, so
        its rules stay frozen rather than alerting on a feature never set up."""
        out: dict[str, dict[str, float | None]] = {}
        if "broker_connected" in names:
            state = self.ingest_status()["state"]
            if state != "unconfigured":
                out["broker_connected"] = {
                    GLOBAL_INSTANCE: 1.0 if state == "connected" else 0.0
                }
        if "ha_connected" in names:
            state = self.ha_status()["state"]
            if state != "unconfigured":
                out["ha_connected"] = {GLOBAL_INSTANCE: 1.0 if state == "connected" else 0.0}
        if "tap_agents" in names:
            out["tap_agents"] = {GLOBAL_INSTANCE: float(len(self.tap.agents))}
        if "collector_loop_lag_ms" in names:
            out["collector_loop_lag_ms"] = {
                GLOBAL_INSTANCE: self.loop_lag.max_window_ms()
            }
        if "probe_heartbeat_age_s" in names:
            now = time.time()
            probe_stats = self.probes.stats()
            ages: dict[str, float | None] = {}
            rows = self._db.connect().execute(
                "SELECT target, last_health_at, deployed_at FROM tiles "
                "WHERE capability = ? AND status = 'deployed'",
                (CAPABILITY_Z2M_EXTENSION,),
            ).fetchall()
            for row in rows:
                # In-memory heartbeat first; the persisted last_health_at
                # covers the stretch right after a collector restart.
                beat = probe_stats.get(row["target"], {}).get("last_heartbeat_at")
                beat = beat or row["last_health_at"] or row["deployed_at"]
                ages[row["target"]] = max(0.0, now - beat) if beat else None
            out["probe_heartbeat_age_s"] = ages
        if "seq_gaps_delta" in names:
            out["seq_gaps_delta"] = {
                base: float(stat.get("seq_gaps", 0))
                for base, stat in self.probes.stats().items()
            }
        if {"layout_mismatch_delta", "delivery_failed_delta", "avg_tx"} & names:
            totals = self.tap.instance_wire_totals()
            if "layout_mismatch_delta" in names:
                out["layout_mismatch_delta"] = {
                    instance: float(t["layout_mismatch"]) for instance, t in totals.items()
                }
            if "delivery_failed_delta" in names:
                out["delivery_failed_delta"] = {
                    instance: float(t["delivery_failed"]) for instance, t in totals.items()
                }
            if "avg_tx" in names:
                out["avg_tx"] = {
                    instance: t["avg_tx"]
                    for instance, t in totals.items()
                    if t["avg_tx"] is not None
                }
        if "wire_p95_ms" in names:
            out["wire_p95_ms"] = {
                instance: float(view["p95_ms"])
                for instance, view in self.tap.latency.snapshot().items()
            }
        if self.COST_METRIC_NAMES & names:
            for name, samples in self._ledger_cost_metrics().items():
                if name in names:
                    out[name] = samples
        if {"budget_pct", "load_eps", "knee_utilization_pct", "steady_headroom_eps"} & names:
            snapshot = self.tap.airtime.snapshot()
            loads = {
                instance: sum(
                    view["buckets"].get(bucket, {}).get("frames_60s", 0)
                    for bucket in TX_BUCKETS
                )
                / 60.0
                for instance, view in snapshot.items()
            }
            if "budget_pct" in names:
                out["budget_pct"] = {
                    instance: view["budget_pct_60s"] for instance, view in snapshot.items()
                }
            if "load_eps" in names:
                out["load_eps"] = {instance: round(v, 2) for instance, v in loads.items()}
            if {"knee_utilization_pct", "steady_headroom_eps"} & names:
                # Iterate calibrated instances: an instance with a knee but no
                # recent TX is at zero load, not unknown.
                utilization: dict[str, float | None] = {}
                headroom: dict[str, float | None] = {}
                for instance, knee in self._alert_knees().items():
                    load = loads.get(instance, 0.0)
                    utilization[instance] = round(load / knee * 100.0, 1)
                    headroom[instance] = round(knee - load, 2)
                if "knee_utilization_pct" in names:
                    out["knee_utilization_pct"] = utilization
                if "steady_headroom_eps" in names:
                    out["steady_headroom_eps"] = headroom
        return out

    # -- rollups & persistence -------------------------------------------------

    def flush_rollups(self) -> int:
        conn = self._db.connect()
        now = int(time.time())
        written = 0
        settings = self.runtime_settings()
        rollup_cutoff = now - settings["retention_rollup_days"] * 86400
        chain_cutoff = now - settings["retention_chains_hours"] * 3600

        rows = self.rates.drain_completed_windows()
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO series_10s (ts, instance, kind, count) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.execute("DELETE FROM series_10s WHERE ts < ?", (rollup_cutoff,))
            written += len(rows)
            conn.commit()

        class_rows = self.class_rates.drain_completed_windows()
        if class_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO attribution_10s (ts, instance, klass, count) "
                "VALUES (?, ?, ?, ?)",
                class_rows,
            )
            conn.execute("DELETE FROM attribution_10s WHERE ts < ?", (rollup_cutoff,))
            written += len(class_rows)
            conn.commit()

        airtime_rows = self.tap.airtime.drain_completed_windows()
        if airtime_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO airtime_10s (ts, instance, bucket, airtime_us, frames) "
                "VALUES (?, ?, ?, ?, ?)",
                airtime_rows,
            )
            conn.execute("DELETE FROM airtime_10s WHERE ts < ?", (rollup_cutoff,))
            written += len(airtime_rows)
            conn.commit()

        latency_rows = self.tap.latency.drain_completed_windows()
        if latency_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO latency_10s (ts, instance, count, p50_ms, p95_ms, max_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                latency_rows,
            )
            conn.execute("DELETE FROM latency_10s WHERE ts < ?", (rollup_cutoff,))
            written += len(latency_rows)
            conn.commit()

        finalized = self.chains.drain_finalized()
        if finalized:
            conn.executemany(
                "INSERT INTO chains (instance, target, verb, opened_at, client, "
                "payload_size, echo_count, first_echo_ms, redundant, payload_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        chain.payload_digest,
                    )
                    for chain in finalized
                ],
            )
            conn.execute("DELETE FROM chains WHERE opened_at < ?", (chain_cutoff,))
            written += len(finalized)
            conn.commit()

        written += self._flush_ledger(conn, finalized)
        conn.commit()

        journal_pending, self._journal_pending = self._journal_pending, []
        if journal_pending:
            conn.executemany(
                "INSERT INTO journal (ts, instance, kind, subject, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                journal_pending,
            )
            conn.execute(
                "DELETE FROM journal WHERE ts < ?",
                (time.time() - JOURNAL_RETENTION_DAYS * 86400,),
            )
            written += len(journal_pending)

        if written:
            conn.commit()
        return written

    def _flush_ledger(self, conn, finalized: list[Chain]) -> int:
        """Price finalized chains, self commands, and autonomous publishes
        into the daily cost ledger (V2_PROPOSAL.md §V2-2). Units are µs;
        every row carries provenance plus the pricing parameters in force."""
        params_cache: dict[str, tuple[int, float | None, float | None]] = {}

        def context(instance: str) -> tuple[int, float | None, float | None]:
            if instance not in params_cache:
                avg_tx, retry_rate = self.tap.pricing_params(instance)
                params_cache[instance] = (
                    self.registry.router_count_for(instance),
                    avg_tx,
                    retry_rate,
                )
            return params_cache[instance]

        hops_cache: dict[str, dict[str, int]] = {}
        coordinators = {
            row["base_topic"]: row.get("coordinator_ieee")
            for row in self.registry.snapshot()
        }

        def target_hops(instance: str, target: str) -> int:
            """Route depth for a unicast target (§10's hop term).

            Cached for the whole flush pass: topology snapshots change on the
            order of hours while chains arrive on the order of milliseconds,
            so re-deriving per chain would be pure waste. A target the map
            cannot place takes the conservative default rather than the
            cheapest assumption.
            """
            depths = hops_cache.get(instance)
            if depths is None:
                entry = self.topology.latest(instance, include_raw=True).get(instance) or {}
                raw = entry.get("raw")
                depths = (
                    hop_model.depths_by_name(
                        topology_graph(raw), coordinators.get(instance)
                    )
                    if raw
                    else {}
                )
                hops_cache[instance] = depths
            return depths.get(target, airtime.DEFAULT_UNKNOWN_HOPS)

        rows: dict[tuple[str, str, str], dict] = {}

        def accumulate(
            instance: str, day: str, commander: str, price: ledger.ChainPrice, count: int
        ) -> None:
            n_routers, avg_tx, retry_rate = context(instance)
            entry = rows.setdefault(
                (instance, day, commander), {"chains": 0, "tx_us": 0.0, "rx_us": 0.0}
            )
            entry["chains"] += count
            entry["tx_us"] += price.tx_us * count
            entry["rx_us"] += price.rx_us * count
            entry["provenance"] = price.provenance
            entry["params"] = ledger.instance_params(
                n_routers, avg_tx, retry_rate, bool(hops_cache.get(instance))
            )

        for chain in finalized:
            n_routers, avg_tx, retry_rate = context(chain.instance)
            group_target = self.registry.is_group(chain.instance, chain.target)
            price = ledger.price_chain(
                verb=chain.verb,
                group_target=group_target,
                n_routers=n_routers,
                echo_count=chain.echoes,
                avg_tx=avg_tx,
                retry_rate=retry_rate,
                hops=1 if group_target else target_hops(chain.instance, chain.target),
            )
            day = ledger.utc_day(chain.opened_at)
            commander = chain.client or ledger.UNATTRIBUTED
            accumulate(chain.instance, day, commander, price, 1)

        self_pending, self._ledger_self = self._ledger_self, {}
        for (instance, day, verb, group_target), count in self_pending.items():
            n_routers, avg_tx, retry_rate = context(instance)
            # Self traffic aggregates by (verb, group_target) without keeping
            # the target, so there is no route to price: it takes the
            # coordinator hop. This under-counts our own probe reads slightly,
            # which is the right direction for a self-attributed number.
            price = ledger.price_chain(
                verb=verb,
                group_target=group_target,
                n_routers=n_routers,
                echo_count=0,
                avg_tx=avg_tx,
                retry_rate=retry_rate,
            )
            accumulate(instance, day, ledger.SELF_COMMANDER, price, count)

        written = 0
        if rows:
            conn.executemany(
                "INSERT INTO ledger_daily "
                "(instance, day, commander, chains, tx_us, rx_us, provenance, params, "
                "pricing_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance, day, commander) DO UPDATE SET "
                "chains = chains + excluded.chains, "
                "tx_us = tx_us + excluded.tx_us, "
                "rx_us = rx_us + excluded.rx_us, "
                "provenance = excluded.provenance, "
                "params = excluded.params, "
                # A day that accumulated under two cost models is not a
                # comparable quantity; record that rather than letting the
                # last writer's version speak for the whole row.
                "pricing_version = CASE WHEN pricing_version = excluded.pricing_version "
                "THEN pricing_version ELSE ? END",
                [
                    (
                        instance,
                        day,
                        commander,
                        entry["chains"],
                        entry["tx_us"],
                        entry["rx_us"],
                        entry["provenance"],
                        json.dumps(entry["params"]),
                        ledger.PRICING_MODEL_VERSION,
                        ledger.MIXED_PRICING_VERSION,
                    )
                    for (instance, day, commander), entry in rows.items()
                ],
            )
            written += len(rows)

        auto_pending, self._ledger_autonomous = self._ledger_autonomous, {}
        if auto_pending:
            unit_us = ledger.autonomous_publish_cost_us()
            conn.executemany(
                "INSERT INTO ledger_device_daily "
                "(instance, day, device, publishes, autonomous_us, provenance, "
                "pricing_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance, day, device) DO UPDATE SET "
                "publishes = publishes + excluded.publishes, "
                "autonomous_us = autonomous_us + excluded.autonomous_us, "
                "provenance = excluded.provenance, "
                "pricing_version = CASE WHEN pricing_version = excluded.pricing_version "
                "THEN pricing_version ELSE ? END",
                [
                    (
                        instance,
                        day,
                        device,
                        publishes,
                        publishes * unit_us,
                        ledger.AUTONOMOUS_PROVENANCE,
                        ledger.AUTONOMOUS_PRICING_MODEL_VERSION,
                        ledger.MIXED_PRICING_VERSION,
                    )
                    for (instance, day, device), publishes in auto_pending.items()
                ],
            )
            written += len(auto_pending)

        if written:
            cutoff_day = ledger.utc_day(time.time() - ledger.RETENTION_DAYS * 86400)
            conn.execute("DELETE FROM ledger_daily WHERE day < ?", (cutoff_day,))
            conn.execute("DELETE FROM ledger_device_daily WHERE day < ?", (cutoff_day,))
        return written

    def _flush_and_tick(self) -> None:
        """The 10 s storage pass, run on one worker thread: the flush and the
        alert tick share that thread so they can never collide on the write
        lock, and neither ever blocks the event loop (their commits once
        stalled it for seconds and distorted every time-sensitive consumer
        sharing it: the calibration pacer above all; that is how the meter
        once measured itself)."""
        # The worker_* spans time work that runs off the loop: a slow flush
        # here beside a clean stall record is the decoupling working.
        try:
            with self.loop_activity.span("worker_rollup_flush"):
                self.flush_rollups()
        except Exception:
            # Never let a storage hiccup kill the pass; next tick retries.
            pass
        try:
            self.alerts.tick()
        except Exception:
            pass
        try:
            settings = self.runtime_settings()
            with self.loop_activity.span("worker_events_flush"):
                self.events.flush(
                    quota_mb=settings["raw_event_quota_mb"],
                    horizon_hours=settings["raw_event_horizon_hours"],
                )
        except Exception:
            pass

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(ROLLUP_SECONDS)
            try:
                await asyncio.to_thread(self._flush_and_tick)
            except Exception:
                pass
            try:
                # Detector pass on its own slow cadence, off the event loop:
                # store reads can take a second or two on a full window, and
                # its GIL-bound stretches still jitter the loop, so it defers
                # while a calibration run needs the pacer undisturbed.
                if self.recommendations.due() and not self.calibration.active:
                    await asyncio.to_thread(self.recommendations.run)
            except Exception:
                pass

    async def _loop_lag_loop(self) -> None:
        while True:
            before = time.monotonic()
            await asyncio.sleep(LOOP_LAG_SAMPLE_SECONDS)
            self.loop_lag.record(
                time.monotonic() - before - LOOP_LAG_SAMPLE_SECONDS
            )
