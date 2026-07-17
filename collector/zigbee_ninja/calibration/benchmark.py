"""NCP throughput calibration: per-run-authorized closed-loop ramp (DESIGN.md §11).

This is the one zigbee-ninja feature that transmits onto the mesh on purpose,
so its posture is the strictest in the product:

- **Per-run authorization, never a standing grant.** A dry-run preview shows
  the exact traffic, schedule, caps, and stop rules and mints a single-use
  authorization token (short TTL). Starting a run requires echoing that token;
  nothing persists across runs (DESIGN.md §6).
- **Closed loop.** Reads are paced at the step rate but bounded by outstanding
  replies: a stalling mesh throttles the driver instead of being buried.
- **Benign traffic.** Unicast attribute reads through the instance's own MQTT
  command path (`<base>/<target>/get {"<attr>": ""}`), the same path
  controllers use. Reads actuate nothing; each reply republishes device state.
- **Self-attributed.** The engine classifies both the reads and their state
  echoes as `self` (P4); run windows are recorded in the calibrations table so
  utilization views can flag or exclude them.

The knee (§10 denominator 2) is the highest ramp step sustained without a stop
rule firing: p95 RTT breach vs the step-1 baseline, read-timeout ratio,
instance delivery failures, or driver saturation (the closed loop can no
longer reach the requested rate: which indicates the *pipeline* service
ceiling, denominator 3, and bounds the NCP knee from below; the record says
which). RTT prefers the wire-tier SLI when a tap covers the coordinator and
falls back to the MQTT command→state-echo path, provenance-tagged either way.
"""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

from ..store.db import Database

# -- ramp schedule & hard caps (all shown verbatim in the dry-run preview) ------
RAMP_RATES_EPS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
# Spread mode round-robins reads across several routers so no single device's
# Zigbee2MQTT queue binds first: the aggregate ramp probes the NCP/global
# pipeline knee (§10 denominator 2) instead of the per-device ceiling.
SPREAD_RATES_EPS = (8.0, 16.0, 32.0, 64.0)
SPREAD_MIN_TARGETS = 4
SPREAD_DEFAULT_TARGETS = 6
SPREAD_MAX_TARGETS = 10
SPREAD_PER_TARGET_MAX_EPS = 16.0  # per-device share stays under measured ceilings
STEP_SECONDS = 20.0
READ_TIMEOUT_SECONDS = 5.0
SETTLE_TICK_SECONDS = 0.2
# The outstanding bound allows this much in-flight time before throttling;
# beyond it the driver defers sends and the step registers as saturated.
OUTSTANDING_RTT_ALLOWANCE = 0.25
MIN_OUTSTANDING = 4
MAX_RATE_EPS = 50.0
MAX_RUN_SECONDS = 900.0
MAX_TOTAL_READS = 5000
COOLDOWN_SECONDS = 120.0
AUTHORIZATION_TTL_SECONDS = 600.0

# -- stop rules (knee detection: a normal end of ramp) -------------------------
RTT_BREACH_FACTOR = 3.0
RTT_FLOORS_MS = {"wire": 300.0, "echo": 2000.0}
TIMEOUT_BREACH_RATIO = 0.10
SATURATION_RATIO = 0.80
DELIVERY_FAILURES_PER_STEP = 10

# -- pacer integrity (the meter refuses knees its own timekeeping distorted) ----
# The driver measures how late each pacing sleep wakes up. Total lateness is
# recorded telemetry; only STALL-SIZED wakeups (PACER_STALL_SECONDS or more)
# count toward the cumulative refusal bound. The absolute schedule's catch-up
# absorbs scheduler-granularity lateness by construction (a few ms per wakeup
# scales with send count, not interference, and achieved==requested proves
# the schedule held), while a stall queues sends into a burst and lags the
# receive-side stamps. A step whose stall time crosses the fraction, or any
# single stall of a second or more, cannot tell a saturated pipeline from a
# stalled collector, so the run records no knee.
PACER_STALL_SECONDS = 0.25
PACER_DEGRADED_FRACTION = 0.05
PACER_DEGRADED_MAX_STALL_S = 1.0

# -- ambient context (recorded always; the preview warns above these rates) ----
AMBIENT_LOOKBACK_SECONDS = 600
AMBIENT_WARN_COMMANDS_PER_S = 1.0
AMBIENT_WARN_STATE_PER_S = 3.0

# -- watchdog rules (abort: something beyond the run is being affected) --------
WATCHDOG_BRIDGE_ERRORS = 5
STALL_MIN_SENT = 10
STALL_SECONDS = 10.0

# -- controlled replay (V2_PROPOSAL.md §V2-5 detector 5c) ----------------------
# A replay reproduces a recorded or recomposed burst's TIMING SHAPE with the
# same benign reads the ramp uses, round-robined across eligible routers. It
# measures no knee: the latency and delivery observed under the reproduced
# shape are the result. Unlike the ramp's consume-or-defer slots, a replay
# never drops a send: a slot blocked by the outstanding bound waits, and the
# slip is visible in the recorded send offsets.
REPLAY_MIN_COMMANDS = 4
REPLAY_MAX_COMMANDS = 400
REPLAY_MAX_WINDOW_SECONDS = 30.0
REPLAY_MAX_OUTSTANDING = 32
REPLAY_MIN_TARGETS = 2
REPLAY_DEFAULT_TARGETS = 6
# Scenario-sourced replays reproduce the recomposed stream this far around
# the predicted peak second.
REPLAY_SCENARIO_PAD_SECONDS = 5.0
REPLAY_BLOCKED_TICK_SECONDS = 0.02
REPLAY_VARIANTS = ("as_recorded", "compressed")

HISTORY_LIMIT = 20


class CalibrationRejected(RuntimeError):
    """Refused before any mesh traffic was generated."""


class _Abort(Exception):
    """Internal: watchdog or manual abort; stop transmitting immediately."""


@dataclass
class StepResult:
    rate_eps: float
    duration_s: float
    started_at: float = 0.0
    sent: int = 0
    completed: int = 0
    timeouts: int = 0
    deferred: int = 0
    achieved_eps: float = 0.0
    echo_p50_ms: float | None = None
    echo_p95_ms: float | None = None
    wire_p50_ms: float | None = None
    wire_p95_ms: float | None = None
    wire_samples: int = 0
    delivery_failed_delta: int = 0
    rtt_source: str | None = None
    breach: str | None = None
    pacer_late_s: float = 0.0
    pacer_stall_s: float = 0.0
    pacer_max_late_ms: float = 0.0
    pacer_stalls: int = 0
    # Replay steps record when each send actually left, relative to the step
    # start: the achieved shape beside the requested one. Ramp steps leave
    # this empty.
    sent_offsets: list[float] = field(default_factory=list)


@dataclass
class _ActiveRun:
    run_id: str
    plan: dict
    started_at: float
    state: str = "running"  # running | settling
    step_index: int = 0
    current: StepResult | None = None
    steps: list[StepResult] = field(default_factory=list)
    # Send timestamps, one FIFO per target so replies pair with their own
    # device's reads (a single-target run is simply K = 1).
    outstanding: dict[str, deque] = field(default_factory=dict)
    target_names: frozenset = field(default_factory=frozenset)
    rr_index: int = 0
    step_echo_rtts: list[float] = field(default_factory=list)
    sent_total: int = 0
    bridge_errors: int = 0
    abort_reason: str | None = None
    # Instance traffic that is not the benchmark's own, tallied while the
    # run is active: a noisy window is visible in the record instead of
    # silently confounding it.
    ambient: dict = field(default_factory=dict)

    def outstanding_total(self) -> int:
        return sum(len(fifo) for fifo in self.outstanding.values())


@dataclass
class _BulkState:
    """A sequential queue of authorized runs: one batch, one authorization."""

    batch_id: str
    queue: list[dict]
    started_at: float
    position: int = 0
    state: str = "starting"  # waiting_cooldown | running
    skipped: list[dict] = field(default_factory=list)
    abort_requested: bool = False


def _pacer_degraded(step: StepResult) -> bool:
    return (
        step.pacer_stall_s > PACER_DEGRADED_FRACTION * step.duration_s
        or step.pacer_max_late_ms >= PACER_DEGRADED_MAX_STALL_S * 1000.0
    )


def _offsets_peak_1s(offsets: list[float]) -> float:
    """Highest send count inside any sliding 1 s span of a schedule."""
    if not offsets:
        return 0.0
    best, left = 1, 0
    for right in range(len(offsets)):
        while offsets[right] - offsets[left] > 1.0:
            left += 1
        best = max(best, right - left + 1)
    return float(best)


def _percentiles(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    ordered = sorted(values)
    p50 = round(statistics.median(ordered), 1)
    p95 = round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1)
    return p50, p95


def _device_lqi_and_degree(raw_map: dict) -> dict[str, dict]:
    """Per-IEEE best link LQI and link count from a raw Z2M networkmap."""
    result: dict[str, dict] = {}
    for link in raw_map.get("links") or []:
        source = str(link.get("sourceIeeeAddr") or (link.get("source") or {}).get("ieeeAddr"))
        target = str(link.get("targetIeeeAddr") or (link.get("target") or {}).get("ieeeAddr"))
        lqi = link.get("lqi", link.get("linkquality"))
        for end in (source, target):
            entry = result.setdefault(end, {"lqi": None, "degree": 0})
            entry["degree"] += 1
            if isinstance(lqi, (int, float)):
                entry["lqi"] = lqi if entry["lqi"] is None else max(entry["lqi"], lqi)
    return result


class CalibrationManager:
    """Preview/authorize/run lifecycle plus the engine-facing message hooks."""

    def __init__(
        self,
        db: Database,
        publisher: Callable[[str, str], Awaitable[None]],
        devices: Callable[[str], list[dict]],
        groups: Callable[[str], list[dict]],
        instances: Callable[[], list[dict]],
        topology_latest: Callable[[str], dict],
        wire_covers: Callable[[str], bool] | None = None,
        wire_latency_mark: Callable[[str], float] | None = None,
        wire_latency_since: Callable[[str, float], list[float]] | None = None,
        wire_delivery_totals: Callable[[str], tuple[int, int]] | None = None,
        events_log=None,
        registry=None,
        pricing: Callable[[str], tuple[float | None, float | None]] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._db = db
        self._publish = publisher
        self._devices = devices
        self._groups = groups
        self._instances = instances
        self._topology_latest = topology_latest
        self._wire_covers = wire_covers or (lambda _instance: False)
        self._wire_latency_mark = wire_latency_mark or (lambda _instance: 0.0)
        self._wire_latency_since = wire_latency_since or (lambda _instance, _mark: [])
        self._wire_delivery_totals = wire_delivery_totals or (lambda _instance: (0, 0))
        # Controlled-replay dependencies: the raw event store supplies the
        # recorded schedules, the registry and pricing feed scenario-sourced
        # recompositions. Absent in minimal harnesses; replay previews then
        # refuse with a clear message.
        self._events_log = events_log
        self._registry = registry
        self._pricing = pricing
        self._clock = clock
        self._sleep = sleep
        self._authorizations: dict[str, dict] = {}
        self._active: _ActiveRun | None = None
        self._bulk: _BulkState | None = None
        self._task: asyncio.Task | None = None
        self._cooldown_until = 0.0

    # -- engine-facing hooks (hot path guards on `active`) -----------------------

    @property
    def active(self) -> bool:
        return self._active is not None

    def owns_command(self, base: str, target: str, verb: str) -> bool:
        """True for the run's own reads: the engine skips chain attribution
        for them because publish() already accounted them as `self` (P4)."""
        run = self._active
        return (
            run is not None
            and verb == "get"
            and base == run.plan["instance"]
            and target in run.target_names
        )

    def on_state(self, base: str, name: str) -> bool:
        """Complete one outstanding read on a target's state echo.

        Returns True only when an outstanding read was completed: the engine
        then classes the echo `self`. A target state publish with nothing
        outstanding stays an ordinary autonomous report.
        """
        run = self._active
        if run is None or base != run.plan["instance"] or name not in run.target_names:
            return False
        fifo = run.outstanding.get(name)
        if not fifo:
            return False
        sent_at = fifo.popleft()
        run.step_echo_rtts.append((self._clock() - sent_at) * 1000.0)
        if run.current is not None:
            run.current.completed += 1
        return True

    def note_ambient(self, base: str, kind: str) -> None:
        """Tally instance traffic that is not the benchmark's own while a run
        is active; the run record carries the rates so a noisy window is
        visible rather than silently confounding the measurement."""
        run = self._active
        if run is not None and base == run.plan["instance"]:
            run.ambient[kind] = run.ambient.get(kind, 0) + 1

    def on_availability(self, base: str, suffix: str, payload: bytes) -> None:
        """Watchdog: any device on the instance going offline aborts the run."""
        run = self._active
        if run is None or base != run.plan["instance"]:
            return
        name = suffix.rsplit("/", 1)[0]
        try:
            data = json.loads(payload)
            state = data.get("state") if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError):
            state = payload.decode(errors="replace").strip()
        if state != "offline":
            return
        if name in run.target_names:
            self._request_abort(f"target {name} went offline")
        else:
            self._request_abort(f"uninvolved device {name} went offline during the run")

    def on_bridge_log(self, base: str, payload: bytes) -> None:
        """Watchdog: an error spike in the Zigbee2MQTT log aborts the run."""
        run = self._active
        if run is None or base != run.plan["instance"]:
            return
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(data, dict) and data.get("level") == "error":
            run.bridge_errors += 1
            if run.bridge_errors >= WATCHDOG_BRIDGE_ERRORS:
                self._request_abort(
                    f"{run.bridge_errors} Zigbee2MQTT error log lines during the run"
                )

    # -- candidates (§11.1) -------------------------------------------------------

    def candidates(self, instance: str) -> dict:
        devices = self._devices(instance)
        if not devices:
            raise ValueError(f"No device registry for {instance}")
        topology = self._topology_latest(instance) or {}
        link_stats = _device_lqi_and_degree(topology.get("raw") or {})
        unresponsive = set(topology.get("unresponsive_nodes") or [])
        membership: dict[str, int] = {}
        for group in self._groups(instance):
            for ieee in group.get("member_ieee", []):
                membership[ieee] = membership.get(ieee, 0) + 1

        rows = []
        for device in devices:
            if device.get("type") != "Router":
                continue
            ieee = device.get("ieee_address")
            stats = link_stats.get(str(ieee), {})
            reasons: list[str] = []
            eligible = True
            if not str(device.get("power_source") or "").startswith("Mains"):
                eligible = False
                reasons.append("not mains-powered")
            if not device.get("get_attribute"):
                eligible = False
                reasons.append("no gettable attribute")
            if device.get("friendly_name") in unresponsive:
                reasons.append("did not answer the last topology sweep")
            lqi = stats.get("lqi")
            binding_count = int(device.get("binding_count") or 0)
            group_count = membership.get(ieee, 0)
            score = None
            if eligible:
                # Healthy link first, then the least-entangled device: bindings
                # mean reporting consumers, groups mean groupcast interruptions.
                score = round(
                    (lqi if lqi is not None else 0)
                    - 10 * binding_count
                    - 5 * group_count
                    - (50 if device.get("friendly_name") in unresponsive else 0),
                    1,
                )
            rows.append(
                {
                    "friendly_name": device.get("friendly_name"),
                    "ieee_address": ieee,
                    "vendor": device.get("vendor"),
                    "model": device.get("model"),
                    "get_attribute": device.get("get_attribute"),
                    "published_measurements": device.get("published_measurements") or [],
                    "binding_count": binding_count,
                    "group_count": group_count,
                    "lqi": lqi,
                    "degree": stats.get("degree", 0),
                    "eligible": eligible,
                    "reasons": reasons,
                    "score": score,
                }
            )
        rows.sort(key=lambda row: (not row["eligible"], -(row["score"] or -1e9)))
        return {
            "instance": instance,
            "candidates": rows,
            "topology_pulled_at": topology.get("pulled_at"),
        }

    # -- preview & authorization (§11.2) -------------------------------------------

    def preview(self, instance: str, target: str) -> dict:
        return self._authorize(self._build_plan(instance, [target], mode="single"))

    def preview_spread(
        self,
        instance: str,
        count: int = SPREAD_DEFAULT_TARGETS,
        targets: list[str] | None = None,
    ) -> dict:
        """NCP-knee ramp: reads round-robin across the top-ranked routers so
        no single device's pipeline queue binds first (§10 denominator 2)."""
        if targets is None:
            ranked = self.candidates(instance)["candidates"]
            eligible = [row["friendly_name"] for row in ranked if row["eligible"]]
            targets = eligible[: min(max(count, SPREAD_MIN_TARGETS), SPREAD_MAX_TARGETS)]
        return self._authorize(self._build_plan(instance, targets, mode="spread"))

    def preview_replay(self, instance: str, source: dict) -> dict:
        """Controlled replay (V2_PROPOSAL.md §V2-5 detector 5c): reproduce a
        recorded or recomposed burst's timing shape with benign reads, on the
        same per-run authorization rails as the ramp."""
        return self._authorize(self._build_replay_plan(instance, source))

    def _replay_offsets_window(self, instance: str, source: dict) -> tuple[list[float], dict]:
        start = float(source.get("start") or 0)
        end = float(source.get("end") or 0)
        if end <= start:
            raise ValueError("window end must be after start")
        if end - start > REPLAY_MAX_WINDOW_SECONDS:
            raise ValueError(
                f"replay windows are at most {REPLAY_MAX_WINDOW_SECONDS:.0f} s"
            )
        overlap = self._db.connect().execute(
            "SELECT COUNT(*) AS n FROM calibrations "
            "WHERE instance = ? AND started_at < ? AND finished_at > ?",
            (instance, end, start),
        ).fetchone()
        if overlap["n"]:
            raise ValueError(
                "the window overlaps a benchmark run; that traffic was "
                "zigbee-ninja's own and replaying it is meaningless"
            )
        times = self._events_log.event_times(
            instance, start, end, source="mqtt", kinds=("command",)
        )
        offsets = [round(ts - times[0], 3) for ts in times] if times else []
        description = {
            "kind": "window",
            "start": start,
            "end": end,
            "commands_recorded": len(times),
        }
        return offsets, description

    def _replay_offsets_scenario(self, instance: str, source: dict) -> tuple[list[float], dict]:
        if self._registry is None or self._pricing is None:
            raise ValueError("scenario replays need the registry and pricing wired")
        from ..capacity import scenario

        moves = source.get("moves") or []
        report = scenario.price_scenario(
            self._events_log,
            self._db,
            self._registry,
            self._pricing,
            self._topology_latest,
            moves,
            clock=self._clock,
        )
        entry = report["instances"].get(instance)
        if entry is None:
            raise ValueError(f"unknown instance {instance!r}")
        if not entry.get("touched"):
            raise ValueError(
                f"the staged moves do not touch {instance}; replay a "
                "coordinator whose load the scenario changes"
            )
        peak = entry["burst"]["after_peak_1s"]
        if peak is None:
            raise ValueError(
                "the recomposed stream has no recorded traffic to replay"
            )
        moved_devices, moved_groups = scenario.resolve_moves(self._registry, moves)
        out_topics, inbound_by_dest = scenario.recomposition_topics(
            moved_devices, moved_groups
        )
        now = self._clock()
        windows = scenario._benchmark_windows(
            self._db.connect(), now - scenario.DEFAULT_WINDOW_SECONDS
        )
        lo = peak["at"] - REPLAY_SCENARIO_PAD_SECONDS
        hi = peak["at"] + 1.0 + REPLAY_SCENARIO_PAD_SECONDS
        times = scenario.recomposed_times(
            self._events_log,
            windows,
            instance,
            out_topics.get(instance, ()),
            inbound_by_dest.get(instance, []),
            lo,
            hi,
        )
        offsets = [round(ts - times[0], 3) for ts in times] if times else []
        description = {
            "kind": "scenario",
            "moves": [
                {
                    "kind": move["kind"],
                    "subject": move["subject"],
                    "from_instance": move["from_instance"],
                    "to_instance": move["to_instance"],
                }
                for move in moves
            ],
            "peak_window_at": peak["at"],
            "commands_recomposed": len(times),
        }
        predicted = {
            "peak_1s_eps": peak["eps_1s"],
            "verdict": entry["burst"]["verdict"],
            "limits": entry.get("limits"),
        }
        description["predicted"] = predicted
        return offsets, description

    def _build_replay_plan(self, instance: str, source: dict) -> dict:
        if self._events_log is None:
            raise ValueError("controlled replay needs the raw event store wired")
        variant = source.get("variant") or "as_recorded"
        if variant not in REPLAY_VARIANTS:
            raise ValueError(f"variant must be one of {REPLAY_VARIANTS}")
        kind = source.get("kind")
        if kind == "window":
            offsets, description = self._replay_offsets_window(instance, source)
        elif kind == "scenario":
            offsets, description = self._replay_offsets_scenario(instance, source)
        else:
            raise ValueError("source kind must be window or scenario")
        if len(offsets) < REPLAY_MIN_COMMANDS:
            raise ValueError(
                f"the source holds {len(offsets)} commands; a replay needs at "
                f"least {REPLAY_MIN_COMMANDS} to mean anything"
            )
        if len(offsets) > REPLAY_MAX_COMMANDS:
            raise ValueError(
                f"the source holds {len(offsets)} commands; replays are capped "
                f"at {REPLAY_MAX_COMMANDS}"
            )
        window_seconds = max(offsets[-1], 1.0)
        requested_peak = _offsets_peak_1s(offsets)
        if variant == "compressed":
            # The no-stagger variant: every send due at once; the outstanding
            # bound is the only pacing, so the mesh sees the recorded moment
            # with its spacing stripped.
            offsets = [0.0] * len(offsets)
            requested_peak = float(len(offsets))

        ranked = self.candidates(instance)["candidates"]
        eligible = [row["friendly_name"] for row in ranked if row["eligible"]]
        targets_wanted = min(REPLAY_DEFAULT_TARGETS, len(eligible))
        if targets_wanted < REPLAY_MIN_TARGETS:
            raise ValueError(
                f"a replay needs at least {REPLAY_MIN_TARGETS} eligible routers "
                f"to spread reads across; {instance} has {len(eligible)}"
            )
        target_names = eligible[:targets_wanted]
        devices = [self._find_device(instance, name) for name in target_names]
        info = self._instance_info(instance)
        wire = self._wire_covers(instance)
        warnings = self._plan_warnings(instance, devices, info, wire)
        if variant == "compressed":
            warnings.append(
                "Compressed variant: the recorded spacing is stripped and every "
                "read is due at once; the outstanding bound is the only pacing. "
                "This is the worst-case shape the staggers exist to prevent."
            )

        targets = [
            {
                "friendly_name": device["friendly_name"],
                "get_attribute": device["get_attribute"],
                "topic": f"{instance}/{device['friendly_name']}/get",
                "payload": json.dumps({device["get_attribute"]: ""}),
            }
            for device in devices
        ]
        now = self._clock()
        return {
            "mode": "replay",
            "instance": instance,
            "target": f"{len(devices)} routers (replay)",
            "target_ieee": None,
            "get_attribute": None,
            "topic": None,
            "payload": None,
            "targets": targets,
            "traffic": (
                f"Unicast ZCL attribute reads reproducing the source's timing "
                f"shape ({len(offsets)} commands over "
                f"{window_seconds:.1f} s, peak {requested_peak:.0f}/s), "
                f"round-robined across {len(devices)} routers via Zigbee2MQTT's "
                "own command path. Nothing is written or actuated; no capacity "
                "limit is recorded: the latency and delivery under this shape "
                "are the result."
            ),
            "per_target_max_eps": None,
            "steps": [
                {
                    "rate_eps": round(requested_peak, 1),
                    "duration_s": round(window_seconds, 2),
                    "reads": len(offsets),
                    "offsets": offsets,
                }
            ],
            "total_reads": len(offsets),
            "estimated_duration_s": int(window_seconds + READ_TIMEOUT_SECONDS + 1),
            "read_timeout_s": READ_TIMEOUT_SECONDS,
            "max_outstanding_rule": (
                f"clamp(peak × {OUTSTANDING_RTT_ALLOWANCE}s, {MIN_OUTSTANDING}, "
                f"{REPLAY_MAX_OUTSTANDING}): a stalling mesh delays the shape "
                "instead of being buried; the slip is recorded"
            ),
            "rtt_source": "wire" if wire else "echo",
            "caps": {
                "max_rate_eps": None,
                "max_run_seconds": MAX_RUN_SECONDS,
                "max_total_reads": MAX_TOTAL_READS,
            },
            "stop_rules": {
                "note": (
                    "replays do not stop on latency or saturation: elevated "
                    "latency under the reproduced shape is the measurement; "
                    "watchdogs and hard caps still abort"
                )
            },
            "watchdog": {
                "bridge_error_lines": WATCHDOG_BRIDGE_ERRORS,
                "uninvolved_offline": "any device on the instance going offline aborts",
                "stall": f"no replies at all after {STALL_MIN_SENT} reads / {STALL_SECONDS:.0f}s",
                "manual_abort": "always available",
            },
            "cooldown_seconds": COOLDOWN_SECONDS,
            "warnings": warnings,
            "replay": {
                "source": description,
                "variant": variant,
                "window_seconds": round(window_seconds, 2),
                "requested_peak_1s_eps": round(requested_peak, 1),
                "predicted": description.get("predicted"),
            },
            "environment": {
                "z2m_version": (info or {}).get("version"),
                "coordinator_type": (info or {}).get("coordinator_type"),
                "coordinator_revision": (info or {}).get("coordinator_revision"),
            },
            "created_at": now,
        }

    def preview_bulk(
        self, instances: list[str] | None = None, targets: dict[str, str] | None = None
    ) -> dict:
        """One enumerated batch, one authorization: auto-picks each instance's
        top-ranked eligible router unless a target is pinned; instances with
        nothing eligible are listed as skipped rather than failing the batch."""
        bases = instances or [info["base_topic"] for info in self._instances()]
        if not bases:
            raise ValueError("No instances discovered")
        runs: list[dict] = []
        skipped: list[dict] = []
        for base in sorted(set(bases)):
            pinned = (targets or {}).get(base)
            try:
                if pinned is None:
                    ranked = self.candidates(base)["candidates"]
                    top = next((row for row in ranked if row["eligible"]), None)
                    if top is None:
                        skipped.append({"instance": base, "reason": "no eligible router"})
                        continue
                    pinned = top["friendly_name"]
                runs.append(self._build_plan(base, [pinned], mode="single"))
            except ValueError as exc:
                skipped.append({"instance": base, "reason": str(exc)})
        if not runs:
            raise ValueError("No instances with an eligible calibration target")
        return self._authorize(
            {
                "batch": True,
                "batch_id": f"batch-{secrets.token_hex(4)}",
                "runs": runs,
                "skipped": skipped,
                "total_reads": sum(plan["total_reads"] for plan in runs),
                "estimated_duration_s": int(
                    sum(plan["estimated_duration_s"] for plan in runs)
                    + COOLDOWN_SECONDS * max(len(runs) - 1, 0)
                ),
                "cooldown_between_runs_s": COOLDOWN_SECONDS,
                "created_at": self._clock(),
            }
        )

    def _authorize(self, plan: dict) -> dict:
        token = secrets.token_hex(8)
        now = self._clock()
        self._authorizations[token] = plan
        for stale in [
            key
            for key, value in self._authorizations.items()
            if now - value["created_at"] > AUTHORIZATION_TTL_SECONDS
        ]:
            del self._authorizations[stale]
        return {
            **plan,
            "authorization": token,
            "authorization_expires_at": plan["created_at"] + AUTHORIZATION_TTL_SECONDS,
        }

    def _plan_warnings(
        self, instance: str, devices: list[dict], info: dict | None, wire: bool
    ) -> list[str]:
        """Preview warnings shared by ramps and replays: chatty targets,
        missing wire coverage, shared channels, and a noisy recent window."""
        warnings: list[str] = []
        chatty = [
            device["friendly_name"]
            for device in devices
            if device.get("published_measurements")
        ]
        if chatty:
            measurements = sorted(
                {
                    prop
                    for device in devices
                    for prop in device.get("published_measurements") or []
                }
            )
            warnings.append(
                f"Each reply republishes device state; {', '.join(chatty)} report(s) "
                f"{', '.join(measurements)}: expect state/recorder churn in "
                "controllers for the run duration."
            )
        if not wire:
            warnings.append(
                "No wire tap covers this coordinator: RTT falls back to the "
                "MQTT command→state-echo path (coarser; the knee is tagged accordingly)."
            )
        shared = [
            other["base_topic"]
            for other in self._instances()
            if other.get("base_topic") != instance
            and other.get("channel") is not None
            and other.get("channel") == (info or {}).get("channel")
        ]
        if shared:
            warnings.append(
                f"Shares Zigbee channel {(info or {}).get('channel')} with "
                f"{', '.join(shared)}: their traffic contends with the benchmark."
            )
        ambient = self._recent_ambient(instance)
        if ambient is not None:
            cmd_rate, state_rate = ambient
            if cmd_rate > AMBIENT_WARN_COMMANDS_PER_S or state_rate > AMBIENT_WARN_STATE_PER_S:
                warnings.append(
                    f"Ambient traffic is elevated ({cmd_rate:.1f} commands/s, "
                    f"{state_rate:.1f} state reports/s over the last "
                    f"{AMBIENT_LOOKBACK_SECONDS // 60} minutes). The run records "
                    "ambient rates either way; a quieter hour gives a cleaner "
                    "measurement."
                )
        return warnings

    def _build_plan(self, instance: str, target_names: list[str], mode: str) -> dict:
        if len(set(target_names)) != len(target_names):
            raise ValueError("Duplicate calibration targets")
        if mode == "spread" and not (
            SPREAD_MIN_TARGETS <= len(target_names) <= SPREAD_MAX_TARGETS
        ):
            raise ValueError(
                f"A spread ramp needs {SPREAD_MIN_TARGETS}–{SPREAD_MAX_TARGETS} "
                f"eligible routers; got {len(target_names)}"
            )
        devices = [self._find_device(instance, name) for name in target_names]
        info = self._instance_info(instance)
        wire = self._wire_covers(instance)
        warnings = self._plan_warnings(instance, devices, info, wire)

        rates = RAMP_RATES_EPS if mode == "single" else SPREAD_RATES_EPS
        cap = MAX_RATE_EPS if mode == "single" else SPREAD_PER_TARGET_MAX_EPS * len(devices)
        steps = [
            {"rate_eps": rate, "duration_s": STEP_SECONDS, "reads": int(rate * STEP_SECONDS)}
            for rate in rates
            if rate <= cap
        ]
        targets = [
            {
                "friendly_name": device["friendly_name"],
                "get_attribute": device["get_attribute"],
                "topic": f"{instance}/{device['friendly_name']}/get",
                "payload": json.dumps({device["get_attribute"]: ""}),
            }
            for device in devices
        ]
        single = mode == "single"
        now = self._clock()
        plan = {
            "mode": mode,
            "instance": instance,
            "target": (
                target_names[0] if single else f"{len(devices)} routers (spread)"
            ),
            "target_ieee": devices[0].get("ieee_address") if single else None,
            "get_attribute": devices[0]["get_attribute"] if single else None,
            "topic": targets[0]["topic"] if single else None,
            "payload": targets[0]["payload"] if single else None,
            "targets": targets,
            "traffic": (
                "Unicast ZCL attribute reads via Zigbee2MQTT's own command path; "
                "each read is one TX unicast plus the target's reply: nothing is "
                "written or actuated."
                if single
                else (
                    f"Unicast ZCL attribute reads round-robined across "
                    f"{len(devices)} routers via Zigbee2MQTT's own command path: "
                    f"per-device share stays at or below "
                    f"{SPREAD_PER_TARGET_MAX_EPS:.0f}/s (under every measured "
                    "per-device ceiling), so the aggregate ramp probes the "
                    "NCP/global pipeline knee (denominator 2). Nothing is written "
                    "or actuated."
                )
            ),
            "per_target_max_eps": (
                None if single else round(max(rate for rate in rates) / len(devices), 1)
            ),
            "steps": steps,
            "total_reads": sum(step["reads"] for step in steps),
            "estimated_duration_s": int(len(steps) * (STEP_SECONDS + READ_TIMEOUT_SECONDS)),
            "read_timeout_s": READ_TIMEOUT_SECONDS,
            "max_outstanding_rule": (
                f"max({MIN_OUTSTANDING}, rate × {OUTSTANDING_RTT_ALLOWANCE}s): a "
                "stalling mesh throttles the driver"
            ),
            "rtt_source": "wire" if wire else "echo",
            "caps": {
                "max_rate_eps": MAX_RATE_EPS,
                "max_run_seconds": MAX_RUN_SECONDS,
                "max_total_reads": MAX_TOTAL_READS,
            },
            "stop_rules": {
                "rtt_p95": (
                    f"p95 RTT above max({RTT_BREACH_FACTOR}× step-1 baseline, "
                    f"{RTT_FLOORS_MS['wire']:.0f} ms wire / {RTT_FLOORS_MS['echo']:.0f} ms echo)"
                ),
                "timeout_ratio": TIMEOUT_BREACH_RATIO,
                "saturation_ratio": SATURATION_RATIO,
                "delivery_failures_per_step": DELIVERY_FAILURES_PER_STEP,
            },
            "watchdog": {
                "bridge_error_lines": WATCHDOG_BRIDGE_ERRORS,
                "uninvolved_offline": "any device on the instance going offline aborts",
                "stall": f"no replies at all after {STALL_MIN_SENT} reads / {STALL_SECONDS:.0f}s",
                "manual_abort": "always available",
            },
            "cooldown_seconds": COOLDOWN_SECONDS,
            "warnings": warnings,
            "environment": {
                "z2m_version": (info or {}).get("version"),
                "coordinator_type": (info or {}).get("coordinator_type"),
                "coordinator_revision": (info or {}).get("coordinator_revision"),
            },
            "created_at": now,
        }
        return plan

    # -- run lifecycle ---------------------------------------------------------------

    async def start(self, instance: str, target: str, authorization: str) -> dict:
        plan = self._take_authorization(authorization, batch=False)
        if plan.get("mode") == "replay":
            raise CalibrationRejected(
                "This authorization is for a replay: use /api/calibration/replay/run"
            )
        single = plan.get("mode", "single") == "single"
        if plan["instance"] != instance or (single and plan["target"] != target):
            raise CalibrationRejected("Authorization does not match this instance/target")
        self._require_idle()
        self._verify_plan_targets(plan)
        self._authorizations.pop(authorization, None)  # single-use
        run = self._make_run(plan)
        self._active = run
        self._task = asyncio.get_running_loop().create_task(self._run(run))
        return self.status()

    async def start_replay(self, instance: str, authorization: str) -> dict:
        plan = self._take_authorization(authorization, batch=False)
        if plan.get("mode") != "replay":
            raise CalibrationRejected(
                "This authorization is for a ramp: use /api/calibration/run"
            )
        if plan["instance"] != instance:
            raise CalibrationRejected("Authorization does not match this instance")
        self._require_idle()
        self._verify_plan_targets(plan)
        self._authorizations.pop(authorization, None)  # single-use
        run = self._make_run(plan)
        self._active = run
        self._task = asyncio.get_running_loop().create_task(self._run(run))
        return self.status()

    def _verify_plan_targets(self, plan: dict) -> None:
        """Re-verify against the live registry: the fleet may have changed
        between preview and confirmation."""
        for entry in plan["targets"]:
            try:
                device = self._find_device(plan["instance"], entry["friendly_name"])
            except ValueError as exc:
                raise CalibrationRejected(str(exc)) from exc
            if device.get("get_attribute") != entry["get_attribute"]:
                raise CalibrationRejected("Target definition changed since the preview")

    def _make_run(self, plan: dict) -> _ActiveRun:
        return _ActiveRun(
            run_id=f"cal-{secrets.token_hex(4)}",
            plan=plan,
            started_at=self._clock(),
            outstanding={entry["friendly_name"]: deque() for entry in plan["targets"]},
            target_names=frozenset(entry["friendly_name"] for entry in plan["targets"]),
        )

    async def start_bulk(self, authorization: str) -> dict:
        batch = self._take_authorization(authorization, batch=True)
        self._require_idle()
        self._authorizations.pop(authorization, None)  # single-use
        self._bulk = _BulkState(
            batch_id=batch["batch_id"],
            queue=list(batch["runs"]),
            started_at=self._clock(),
        )
        self._task = asyncio.get_running_loop().create_task(self._run_bulk())
        return self.status()

    def _take_authorization(self, authorization: str, batch: bool) -> dict:
        """Validate (but do not consume) an authorization of the given shape."""
        value = self._authorizations.get(authorization)
        if value is None:
            raise CalibrationRejected(
                "Unknown or already-used authorization: request a fresh preview"
            )
        if self._clock() - value["created_at"] > AUTHORIZATION_TTL_SECONDS:
            self._authorizations.pop(authorization, None)
            raise CalibrationRejected("Authorization expired: request a fresh preview")
        if bool(value.get("batch")) != batch:
            raise CalibrationRejected(
                "This authorization is for a batch: start it via the bulk endpoint"
                if value.get("batch")
                else "This authorization is for a single run: use /api/calibration/run"
            )
        return value

    def abort(self) -> dict:
        """Abort the active run and, if a batch is in flight, its whole queue."""
        if self._active is None and self._bulk is None:
            raise CalibrationRejected("No calibration run is active")
        if self._bulk is not None:
            self._bulk.abort_requested = True
        if self._active is not None:
            self._request_abort("manual abort")
        return self.status()

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _require_idle(self) -> None:
        if self._bulk is not None:
            raise CalibrationRejected(
                f"Calibration batch {self._bulk.batch_id} is in progress"
            )
        if self._active is not None:
            raise CalibrationRejected(
                f"A calibration of {self._active.plan['target']} is already running"
            )
        now = self._clock()
        if now < self._cooldown_until:
            raise CalibrationRejected(
                f"Cooling down: next run allowed in {int(self._cooldown_until - now)}s"
            )

    def _request_abort(self, reason: str) -> None:
        run = self._active
        if run is not None and run.abort_reason is None:
            run.abort_reason = reason

    # -- the ramp ---------------------------------------------------------------------

    async def _run(self, run: _ActiveRun) -> None:
        status = "completed"
        cancelled = False
        try:
            baselines: dict[str, float] = {}
            for index, spec in enumerate(run.plan["steps"]):
                step = StepResult(
                    rate_eps=spec["rate_eps"],
                    duration_s=spec["duration_s"],
                    started_at=self._clock(),
                )
                run.step_index = index
                run.current = step
                run.step_echo_rtts = []
                wire_mark = self._wire_latency_mark(run.plan["instance"])
                delivery_before = self._wire_delivery_totals(run.plan["instance"])

                offsets = spec.get("offsets")
                if offsets is not None:
                    await self._run_replay_step(run, step, offsets)
                else:
                    await self._run_step(run, step)
                await self._settle(run, step)

                self._finalize_step(run, step, wire_mark, delivery_before)
                run.steps.append(step)
                run.current = None
                # A replay has no stop rules: elevated latency under the
                # reproduced shape is the measurement, not a breach.
                if offsets is None and self._evaluate_stop(step, index, baselines):
                    break
        except _Abort as exc:
            status = "aborted"
            if run.abort_reason is None:
                run.abort_reason = str(exc)
            if run.current is not None:  # keep the partial step's curves
                run.steps.append(run.current)
                run.current = None
        except asyncio.CancelledError:
            status = "aborted"
            cancelled = True
            run.abort_reason = run.abort_reason or "collector shutdown"
            if run.current is not None:
                run.steps.append(run.current)
            raise  # finally records + releases before this propagates
        except Exception as exc:  # never leave a run half-alive on a bug
            status = "error"
            run.abort_reason = f"internal error: {exc}"
        finally:
            try:
                # Off the loop: the insert can wait seconds on the flush
                # worker's write transaction, and that wait must never
                # reach the loop's time-sensitive consumers. A cancelled
                # task (collector shutdown) writes in place instead of
                # awaiting mid-teardown.
                if cancelled:
                    self._record(run, status)
                else:
                    await asyncio.to_thread(self._record, run, status)
            finally:
                # Even a storage failure must release the run and arm the
                # cooldown: a stuck "running" state would block all runs.
                self._cooldown_until = self._clock() + COOLDOWN_SECONDS
                self._active = None

    async def _run_bulk(self) -> None:
        """Execute the batch queue sequentially: same rails per run, the full
        cooldown between runs, and any abort (manual or watchdog) stops the
        remainder: a batch never outruns the conditions it was authorized
        under."""
        bulk = self._bulk
        assert bulk is not None
        try:
            for index, plan in enumerate(bulk.queue):
                bulk.position = index
                bulk.state = "waiting_cooldown"
                while self._clock() < self._cooldown_until:
                    if bulk.abort_requested:
                        return
                    await self._sleep(1.0)
                if bulk.abort_requested:
                    return
                # Re-verify at its turn in the queue: the fleet may have
                # changed since the batch was authorized.
                try:
                    self._verify_plan_targets(plan)
                except CalibrationRejected as exc:
                    bulk.skipped.append(
                        {
                            "instance": plan["instance"],
                            "target": plan["target"],
                            "reason": str(exc),
                        }
                    )
                    await asyncio.to_thread(
                        self._record_skip, {**plan, "batch_id": bulk.batch_id}, str(exc)
                    )
                    continue
                bulk.state = "running"
                run = self._make_run({**plan, "batch_id": bulk.batch_id})
                self._active = run
                await self._run(run)
                if run.abort_reason is not None:
                    for rest in bulk.queue[index + 1 :]:
                        bulk.skipped.append(
                            {
                                "instance": rest["instance"],
                                "target": rest["target"],
                                "reason": f"batch stopped: {run.abort_reason}",
                            }
                        )
                    return
        finally:
            self._bulk = None

    async def _run_step(self, run: _ActiveRun, step: StepResult) -> None:
        interval = 1.0 / step.rate_eps
        bound = max(MIN_OUTSTANDING, math.ceil(step.rate_eps * OUTSTANDING_RTT_ALLOWANCE))
        targets = run.plan["targets"]
        step_end = step.started_at + step.duration_s
        # Send slots ride an absolute schedule with catch-up: per-iteration
        # work (publish awaits, interleaved ingest) and loop jitter then
        # cannot silently stretch the period and cap the achieved rate, the
        # way a relative sleep-per-send pacer does. Every due slot is
        # consumed exactly once: sent, or deferred when the outstanding
        # bound blocks it, so a saturated step always means real
        # backpressure rather than driver overhead.
        next_due = self._clock()
        while self._clock() < step_end:
            self._check_abort(run)
            self._expire_timeouts(run, step)
            self._check_stall(run, step)
            if run.sent_total >= MAX_TOTAL_READS:
                raise _Abort("total read cap reached")
            if self._clock() - run.started_at > MAX_RUN_SECONDS:
                raise _Abort("run wall-clock cap reached")
            while next_due <= self._clock() < step_end:
                if run.outstanding_total() < bound:
                    entry = targets[run.rr_index % len(targets)]
                    run.rr_index += 1
                    try:
                        await self._publish(entry["topic"], entry["payload"])
                    except Exception as exc:
                        raise _Abort(f"publish failed: {exc}") from exc
                    run.outstanding[entry["friendly_name"]].append(self._clock())
                    step.sent += 1
                    run.sent_total += 1
                    if run.sent_total >= MAX_TOTAL_READS:
                        break
                else:
                    step.deferred += 1
                next_due += interval
            wait = max(next_due - self._clock(), 0.001)
            before = self._clock()
            await self._sleep(wait)
            late = self._clock() - before - wait
            if late > 0:
                step.pacer_late_s += late
                if late * 1000.0 > step.pacer_max_late_ms:
                    step.pacer_max_late_ms = round(late * 1000.0, 1)
                if late >= PACER_STALL_SECONDS:
                    step.pacer_stalls += 1
                    step.pacer_stall_s += late

    async def _run_replay_step(
        self, run: _ActiveRun, step: StepResult, offsets: list[float]
    ) -> None:
        """Send on an explicit schedule instead of a constant interval.

        Unlike the ramp's consume-or-defer slots, a replay never drops a
        send: a slot blocked by the outstanding bound waits, and the slip
        shows in ``sent_offsets`` beside the requested schedule. Pacer
        integrity is measured on the shape-driven sleeps only; waiting out
        backpressure is intentional and is not pacing lateness (the global
        loop-lag monitor still covers stalls during those waits).
        """
        peak = float(run.plan["replay"]["requested_peak_1s_eps"])
        bound = min(
            max(MIN_OUTSTANDING, math.ceil(peak * OUTSTANDING_RTT_ALLOWANCE)),
            REPLAY_MAX_OUTSTANDING,
        )
        targets = run.plan["targets"]
        index = 0
        while index < len(offsets):
            self._check_abort(run)
            self._expire_timeouts(run, step)
            self._check_stall(run, step)
            if run.sent_total >= MAX_TOTAL_READS:
                raise _Abort("total read cap reached")
            if self._clock() - run.started_at > MAX_RUN_SECONDS:
                raise _Abort("run wall-clock cap reached")
            due = step.started_at + offsets[index]
            now = self._clock()
            if now < due:
                wait = due - now
                before = now
                await self._sleep(wait)
                late = self._clock() - before - wait
                if late > 0:
                    step.pacer_late_s += late
                    if late * 1000.0 > step.pacer_max_late_ms:
                        step.pacer_max_late_ms = round(late * 1000.0, 1)
                    if late >= PACER_STALL_SECONDS:
                        step.pacer_stalls += 1
                        step.pacer_stall_s += late
                continue
            if run.outstanding_total() >= bound:
                await self._sleep(REPLAY_BLOCKED_TICK_SECONDS)
                continue
            entry = targets[run.rr_index % len(targets)]
            run.rr_index += 1
            try:
                await self._publish(entry["topic"], entry["payload"])
            except Exception as exc:
                raise _Abort(f"publish failed: {exc}") from exc
            run.outstanding[entry["friendly_name"]].append(self._clock())
            step.sent += 1
            run.sent_total += 1
            step.sent_offsets.append(round(self._clock() - step.started_at, 3))
            index += 1

    async def _settle(self, run: _ActiveRun, step: StepResult) -> None:
        """Drain in-flight reads so steps don't bleed into each other."""
        run.state = "settling"
        deadline = self._clock() + READ_TIMEOUT_SECONDS + 1.0
        try:
            while run.outstanding_total() and self._clock() < deadline:
                self._check_abort(run)
                self._expire_timeouts(run, step)
                if run.outstanding_total():
                    await self._sleep(SETTLE_TICK_SECONDS)
            for fifo in run.outstanding.values():  # anything left is past timeout
                while fifo:
                    fifo.popleft()
                    step.timeouts += 1
        finally:
            run.state = "running"

    def _finalize_step(
        self,
        run: _ActiveRun,
        step: StepResult,
        wire_mark: float,
        delivery_before: tuple[int, int],
    ) -> None:
        step.achieved_eps = round(step.sent / step.duration_s, 2)
        step.echo_p50_ms, step.echo_p95_ms = _percentiles(run.step_echo_rtts)
        wire_samples = self._wire_latency_since(run.plan["instance"], wire_mark)
        step.wire_samples = len(wire_samples)
        step.wire_p50_ms, step.wire_p95_ms = _percentiles(wire_samples)
        _, failed_after = self._wire_delivery_totals(run.plan["instance"])
        step.delivery_failed_delta = failed_after - delivery_before[1]
        step.rtt_source = (
            "wire"
            if run.plan["rtt_source"] == "wire" and step.wire_p95_ms is not None
            else "echo"
        )

    def _evaluate_stop(
        self, step: StepResult, index: int, baselines: dict[str, float]
    ) -> bool:
        """Apply the §11 stop rules; True ends the ramp (knee found).

        Error-budget rules run before the RTT rule: lost replies inflate the
        FIFO-paired echo RTT, so when reads go unanswered the timeout ratio is
        the primary signal and the RTT number is derivative.
        """
        if step.sent and step.timeouts / step.sent > TIMEOUT_BREACH_RATIO:
            step.breach = "timeout_ratio"
            return True
        if step.delivery_failed_delta > DELIVERY_FAILURES_PER_STEP:
            step.breach = "delivery_failures"
            return True
        source = step.rtt_source or "echo"
        p95 = step.wire_p95_ms if source == "wire" else step.echo_p95_ms
        baseline = baselines.get(source)
        if p95 is not None and baseline is None:
            baselines[source] = p95  # first step observed on this source
        elif p95 is not None and baseline is not None and index > 0:
            threshold = max(RTT_BREACH_FACTOR * baseline, RTT_FLOORS_MS[source])
            if p95 > threshold:
                step.breach = "rtt_p95"
                return True
        if step.achieved_eps < SATURATION_RATIO * step.rate_eps:
            step.breach = "saturated"
            return True
        return False

    def _check_abort(self, run: _ActiveRun) -> None:
        if run.abort_reason is not None:
            raise _Abort(run.abort_reason)

    def _check_stall(self, run: _ActiveRun, step: StepResult) -> None:
        if (
            step.sent >= STALL_MIN_SENT
            and step.completed == 0
            and self._clock() - step.started_at > STALL_SECONDS
        ):
            raise _Abort("no replies at all: target or pipeline unresponsive")

    def _expire_timeouts(self, run: _ActiveRun, step: StepResult) -> None:
        now = self._clock()
        for fifo in run.outstanding.values():
            while fifo and now - fifo[0] > READ_TIMEOUT_SECONDS:
                fifo.popleft()
                step.timeouts += 1

    # -- record & read side ---------------------------------------------------------

    def _recent_ambient(self, instance: str) -> tuple[float, float] | None:
        """Command and state rates over the recent lookback, from the 10 s
        rollups: the preview's noisy-window signal."""
        since = int(self._clock()) - AMBIENT_LOOKBACK_SECONDS
        rows = self._db.connect().execute(
            "SELECT kind, SUM(count) AS n FROM series_10s "
            "WHERE instance = ? AND ts >= ? AND kind IN ('command', 'state') "
            "GROUP BY kind",
            (instance, since),
        ).fetchall()
        if not rows:
            return None
        counts = {row["kind"]: row["n"] or 0 for row in rows}
        return (
            counts.get("command", 0) / AMBIENT_LOOKBACK_SECONDS,
            counts.get("state", 0) / AMBIENT_LOOKBACK_SECONDS,
        )

    def _record_skip(self, plan: dict, reason: str) -> None:
        """A batch item that never ran still leaves a durable history row."""
        now = self._clock()
        detail = {
            "plan": plan,
            "steps": [],
            "knee": None,
            "abort_reason": reason,
            "bridge_errors": 0,
            "environment": plan.get("environment", {}),
        }
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO calibrations (instance, target, started_at, finished_at, "
            "status, knee_eps, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plan["instance"], plan["target"], now, now, "skipped", None, json.dumps(detail)),
        )
        conn.commit()

    def _record(self, run: _ActiveRun, status: str) -> None:
        replay = run.plan.get("mode") == "replay"
        good = [step for step in run.steps if step.breach is None]
        breached = [step for step in run.steps if step.breach is not None]
        degraded = [step for step in run.steps if _pacer_degraded(step)]
        knee_eps = None
        knee = None
        verdict = None
        if degraded:
            # The driver's own pacing lost time to event-loop interference:
            # a saturated pipeline and a stalled collector are then
            # indistinguishable, so no capacity limit is claimed (and a
            # replayed shape cannot be trusted as faithful).
            worst_ms = max(step.pacer_max_late_ms for step in degraded)
            verdict = (
                "unreliable: the collector's own send pacing degraded during "
                f"the run (worst stall {worst_ms:.0f} ms); "
                + (
                    "the reproduced shape is not trustworthy"
                    if replay
                    else "no capacity limit was recorded"
                )
            )
        elif status == "completed" and good and not replay:
            knee_eps = good[-1].achieved_eps
            knee = {
                "eps": knee_eps,
                "censored": not breached,  # ramp exhausted without a breach
                "breach": breached[0].breach if breached else None,
                "breach_rate_eps": breached[0].rate_eps if breached else None,
                "rtt_source": good[-1].rtt_source,
            }
        duration = max(self._clock() - run.started_at, 1.0)
        detail = {
            "plan": run.plan,
            "steps": [asdict(step) for step in run.steps],
            "knee": knee,
            "verdict": verdict,
            "replay": self._replay_result(run, verdict) if replay else None,
            "ambient": {
                "commands": run.ambient.get("command", 0),
                "state_reports": run.ambient.get("state", 0),
                "commands_per_s": round(run.ambient.get("command", 0) / duration, 2),
                "state_per_s": round(run.ambient.get("state", 0) / duration, 2),
            },
            "abort_reason": run.abort_reason if status != "completed" else None,
            "bridge_errors": run.bridge_errors,
            "environment": run.plan.get("environment", {}),
        }
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO calibrations (instance, target, started_at, finished_at, "
            "status, knee_eps, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run.plan["instance"],
                run.plan["target"],
                run.started_at,
                self._clock(),
                status,
                knee_eps,
                json.dumps(detail),
            ),
        )
        conn.commit()

    def _replay_result(self, run: _ActiveRun, verdict: str | None) -> dict:
        """The replay's outcome beside its request: the achieved shape, the
        latency and delivery observed under it, and the scenario prediction
        it was verifying when one exists."""
        plan_replay = run.plan.get("replay") or {}
        step = run.steps[0] if run.steps else None
        if step is None:
            return {**plan_replay, "achieved": None}
        achieved_peak = _offsets_peak_1s(step.sent_offsets)
        requested_peak = float(plan_replay.get("requested_peak_1s_eps") or 0)
        return {
            **plan_replay,
            "achieved": {
                "sent": step.sent,
                "timeouts": step.timeouts,
                "delivery_failed": step.delivery_failed_delta,
                "peak_1s_eps": round(achieved_peak, 1),
                "first_to_last_s": (
                    round(step.sent_offsets[-1] - step.sent_offsets[0], 3)
                    if len(step.sent_offsets) >= 2
                    else 0.0
                ),
                "wire_p50_ms": step.wire_p50_ms,
                "wire_p95_ms": step.wire_p95_ms,
                "wire_samples": step.wire_samples,
                "echo_p50_ms": step.echo_p50_ms,
                "echo_p95_ms": step.echo_p95_ms,
                "rtt_source": step.rtt_source,
            },
            # Compressed variants have no spacing to reproduce: fidelity is
            # every command going out. As-recorded fidelity is the achieved
            # peak staying near the requested one.
            "shape_reproduced": verdict is None
            and (
                step.sent >= int(run.plan.get("total_reads") or 0)
                if plan_replay.get("variant") == "compressed"
                else requested_peak > 0 and achieved_peak >= 0.9 * requested_peak
            ),
        }

    def status(self) -> dict:
        active = None
        run = self._active
        if run is not None:
            active = {
                "run_id": run.run_id,
                "instance": run.plan["instance"],
                "target": run.plan["target"],
                "mode": run.plan.get("mode", "single"),
                "state": run.state,
                "started_at": run.started_at,
                "step_index": run.step_index,
                "total_steps": len(run.plan["steps"]),
                "current": asdict(run.current) if run.current is not None else None,
                "outstanding": run.outstanding_total(),
                "sent_total": run.sent_total,
                "steps": [asdict(step) for step in run.steps],
                "abort_requested": run.abort_reason,
                "plan": run.plan,
            }
        bulk = None
        if self._bulk is not None:
            state = self._bulk
            bulk = {
                "batch_id": state.batch_id,
                "position": state.position,
                "total": len(state.queue),
                "state": state.state,
                "started_at": state.started_at,
                "runs": [
                    {"instance": plan["instance"], "target": plan["target"]}
                    for plan in state.queue
                ],
                "skipped": state.skipped,
                "abort_requested": state.abort_requested,
            }
        now = self._clock()
        return {
            "active": active,
            "bulk": bulk,
            "cooldown_until": self._cooldown_until if self._cooldown_until > now else None,
        }

    def history(self, limit: int = HISTORY_LIMIT) -> list[dict]:
        rows = self._db.connect().execute(
            "SELECT id, instance, target, started_at, finished_at, status, knee_eps, "
            "detail FROM calibrations ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            detail = json.loads(row["detail"])
            result.append(
                {
                    "id": row["id"],
                    "instance": row["instance"],
                    "target": row["target"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "status": row["status"],
                    "knee_eps": row["knee_eps"],
                    "steps": detail.get("steps", []),
                    "knee": detail.get("knee"),
                    "verdict": detail.get("verdict"),
                    "ambient": detail.get("ambient"),
                    "abort_reason": detail.get("abort_reason"),
                    "environment": detail.get("environment", {}),
                    "rtt_source": (detail.get("plan") or {}).get("rtt_source"),
                    "batch_id": (detail.get("plan") or {}).get("batch_id"),
                    "mode": (detail.get("plan") or {}).get("mode", "single"),
                    "replay": detail.get("replay"),
                }
            )
        return result

    def view(self) -> dict:
        return {**self.status(), "history": self.history()}

    # -- helpers -----------------------------------------------------------------------

    def _find_device(self, instance: str, target: str) -> dict:
        devices = self._devices(instance)
        if not devices:
            raise ValueError(f"No device registry for {instance}")
        for device in devices:
            if device.get("friendly_name") == target:
                if device.get("type") != "Router":
                    raise ValueError(
                        f"{target} is not a router: benchmarks only target "
                        "mains-powered routers"
                    )
                if not str(device.get("power_source") or "").startswith("Mains"):
                    raise ValueError(f"{target} is not mains-powered")
                if not device.get("get_attribute"):
                    raise ValueError(f"{target} exposes no gettable attribute")
                return device
        raise ValueError(f"Unknown device {target} on {instance}")

    def _instance_info(self, instance: str) -> dict | None:
        for info in self._instances():
            if info.get("base_topic") == instance:
                return info
        return None
