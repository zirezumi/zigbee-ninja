"""Alert rules + evaluator (DESIGN.md §14).

Threshold rules over first-class metrics, evaluated on the engine's 10 s
rollup cadence. Each (rule, instance) pair runs an independent state machine:

- **open**: the condition holds continuously for ``sustain_seconds``;
- **clear**: the value stays on the OK side of ``clear_threshold`` (default:
  the threshold itself) continuously for ``max(sustain_seconds, 60 s)``; the
  floor keeps zero-sustain rules (counter deltas) from flapping every tick;
- **freeze**: a metric with no current value (undeployed probe, unconfigured
  HA link, no tap coverage) neither opens nor clears anything.

Metric samples come from a provider callable ``(metric_names) -> {metric:
{instance: value}}``; global metrics report under the ``'*'`` instance.
``counter``-kind metrics report cumulative totals and the evaluator
differences consecutive ticks: first sight baselines at zero and a decrease
rebaselines, so collector restarts never alert retroactively.

Built-in rules seed exactly once (tracked in settings), so deleting or
disabling one is durable across restarts. Self-health rules ship enabled:
they only fire when a foothold the user deployed stops reporting or a
configured link drops; capacity rules ship disabled until the user opts in
with thresholds that fit the installation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from .store.config import ConfigStore
from .store.db import Database

GLOBAL_INSTANCE = "*"  # instance key for whole-service metrics
SEVERITIES = ("info", "warning", "critical")
OPS = (">", "<")
CLEAR_MIN_SUSTAIN_SECONDS = 60.0
EVENT_RETENTION_SECONDS = 90 * 24 * 3600
HISTORY_LIMIT = 500

# kind "gauge": the provider reports the current value. kind "counter": the
# provider reports a cumulative total; the evaluator differences ticks and
# evaluates the per-tick delta.
METRICS: dict[str, dict] = {
    "broker_connected": {
        "scope": "global",
        "kind": "gauge",
        "unit": "0/1",
        "description": "MQTT broker link (1 = connected; absent until configured)",
    },
    "ha_connected": {
        "scope": "global",
        "kind": "gauge",
        "unit": "0/1",
        "description": "Home Assistant link (1 = connected; absent until configured)",
    },
    "tap_agents": {
        "scope": "global",
        "kind": "gauge",
        "unit": "agents",
        "description": "Connected ninja-tap capture agents",
    },
    "probe_heartbeat_age_s": {
        "scope": "instance",
        "kind": "gauge",
        "unit": "s",
        "description": "Seconds since the Z2M extension probe's last heartbeat (deployed only)",
    },
    "seq_gaps_delta": {
        "scope": "instance",
        "kind": "counter",
        "unit": "events",
        "description": "Probe telemetry sequence gaps since the previous evaluation",
    },
    "layout_mismatch_delta": {
        "scope": "instance",
        "kind": "counter",
        "unit": "frames",
        "description": "Wire frames failing the EZSP layout self-check since the previous "
        "evaluation",
    },
    "delivery_failed_delta": {
        "scope": "instance",
        "kind": "counter",
        "unit": "frames",
        "description": "APS delivery failures on the wire since the previous evaluation",
    },
    "wire_p95_ms": {
        "scope": "instance",
        "kind": "gauge",
        "unit": "ms",
        "description": "Wire-tier p95 command latency (sendUnicast → messageSentHandler)",
    },
    "budget_pct": {
        "scope": "instance",
        "kind": "gauge",
        "unit": "%",
        "description": "Channel airtime budget consumed (60 s window)",
    },
    "load_eps": {
        "scope": "instance",
        "kind": "gauge",
        "unit": "eps",
        "description": "Coordinator TX frames per second (60 s window)",
    },
    "knee_utilization_pct": {
        "scope": "instance",
        "kind": "gauge",
        "unit": "%",
        "description": "Load as a share of the calibrated capacity limit (60 s window)",
    },
    "steady_headroom_eps": {
        "scope": "instance",
        "kind": "gauge",
        "unit": "eps",
        "description": "Calibrated capacity limit minus current load (60 s window)",
    },
    "avg_tx": {
        "scope": "instance",
        "kind": "gauge",
        "unit": "x",
        "description": "Measured broadcast retransmission factor (passive avg_tx)",
    },
    # Cost-ledger metrics (V2_PROPOSAL.md §V2-4). Rules on commander/device
    # scopes key their state machines on commander or device names rather
    # than instance base topics; '*' watches every spender the ledger knows.
    "commander_cost_ratio": {
        "scope": "commander",
        "kind": "gauge",
        "unit": "x",
        "description": "Trailing 24 h airtime cost vs the commander's 14-day median "
        "(absent until 3 completed days of ledger history)",
    },
    "device_cost_ratio": {
        "scope": "device",
        "kind": "gauge",
        "unit": "x",
        "description": "Trailing 24 h reporting cost vs the device's 14-day median "
        "(absent until 3 completed days of ledger history)",
    },
    "commander_cost_us_per_s": {
        "scope": "commander",
        "kind": "gauge",
        "unit": "µs/s",
        "description": "Trailing 24 h airtime spend rate per commander, for explicit "
        "cost budgets",
    },
    "instance_cost_us_per_s": {
        "scope": "instance",
        "kind": "gauge",
        "unit": "µs/s",
        "description": "Trailing 24 h ledger cost rate per coordinator (commands plus "
        "device reporting)",
    },
}

SEED_RULES: list[dict] = [
    {
        "builtin": "probe_stale",
        "name": "Extension probe heartbeat stale",
        "metric": "probe_heartbeat_age_s",
        "instance": "*",
        "op": ">",
        "threshold": 90.0,
        "clear_threshold": 45.0,
        "sustain_seconds": 60,
        "severity": "warning",
        "enabled": 1,
    },
    {
        "builtin": "tap_down",
        "name": "Wire-tap agent disconnected",
        "metric": "tap_agents",
        "instance": "*",
        "op": "<",
        "threshold": 1.0,
        "clear_threshold": None,
        # A collector redeploy costs the tap 30–60 s of reconnect; the sustain
        # keeps routine restarts out of the alert history.
        "sustain_seconds": 120,
        "severity": "warning",
        "enabled": 1,
    },
    {
        "builtin": "broker_down",
        "name": "MQTT broker disconnected",
        "metric": "broker_connected",
        "instance": "*",
        "op": "<",
        "threshold": 1.0,
        "clear_threshold": None,
        "sustain_seconds": 30,
        "severity": "critical",
        "enabled": 1,
    },
    {
        "builtin": "ha_down",
        "name": "Home Assistant link down",
        "metric": "ha_connected",
        "instance": "*",
        "op": "<",
        "threshold": 1.0,
        "clear_threshold": None,
        "sustain_seconds": 60,
        "severity": "warning",
        "enabled": 1,
    },
    {
        "builtin": "layout_mismatch",
        "name": "EZSP layout mismatch",
        "metric": "layout_mismatch_delta",
        "instance": "*",
        "op": ">",
        "threshold": 0.0,
        "clear_threshold": None,
        "sustain_seconds": 0,
        "severity": "critical",
        "enabled": 1,
    },
    {
        "builtin": "knee_utilization",
        "name": "Capacity utilization high",
        "metric": "knee_utilization_pct",
        "instance": "*",
        "op": ">",
        "threshold": 60.0,
        "clear_threshold": 45.0,
        "sustain_seconds": 120,
        "severity": "warning",
        "enabled": 0,
    },
    {
        "builtin": "steady_headroom",
        "name": "Steady headroom low",
        "metric": "steady_headroom_eps",
        "instance": "*",
        "op": "<",
        "threshold": 5.0,
        "clear_threshold": 8.0,
        "sustain_seconds": 300,
        "severity": "warning",
        "enabled": 0,
    },
    {
        "builtin": "wire_p95",
        "name": "Wire p95 latency elevated",
        "metric": "wire_p95_ms",
        "instance": "*",
        "op": ">",
        "threshold": 500.0,
        "clear_threshold": 300.0,
        "sustain_seconds": 120,
        "severity": "warning",
        "enabled": 0,
    },
    {
        "builtin": "budget_pct",
        "name": "Channel airtime budget high",
        "metric": "budget_pct",
        "instance": "*",
        "op": ">",
        "threshold": 20.0,
        "clear_threshold": 15.0,
        "sustain_seconds": 300,
        "severity": "warning",
        "enabled": 0,
    },
    {
        "builtin": "delivery_failures",
        "name": "APS delivery failures",
        "metric": "delivery_failed_delta",
        "instance": "*",
        "op": ">",
        "threshold": 0.0,
        "clear_threshold": None,
        "sustain_seconds": 0,
        "severity": "warning",
        "enabled": 0,
    },
    {
        "builtin": "seq_gaps",
        "name": "Probe telemetry sequence gaps",
        "metric": "seq_gaps_delta",
        "instance": "*",
        "op": ">",
        "threshold": 0.0,
        "clear_threshold": None,
        "sustain_seconds": 0,
        "severity": "info",
        "enabled": 0,
    },
    {
        "builtin": "avg_tx_high",
        "name": "Broadcast retry factor high",
        "metric": "avg_tx",
        "instance": "*",
        "op": ">",
        "threshold": 2.2,
        "clear_threshold": 1.8,
        "sustain_seconds": 600,
        "severity": "info",
        "enabled": 0,
    },
    # V2 cost regression + budget rules (§V2-10: loose defaults, 2x over the
    # 14-day median sustained 24 h; seeded disabled like all capacity rules).
    {
        "builtin": "commander_cost_regression",
        "name": "Commander cost regression",
        "metric": "commander_cost_ratio",
        "instance": "*",
        "op": ">",
        "threshold": 2.0,
        "clear_threshold": 1.5,
        "sustain_seconds": 86400,
        "severity": "warning",
        "enabled": 0,
    },
    {
        "builtin": "device_cost_regression",
        "name": "Device cost regression",
        "metric": "device_cost_ratio",
        "instance": "*",
        "op": ">",
        "threshold": 2.0,
        "clear_threshold": 1.5,
        "sustain_seconds": 86400,
        "severity": "warning",
        "enabled": 0,
    },
    {
        "builtin": "commander_cost_budget",
        "name": "Commander cost budget",
        "metric": "commander_cost_us_per_s",
        "instance": "*",
        "op": ">",
        "threshold": 2000.0,
        "clear_threshold": 1500.0,
        "sustain_seconds": 3600,
        "severity": "warning",
        "enabled": 0,
    },
    {
        "builtin": "instance_cost_budget",
        "name": "Coordinator cost budget",
        "metric": "instance_cost_us_per_s",
        "instance": "*",
        "op": ">",
        "threshold": 35000.0,
        "clear_threshold": 28000.0,
        "sustain_seconds": 3600,
        "severity": "warning",
        "enabled": 0,
    },
]

_RULE_FIELDS = (
    "name",
    "metric",
    "instance",
    "op",
    "threshold",
    "clear_threshold",
    "sustain_seconds",
    "severity",
    "enabled",
)

MetricsProvider = Callable[[set[str]], dict[str, dict[str, float | None]]]


def metric_catalog() -> list[dict]:
    return [{"metric": name, **info} for name, info in METRICS.items()]


def _validate(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("Rule name must be 1-120 characters")
    metric = data.get("metric")
    if metric not in METRICS:
        raise ValueError(f"Unknown metric: {metric}")
    instance = str(data.get("instance") or "*").strip() or "*"
    if METRICS[metric]["scope"] == "global" and instance != GLOBAL_INSTANCE:
        raise ValueError(f"Metric {metric} is service-wide; use instance '*'")
    op = data.get("op")
    if op not in OPS:
        raise ValueError("Operator must be '>' or '<'")
    try:
        threshold = float(data["threshold"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Threshold must be a number") from exc
    clear = data.get("clear_threshold")
    if clear is not None:
        clear = float(clear)
        # The clear threshold must sit on the OK side of the open threshold,
        # or hysteresis inverts and the rule can never clear.
        if (op == ">" and clear > threshold) or (op == "<" and clear < threshold):
            raise ValueError("clear_threshold must be on the OK side of threshold")
    sustain = int(data.get("sustain_seconds", 60))
    if not 0 <= sustain <= 86400:
        raise ValueError("sustain_seconds must be between 0 and 86400")
    severity = data.get("severity", "warning")
    if severity not in SEVERITIES:
        raise ValueError(f"Severity must be one of {', '.join(SEVERITIES)}")
    return {
        "name": name,
        "metric": metric,
        "instance": instance,
        "op": op,
        "threshold": threshold,
        "clear_threshold": clear,
        "sustain_seconds": sustain,
        "severity": severity,
        "enabled": 1 if data.get("enabled", True) else 0,
    }


@dataclass
class _PairState:
    event_id: int | None = None
    opened_at: float | None = None
    peak: float | None = None
    last_value: float | None = None
    breach_since: float | None = None
    ok_since: float | None = None


class AlertManager:
    def __init__(
        self,
        db: Database,
        config: ConfigStore,
        provider: MetricsProvider,
        clock: Callable[[], float] = time.time,
    ):
        self._db = db
        self._config = config
        self._provider = provider
        self._clock = clock
        self._states: dict[tuple[int, str], _PairState] = {}
        self._deltas: dict[tuple[str, str], float] = {}
        self._rules_by_id: dict[int, dict] = {}
        self._seed()
        self._reattach()
        self._refresh_rules()

    # -- rules ------------------------------------------------------------------

    def _seed(self) -> None:
        seeded = set(self._config.get("alert_rules_seeded") or [])
        missing = [seed for seed in SEED_RULES if seed["builtin"] not in seeded]
        if not missing:
            return
        conn = self._db.connect()
        for seed in missing:
            conn.execute(
                "INSERT OR IGNORE INTO alert_rules (builtin, name, metric, instance, op, "
                "threshold, clear_threshold, sustain_seconds, severity, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seed["builtin"],
                    seed["name"],
                    seed["metric"],
                    seed["instance"],
                    seed["op"],
                    seed["threshold"],
                    seed["clear_threshold"],
                    seed["sustain_seconds"],
                    seed["severity"],
                    seed["enabled"],
                ),
            )
        conn.commit()
        self._config.set(
            "alert_rules_seeded",
            sorted(seeded | {seed["builtin"] for seed in SEED_RULES}),
        )

    def _refresh_rules(self) -> None:
        rows = self._db.connect().execute("SELECT * FROM alert_rules ORDER BY id").fetchall()
        self._rules_by_id = {row["id"]: dict(row) for row in rows}

    def rules(self) -> list[dict]:
        self._refresh_rules()
        return [
            {**rule, "enabled": bool(rule["enabled"])} for rule in self._rules_by_id.values()
        ]

    def create_rule(self, data: dict) -> dict:
        fields = _validate(data)
        conn = self._db.connect()
        cursor = conn.execute(
            "INSERT INTO alert_rules (name, metric, instance, op, threshold, "
            "clear_threshold, sustain_seconds, severity, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(fields[field] for field in _RULE_FIELDS),
        )
        conn.commit()
        self._refresh_rules()
        return {**self._rules_by_id[cursor.lastrowid], "enabled": bool(fields["enabled"])}

    def update_rule(self, rule_id: int, data: dict) -> dict | None:
        fields = _validate(data)
        conn = self._db.connect()
        row = conn.execute("SELECT id FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
        if row is None:
            return None
        assignments = ", ".join(f"{field} = ?" for field in _RULE_FIELDS)
        conn.execute(
            f"UPDATE alert_rules SET {assignments} WHERE id = ?",
            (*(fields[field] for field in _RULE_FIELDS), rule_id),
        )
        conn.commit()
        if not fields["enabled"]:
            self._close_rule_events(rule_id, "rule disabled")
        self._refresh_rules()
        return {**self._rules_by_id[rule_id], "enabled": bool(fields["enabled"])}

    def delete_rule(self, rule_id: int) -> bool:
        conn = self._db.connect()
        cursor = conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
        conn.commit()
        if cursor.rowcount == 0:
            return False
        self._close_rule_events(rule_id, "rule deleted")
        for key in [key for key in self._states if key[0] == rule_id]:
            del self._states[key]
        self._refresh_rules()
        return True

    # -- events ------------------------------------------------------------------

    def _reattach(self) -> None:
        """Resume open events across a restart; the clear path still requires a
        sustained OK reading, so reattachment never auto-clears anything."""
        rows = self._db.connect().execute(
            "SELECT id, rule_id, instance, opened_at, peak_value FROM alert_events "
            "WHERE cleared_at IS NULL"
        ).fetchall()
        for row in rows:
            self._states[(row["rule_id"], row["instance"])] = _PairState(
                event_id=row["id"], opened_at=row["opened_at"], peak=row["peak_value"]
            )

    def _open_event(self, rule: dict, instance: str, value: float, now: float) -> int:
        context = {
            "name": rule["name"],
            "metric": rule["metric"],
            "op": rule["op"],
            "threshold": rule["threshold"],
            "severity": rule["severity"],
            "value_at_open": value,
        }
        conn = self._db.connect()
        cursor = conn.execute(
            "INSERT INTO alert_events (rule_id, instance, opened_at, peak_value, context) "
            "VALUES (?, ?, ?, ?, ?)",
            (rule["id"], instance, now, value, json.dumps(context)),
        )
        conn.commit()
        return cursor.lastrowid

    def _close_event(self, event_id: int, now: float, peak: float | None) -> None:
        conn = self._db.connect()
        conn.execute(
            "UPDATE alert_events SET cleared_at = ?, peak_value = ? WHERE id = ?",
            (now, peak, event_id),
        )
        conn.commit()

    def _close_rule_events(self, rule_id: int, reason: str) -> None:
        now = self._clock()
        conn = self._db.connect()
        conn.execute(
            "UPDATE alert_events SET cleared_at = ?, "
            "context = json_set(context, '$.closed', ?) "
            "WHERE rule_id = ? AND cleared_at IS NULL",
            (now, reason, rule_id),
        )
        conn.commit()
        for key, state in self._states.items():
            if key[0] == rule_id and state.event_id is not None:
                self._states[key] = _PairState()

    # -- evaluation ----------------------------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        self._refresh_rules()
        enabled = [rule for rule in self._rules_by_id.values() if rule["enabled"]]
        metric_names = {rule["metric"] for rule in enabled}
        raw = self._provider(metric_names) if metric_names else {}
        values = {
            metric: self._apply_kind(metric, per_instance, now)
            for metric, per_instance in raw.items()
        }

        for rule in enabled:
            per_instance = values.get(rule["metric"]) or {}
            if rule["instance"] == GLOBAL_INSTANCE:
                instances = list(per_instance)
            else:
                instances = [rule["instance"]]
            for instance in instances:
                self._evaluate(rule, instance, per_instance.get(instance), now)

        # Backstop sweep: a rule deleted or disabled outside the CRUD path
        # (or before this evaluator existed) must not strand an open event.
        enabled_ids = {rule["id"] for rule in enabled}
        for (rule_id, _instance), state in list(self._states.items()):
            if state.event_id is not None and rule_id not in enabled_ids:
                self._close_event(state.event_id, now, state.peak)
                self._states[(rule_id, _instance)] = _PairState()

        self._db.connect().execute(
            "DELETE FROM alert_events WHERE cleared_at IS NOT NULL AND cleared_at < ?",
            (now - EVENT_RETENTION_SECONDS,),
        )
        self._db.connect().commit()

    def _apply_kind(
        self, metric: str, per_instance: dict[str, float | None], now: float
    ) -> dict[str, float | None]:
        if METRICS.get(metric, {}).get("kind") != "counter":
            return per_instance
        out: dict[str, float | None] = {}
        for instance, total in per_instance.items():
            if total is None:
                out[instance] = None
                continue
            key = (metric, instance)
            previous = self._deltas.get(key)
            self._deltas[key] = total
            if previous is None or total < previous:
                out[instance] = 0.0  # first sight or reset: baseline, don't alert
            else:
                out[instance] = total - previous
        return out

    def _evaluate(self, rule: dict, instance: str, value: float | None, now: float) -> None:
        state = self._states.setdefault((rule["id"], instance), _PairState())
        if value is None:
            return
        state.last_value = value
        threshold = rule["threshold"]
        clear = rule["clear_threshold"] if rule["clear_threshold"] is not None else threshold
        if rule["op"] == ">":
            breached = value > threshold
            ok = value <= clear
        else:
            breached = value < threshold
            ok = value >= clear

        if state.event_id is None:
            state.ok_since = None
            if not breached:
                state.breach_since = None
                return
            if state.breach_since is None:
                state.breach_since = now
            if now - state.breach_since >= rule["sustain_seconds"]:
                state.event_id = self._open_event(rule, instance, value, now)
                state.opened_at = now
                state.peak = value
                state.breach_since = None
            return

        more_extreme = state.peak is None or (
            value > state.peak if rule["op"] == ">" else value < state.peak
        )
        if more_extreme:
            state.peak = value
            self._db.connect().execute(
                "UPDATE alert_events SET peak_value = ? WHERE id = ?",
                (value, state.event_id),
            )
            self._db.connect().commit()
        if not ok:
            state.ok_since = None
            return
        if state.ok_since is None:
            state.ok_since = now
        if now - state.ok_since >= max(rule["sustain_seconds"], CLEAR_MIN_SUSTAIN_SECONDS):
            self._close_event(state.event_id, now, state.peak)
            self._states[(rule["id"], instance)] = _PairState(last_value=value)

    # -- read side --------------------------------------------------------------

    def active(self) -> list[dict]:
        out = []
        for (rule_id, instance), state in self._states.items():
            if state.event_id is None:
                continue
            rule = self._rules_by_id.get(rule_id, {})
            metric = rule.get("metric")
            out.append(
                {
                    "event_id": state.event_id,
                    "rule_id": rule_id,
                    "name": rule.get("name"),
                    "metric": metric,
                    "unit": METRICS.get(metric, {}).get("unit"),
                    "instance": instance,
                    "severity": rule.get("severity"),
                    "opened_at": state.opened_at,
                    "value": state.last_value,
                    "peak": state.peak,
                    "threshold": rule.get("threshold"),
                    "op": rule.get("op"),
                }
            )
        return sorted(out, key=lambda a: a["opened_at"] or 0.0)

    def active_brief(self) -> list[dict]:
        """Compact active-alert list for the 1 s fleet stream (memory-only)."""
        return [
            {
                "instance": alert["instance"],
                "severity": alert["severity"],
                "name": alert["name"],
            }
            for alert in self.active()
        ]

    def history(self, seconds: int) -> list[dict]:
        since = self._clock() - seconds
        rows = self._db.connect().execute(
            "SELECT id, rule_id, instance, opened_at, cleared_at, peak_value, context "
            "FROM alert_events WHERE opened_at >= ? ORDER BY opened_at DESC LIMIT ?",
            (since, HISTORY_LIMIT),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            try:
                event["context"] = json.loads(event["context"])
            except ValueError:
                event["context"] = {}
            events.append(event)
        return events
