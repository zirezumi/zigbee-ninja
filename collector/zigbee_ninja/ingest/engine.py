"""Wires broker config → MQTT ingest → registry, rates, and attribution."""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from .. import __version__
from ..alerts import GLOBAL_INSTANCE, AlertManager
from ..attribution.chains import Chain, ChainTracker, parse_command
from ..calibration.benchmark import CalibrationManager
from ..capacity import headroom as headroom_model
from ..capacity import ledger
from ..capacity.headroom import TX_BUCKETS
from ..ha_discovery import PUBLISH_INTERVAL_SECONDS, DiscoveryPublisher
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

# Retention defaults (DESIGN.md §12); settings-backed knobs override at runtime.
DEFAULT_ROLLUP_RETENTION_DAYS = 14  # 10s tiers
DEFAULT_CHAIN_RETENTION_HOURS = 48  # chain detail
DEFAULT_TOPOLOGY_SNAPSHOTS = 20  # per instance


class Engine:
    def __init__(
        self, db: Database, config: ConfigStore, secrets: SecretBox, events: RawEventLog
    ):
        self._db = db
        self._config = config
        self._secrets = secrets
        self.events = events
        self._upgrade_secrets()
        self.registry = Registry()
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
        )
        self.ha_attr = HaAttribution()
        self.alerts = AlertManager(db, config, provider=self._alert_metrics)
        self.discovery = DiscoveryPublisher(
            config,
            publish=self.publish,
            granted_bases=self._discovery_granted,
            discovery_prefix=self.registry.discovery_prefix_for,
            metrics=self._discovery_metrics,
            version=__version__,
        )
        self._knees_cache: tuple[float, dict[str, float]] | None = None
        # Cost-ledger accumulators, drained by reference swap on the 10 s
        # flush (V2_PROPOSAL.md §V2-2): autonomous state publishes per
        # (instance, day, device), and zigbee-ninja's own mesh commands per
        # (instance, day, verb, group_target) so self spend stays on the
        # books (DESIGN.md P4).
        self._ledger_autonomous: dict[tuple[str, str, str], int] = {}
        self._ledger_self: dict[tuple[str, str, str, bool], int] = {}
        self._ha_link: HaLink | None = None
        self._ha_task: asyncio.Task | None = None
        self._ingest: MqttIngest | None = None
        self._ingest_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
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

    def _on_probe_heartbeat(self, base: str, heartbeat: dict) -> None:
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
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._discovery_task = asyncio.create_task(self._discovery_loop())
        await self.restart_ingest()
        await self.restart_ha()

    async def stop(self) -> None:
        await self.calibration.shutdown()  # abort any active benchmark run
        for task in (self._ingest_task, self._flush_task, self._ha_task, self._discovery_task):
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
            self.tiles.on_bridge_response(base, action, payload)
            return
        if kind == "bridge" and suffix == "bridge/response/networkmap":
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
        elif kind == "state":
            if self.calibration.active and self.calibration.on_state(base, suffix):
                self.class_rates.record(base, "self")  # reply to a benchmark read
                return
            klass = self.chains.on_state(base, suffix)
            self.class_rates.record(base, klass)
            if klass == "autonomous":
                # Device-initiated report: the per-device side of the ledger
                # (echoes inside a chain window are priced on the chain).
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
        """Headline metrics + alert state for one instance's HA entities."""
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

        class_rows = self.class_rates.drain_completed_windows()
        if class_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO attribution_10s (ts, instance, klass, count) "
                "VALUES (?, ?, ?, ?)",
                class_rows,
            )
            conn.execute("DELETE FROM attribution_10s WHERE ts < ?", (rollup_cutoff,))
            written += len(class_rows)

        airtime_rows = self.tap.airtime.drain_completed_windows()
        if airtime_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO airtime_10s (ts, instance, bucket, airtime_us, frames) "
                "VALUES (?, ?, ?, ?, ?)",
                airtime_rows,
            )
            conn.execute("DELETE FROM airtime_10s WHERE ts < ?", (rollup_cutoff,))
            written += len(airtime_rows)

        latency_rows = self.tap.latency.drain_completed_windows()
        if latency_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO latency_10s (ts, instance, count, p50_ms, p95_ms, max_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                latency_rows,
            )
            conn.execute("DELETE FROM latency_10s WHERE ts < ?", (rollup_cutoff,))
            written += len(latency_rows)

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
            conn.execute("DELETE FROM chains WHERE opened_at < ?", (chain_cutoff,))
            written += len(finalized)

        written += self._flush_ledger(conn, finalized)

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
            entry["params"] = ledger.instance_params(n_routers, avg_tx, retry_rate)

        for chain in finalized:
            n_routers, avg_tx, retry_rate = context(chain.instance)
            price = ledger.price_chain(
                verb=chain.verb,
                group_target=self.registry.is_group(chain.instance, chain.target),
                n_routers=n_routers,
                echo_count=chain.echoes,
                avg_tx=avg_tx,
                retry_rate=retry_rate,
            )
            day = ledger.utc_day(chain.opened_at)
            commander = chain.client or ledger.UNATTRIBUTED
            accumulate(chain.instance, day, commander, price, 1)

        self_pending, self._ledger_self = self._ledger_self, {}
        for (instance, day, verb, group_target), count in self_pending.items():
            n_routers, avg_tx, retry_rate = context(instance)
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
                "(instance, day, commander, chains, tx_us, rx_us, provenance, params) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance, day, commander) DO UPDATE SET "
                "chains = chains + excluded.chains, "
                "tx_us = tx_us + excluded.tx_us, "
                "rx_us = rx_us + excluded.rx_us, "
                "provenance = excluded.provenance, "
                "params = excluded.params",
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
                "(instance, day, device, publishes, autonomous_us, provenance) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance, day, device) DO UPDATE SET "
                "publishes = publishes + excluded.publishes, "
                "autonomous_us = autonomous_us + excluded.autonomous_us, "
                "provenance = excluded.provenance",
                [
                    (
                        instance,
                        day,
                        device,
                        publishes,
                        publishes * unit_us,
                        ledger.AUTONOMOUS_PROVENANCE,
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

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(ROLLUP_SECONDS)
            try:
                self.flush_rollups()
            except Exception:
                # Never let a storage hiccup kill the loop; next tick retries.
                pass
            try:
                self.alerts.tick()
            except Exception:
                pass
            try:
                settings = self.runtime_settings()
                self.events.flush(
                    quota_mb=settings["raw_event_quota_mb"],
                    horizon_hours=settings["raw_event_horizon_hours"],
                )
            except Exception:
                pass
