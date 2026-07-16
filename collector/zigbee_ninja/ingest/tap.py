"""T2 wire-tap ingest: agent pcap stream → per-coordinator ASH/EZSP telemetry.

Each connected ninja-tap agent feeds a raw pcap byte stream (one long-lived
capture). This module reassembles the two directions of every coordinator TCP
flow, runs the S1-validated ASH + EZSP decoders, deep-parses the frames the
capacity model needs (decode/ezsp_params.py), and accumulates:

- per-coordinator frame stats and mesh-health counters,
- per-frame airtime (capacity/airtime.py) into 1 s buckets with 10 s drains,
- the wire-tier latency SLI: sendUnicast → messageSentHandler pairing by
  message tag on pcap timestamps (DESIGN.md §10) — the authoritative
  replacement for the T1 command→state-echo proxy.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from ..capacity import airtime
from ..decode import counters, ezsp_params
from ..decode.ash import AshDecoder
from ..decode.ezsp import EzspStream
from ..decode.pcap import StreamBytes
from ..decode.pcap_stream import StreamingPcapReader

AIRTIME_WINDOW_SECONDS = 300
AIRTIME_ROLLUP_SECONDS = 10

LATENCY_WINDOW_SECONDS = 300.0
LATENCY_MAX_SAMPLES = 1000

PENDING_MAX = 512
PENDING_TTL_SECONDS = 30.0

_EWMA_ALPHA = 0.05

# Passive avg_tx (§10 broadcast retry factor, superseding the §11 groupcast
# stage): readAndClearCounters responses are per-window deltas, so the
# coordinator's own mac_tx_broadcast over the broadcasts it originated
# (APS groupcasts + MTORR route discoveries), less the modeled radius-1
# link-status transmissions, measures its passive-ack retransmission factor
# directly — generalized to router relays on the same mesh.
AVG_TX_MIN_WINDOW_SECONDS = 60.0
# Zigbee2MQTT's ember adapter polls readAndClearCounters on a fixed 1 h
# setInterval (herdsman WATCHDOG_COUNTERS_FEED_INTERVAL), so real windows are
# ~3600 s plus scheduling jitter; the ceiling admits that plus exactly one
# missed harvest (two fused windows). A 3600 s ceiling rejected essentially
# every live sample.
AVG_TX_MAX_WINDOW_SECONDS = 7500.0
AVG_TX_MIN_BROADCASTS = 20  # denominator floor for a usable sample
AVG_TX_EWMA_ALPHA = 0.2  # samples arrive once per counter window, not per frame
# Passive-ack broadcast retransmission is capped at 3 transmissions total, so
# a residual above 3 per originated broadcast proves the window is
# contaminated by relayed foreign NWK broadcasts (see _update_avg_tx).
AVG_TX_PROTOCOL_MAX = 3.0
LINK_STATUS_INTERVAL_SECONDS = 15.0
_IDX_MAC_TX_BROADCAST = 1
_IDX_APS_TX_BROADCAST = 7
_IDX_ROUTE_DISCOVERY = 12

# Passive per-hop MAC retry rate (§10 unicast (1 + retry_rate) term): each
# readAndClearCounters response is a self-contained window, so the ratio
# mac_tx_unicast_retry / mac_tx_unicast_success needs no window length and no
# prior harvest. Measured at the coordinator's own hop; applied to unicast TX
# airtime, which is itself a single-hop lower-bound reconstruction.
RETRY_RATE_MIN_UNICASTS = 50  # denominator floor for a usable sample
RETRY_RATE_EWMA_ALPHA = 0.2  # samples arrive once per counter window
# macMaxFrameRetries is 3, so even a mesh where every success needed the full
# retry budget stays ≤ 3; anything above that is a counter anomaly, clamped.
RETRY_RATE_MAX = 3.0
_IDX_MAC_TX_UNICAST_SUCCESS = 3
_IDX_MAC_TX_UNICAST_RETRY = 4


class AirtimeTracker:
    """1 s airtime buckets per (instance, bucket) with watermarked 10 s drains."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        # (instance, bucket) -> {second: [airtime_us, frames]}
        self._buckets: dict[tuple[str, str], dict[int, list[float]]] = {}
        now = int(self._clock())
        self._drained = now - (now % AIRTIME_ROLLUP_SECONDS)

    def record(self, instance: str, bucket: str, airtime_us: float) -> None:
        now = int(self._clock())
        cutoff = now - AIRTIME_WINDOW_SECONDS
        with self._lock:
            per_second = self._buckets.setdefault((instance, bucket), {})
            cell = per_second.setdefault(now, [0.0, 0])
            cell[0] += airtime_us
            cell[1] += 1
            for stale in [ts for ts in per_second if ts < cutoff]:
                del per_second[stale]

    def snapshot(self) -> dict[str, dict]:
        """Per-instance airtime: per-bucket 60 s totals plus utilization views."""
        last_complete = int(self._clock()) - 1
        horizon = last_complete - 60
        result: dict[str, dict] = {}
        with self._lock:
            for (instance, bucket), per_second in self._buckets.items():
                view = result.setdefault(instance, {"buckets": {}})
                us_60s = frames_60s = 0
                for ts, (us, frames) in per_second.items():
                    if horizon < ts <= last_complete:
                        us_60s += us
                        frames_60s += frames
                if frames_60s:
                    view["buckets"][bucket] = {
                        "airtime_us_60s": round(us_60s, 1),
                        "frames_60s": frames_60s,
                    }
        for view in result.values():
            total_us = sum(b["airtime_us_60s"] for b in view["buckets"].values())
            us_per_s = total_us / 60.0
            view["us_per_s_60s"] = round(us_per_s, 1)
            view["airtime_pct_60s"] = round(us_per_s / 1_000_000.0 * 100.0, 3)
            view["budget_pct_60s"] = round(
                us_per_s / airtime.CHANNEL_BUDGET_US_PER_S * 100.0, 3
            )
            view["provenance"] = airtime.PROVENANCE
        return result

    def drain_completed_windows(self) -> list[tuple[int, str, str, float, int]]:
        """Rows (window_start, instance, bucket, airtime_us, frames), once each."""
        now = int(self._clock())
        current_window = now - (now % AIRTIME_ROLLUP_SECONDS)
        rows: list[tuple[int, str, str, float, int]] = []
        with self._lock:
            for window_start in range(self._drained, current_window, AIRTIME_ROLLUP_SECONDS):
                for (instance, bucket), per_second in self._buckets.items():
                    us = 0.0
                    frames = 0
                    for ts in range(window_start, window_start + AIRTIME_ROLLUP_SECONDS):
                        cell = per_second.get(ts)
                        if cell:
                            us += cell[0]
                            frames += int(cell[1])
                    if frames:
                        rows.append((window_start, instance, bucket, round(us, 1), frames))
            self._drained = current_window
        return rows


class WireLatency:
    """sendUnicast→messageSentHandler latency percentiles per instance.

    Latencies are differences of pcap capture timestamps, so no cross-host
    alignment is needed; rollup windows use the collector wall clock like the
    airtime tracker. Unicast-only by design: broadcast confirms fire on TX (no
    delivery wait) and would skew the SLI downward. The drained 10 s windows
    persist to latency_10s — the series continuous knee validation plots
    against load (DESIGN.md §10).
    """

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._latest_ts: dict[str, float] = {}
        self._windows: dict[tuple[int, str], list[float]] = {}

    def add(self, instance: str, ts: float, latency_ms: float) -> None:
        samples = self._samples.setdefault(instance, deque(maxlen=LATENCY_MAX_SAMPLES))
        samples.append((ts, latency_ms))
        self._latest_ts[instance] = max(self._latest_ts.get(instance, 0.0), ts)
        now = int(self._clock())
        window = (now - (now % AIRTIME_ROLLUP_SECONDS), instance)
        bucket = self._windows.setdefault(window, [])
        if len(bucket) < 2000:
            bucket.append(latency_ms)

    def drain_completed_windows(self) -> list[tuple[int, str, int, float, float, float]]:
        """Rows (window_start, instance, count, p50_ms, p95_ms, max_ms)."""
        now = int(self._clock())
        current_window = now - (now % AIRTIME_ROLLUP_SECONDS)
        rows: list[tuple[int, str, int, float, float, float]] = []
        for (window_start, instance) in sorted(
            key for key in self._windows if key[0] < current_window
        ):
            values = sorted(self._windows.pop((window_start, instance)))
            rows.append(
                (
                    window_start,
                    instance,
                    len(values),
                    round(statistics.median(values), 1),
                    round(values[min(len(values) - 1, int(len(values) * 0.95))], 1),
                    round(values[-1], 1),
                )
            )
        return rows

    def snapshot(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for instance, samples in self._samples.items():
            horizon = self._latest_ts.get(instance, 0.0) - LATENCY_WINDOW_SECONDS
            values = sorted(ms for ts, ms in samples if ts >= horizon)
            if not values:
                continue
            result[instance] = {
                "count": len(values),
                "p50_ms": round(statistics.median(values), 1),
                "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 1),
                "max_ms": round(values[-1], 1),
            }
        return result

    # -- calibration read side (DESIGN.md §11) ---------------------------------
    # Marks are pcap-clock timestamps: take one at a ramp-step boundary, then
    # collect the samples that arrived after it — no cross-clock comparison.

    def latest_ts(self, instance: str) -> float:
        return self._latest_ts.get(instance, 0.0)

    def samples_since(self, instance: str, mark: float) -> list[float]:
        return [ms for ts, ms in self._samples.get(instance, ()) if ts > mark]


@dataclass
class DirectionState:
    stream: StreamBytes = field(default_factory=StreamBytes)
    ash: AshDecoder = field(default_factory=AshDecoder)
    consumed: int = 0


@dataclass
class FlowState:
    instance: str | None
    coordinator: tuple[str, int]
    to_coord: DirectionState = field(default_factory=DirectionState)
    from_coord: DirectionState = field(default_factory=DirectionState)
    ezsp: EzspStream = field(default_factory=EzspStream)
    ezsp_frames: dict[str, int] = field(default_factory=dict)
    data_frames: int = 0
    last_seen: float = 0.0
    # wire-tier telemetry (M5)
    pending: dict[int, tuple[float, str]] = field(default_factory=dict)  # tag -> (ts, kind)
    delivery_ok: int = 0
    delivery_failed: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    route_records: int = 0
    route_errors: dict[str, int] = field(default_factory=dict)
    loopbacks: int = 0
    layout_mismatch: int = 0
    incoming_trailing: dict[str, int] = field(default_factory=dict)
    lqi_ewma: float | None = None
    rssi_ewma: float | None = None
    counters_last: list[int] | None = None
    counters_at: float | None = None
    avg_tx_ewma: float | None = None
    avg_tx_samples: int = 0
    avg_tx_rejected: int = 0
    avg_tx_last: dict | None = None
    retry_rate_ewma: float | None = None
    retry_rate_samples: int = 0
    retry_rate_last: dict | None = None


class TapIngest:
    def __init__(
        self,
        resolve_instance: Callable[[str, int], str | None],
        router_count: Callable[[str], int] | None = None,
        clock: Callable[[], float] = time.time,
        on_event: Callable[[float, str, str, str, int], None] | None = None,
    ):
        # resolve_instance(ip, port) -> base topic, from discovery adapter endpoints.
        self._resolve_instance = resolve_instance
        self._router_count = router_count or (lambda _instance: 0)
        self._clock = clock
        # on_event(pcap_ts, instance, frame_name, direction, size): every decoded
        # EZSP frame, for the raw event store (DESIGN.md §12).
        self._on_event = on_event
        self._readers: dict[str, StreamingPcapReader] = {}
        self._flows: dict[tuple, FlowState] = {}
        self.agents: dict[str, dict] = {}
        self.airtime = AirtimeTracker(clock)
        self.latency = WireLatency(clock)

    def register_agent(self, agent_id: str, meta: dict) -> None:
        self.agents[agent_id] = {
            "meta": meta,
            "connected_at": self._clock(),
            "bytes": 0,
            "segments": 0,
        }
        self._readers[agent_id] = StreamingPcapReader()

    def drop_agent(self, agent_id: str) -> None:
        self.agents.pop(agent_id, None)
        self._readers.pop(agent_id, None)

    def feed(self, agent_id: str, data: bytes) -> None:
        reader = self._readers.get(agent_id)
        if reader is None:
            return
        agent = self.agents[agent_id]
        agent["bytes"] += len(data)
        try:
            for segment in reader.feed(data):
                agent["segments"] += 1
                self._on_segment(segment)
        except Exception:
            # A malformed stream (e.g. a reconnect that skipped the pcap header)
            # must never crash the WS handler. Reset this agent's reader so the
            # next fresh session re-syncs on its global header.
            agent["reader_resets"] = agent.get("reader_resets", 0) + 1
            self._readers[agent_id] = StreamingPcapReader()

    def _flow_for(self, segment) -> FlowState | None:
        # Identify which endpoint is the coordinator (the tapped port side).
        for host, peer in ((segment.dst, segment.src), (segment.src, segment.dst)):
            instance = self._resolve_instance(host[0], host[1])
            if instance is not None:
                key = (peer, host)
                flow = self._flows.get(key)
                if flow is None:
                    flow = FlowState(instance=instance, coordinator=host)
                    self._flows[key] = flow
                return flow
        return None

    def _on_segment(self, segment) -> None:
        flow = self._flow_for(segment)
        if flow is None:
            return
        flow.last_seen = self._clock()
        to_coord = segment.dst == flow.coordinator
        direction = flow.to_coord if to_coord else flow.from_coord
        direction.stream.add(segment)
        fresh = bytes(direction.stream.data[direction.consumed :])
        direction.consumed = len(direction.stream.data)
        for frame in direction.ash.feed(fresh):
            if frame.type == "data" and frame.crc_ok:
                flow.data_frames += 1
                ezsp_frame = flow.ezsp.feed(frame.payload)
                if ezsp_frame is not None:
                    flow.ezsp_frames[ezsp_frame.name] = (
                        flow.ezsp_frames.get(ezsp_frame.name, 0) + 1
                    )
                    self._on_ezsp(flow, to_coord, segment.ts, ezsp_frame, frame.payload)

    # -- deep decode → airtime + latency (M5) ----------------------------------

    def _on_ezsp(
        self, flow: FlowState, to_coord: bool, ts: float, ezsp_frame, payload: bytes
    ) -> None:
        instance = flow.instance
        if instance is None:
            return
        params = payload[5 if ezsp_frame.header_format == "extended" else 3 :]
        name = ezsp_frame.name
        if self._on_event is not None:
            self._on_event(ts, instance, name, "out" if to_coord else "in", len(payload))

        if to_coord and not ezsp_frame.is_response:
            if name == "sendUnicast":
                sent = ezsp_params.parse_send_unicast(params)
                if sent is None:
                    flow.layout_mismatch += 1
                    return
                self._track_pending(flow, sent.tag, ts, "unicast")
                self.airtime.record(
                    instance,
                    "tx_unicast",
                    airtime.unicast_airtime_us(
                        sent.payload_len, retry_rate=flow.retry_rate_ewma or 0.0
                    ),
                )
            elif name == "sendMulticast":
                sent = ezsp_params.parse_send_multicast(params)
                if sent is None:
                    flow.layout_mismatch += 1
                    return
                self._track_pending(flow, sent.tag, ts, "groupcast")
                self.airtime.record(
                    instance,
                    "tx_groupcast",
                    airtime.groupcast_airtime_us(
                        sent.payload_len,
                        self._router_count(instance),
                        avg_tx=flow.avg_tx_ewma or airtime.DEFAULT_AVG_TX,
                    ),
                )
            elif name == "sendBroadcast":
                sent = ezsp_params.parse_send_broadcast(params)
                if sent.tag is not None:
                    self._track_pending(flow, sent.tag, ts, "groupcast")
                self.airtime.record(
                    instance,
                    "tx_groupcast",
                    airtime.groupcast_airtime_us(
                        sent.payload_len,
                        self._router_count(instance),
                        avg_tx=flow.avg_tx_ewma or airtime.DEFAULT_AVG_TX,
                    ),
                )
            return

        if not ezsp_frame.is_callback:
            if name in ("readAndClearCounters", "readCounters") and ezsp_frame.is_response:
                counter_values = ezsp_params.parse_counters(params)
                if counter_values is not None:
                    now = self._clock()
                    if name == "readAndClearCounters":
                        if flow.counters_at is not None:
                            # avg_tx needs the window length (link-status
                            # subtraction), so it waits for a second harvest.
                            self._update_avg_tx(flow, counter_values, now - flow.counters_at)
                        # The retry ratio is scale-invariant, so every
                        # clearing read is a self-contained sample.
                        self._update_retry_rate(flow, counter_values)
                    flow.counters_last = counter_values
                    flow.counters_at = now
            return

        if name == "messageSentHandler":
            sent = ezsp_params.parse_message_sent(params)
            if sent is None:
                flow.layout_mismatch += 1
                return
            status_key = f"0x{sent.status:04x}"
            flow.statuses[status_key] = flow.statuses.get(status_key, 0) + 1
            if sent.ok:
                flow.delivery_ok += 1
            else:
                flow.delivery_failed += 1
            pending = flow.pending.pop(sent.tag, None)
            if pending is not None and pending[1] == "unicast":
                self.latency.add(instance, ts, (ts - pending[0]) * 1000.0)
        elif name == "incomingMessageHandler":
            incoming = ezsp_params.parse_incoming(params)
            if incoming is None:
                flow.layout_mismatch += 1
                return
            if incoming.loopback:
                flow.loopbacks += 1
                return
            if incoming.trailing:
                key = incoming.trailing.hex()
                flow.incoming_trailing[key] = flow.incoming_trailing.get(key, 0) + 1
            group_addressed = incoming.msg_type in (
                ezsp_params.INCOMING_MULTICAST,
                ezsp_params.INCOMING_BROADCAST,
            )
            self.airtime.record(
                instance,
                "rx",
                airtime.incoming_airtime_us(
                    incoming.payload_len,
                    group_addressed=group_addressed,
                    acked=incoming.acked,
                ),
            )
            flow.lqi_ewma = _ewma(flow.lqi_ewma, incoming.lqi)
            flow.rssi_ewma = _ewma(flow.rssi_ewma, incoming.rssi)
        elif name == "incomingRouteRecordHandler":
            record = ezsp_params.parse_route_record(params)
            if record is None:
                flow.layout_mismatch += 1
                return
            flow.route_records += 1
            self.airtime.record(
                instance, "rx_mesh", airtime.route_record_airtime_us(record.relay_count)
            )
            flow.lqi_ewma = _ewma(flow.lqi_ewma, record.lqi)
            flow.rssi_ewma = _ewma(flow.rssi_ewma, record.rssi)
        elif name == "incomingNetworkStatusHandler":
            status = ezsp_params.parse_network_status(params)
            if status is None:
                flow.layout_mismatch += 1
                return
            code_key = f"0x{status.code:02x}"
            flow.route_errors[code_key] = flow.route_errors.get(code_key, 0) + 1
            self.airtime.record(instance, "rx_mesh", airtime.network_status_airtime_us())
        # incomingRouteErrorHandler mirrors incomingNetworkStatusHandler for the
        # same event; counting both would double the error and its airtime.

    def wire_covers(self, instance: str, max_age: float = 60.0) -> bool:
        """A live tap flow exists for this coordinator (calibration RTT source)."""
        now = self._clock()
        return any(
            flow.instance == instance and now - flow.last_seen < max_age
            for flow in self._flows.values()
        )

    def wire_delivery_totals(self, instance: str) -> tuple[int, int]:
        """(delivery_ok, delivery_failed) summed across the instance's flows.

        Reconnects key new flows, so totals are summed, not read from one flow;
        callers diff totals across a window (calibration error accounting)."""
        ok = failed = 0
        for flow in self._flows.values():
            if flow.instance == instance:
                ok += flow.delivery_ok
                failed += flow.delivery_failed
        return ok, failed

    def instance_wire_totals(self) -> dict[str, dict]:
        """Per-instance cumulative wire health counters plus the current avg_tx.

        Counters are summed across an instance's flows (monotonic within a
        collector lifetime); avg_tx comes from the most recently seen flow —
        the EWMA lives per flow, and reconnects start a fresh one."""
        out: dict[str, dict] = {}
        freshest: dict[str, float] = {}
        for flow in self._flows.values():
            if flow.instance is None:
                continue
            entry = out.setdefault(
                flow.instance,
                {"delivery_failed": 0, "layout_mismatch": 0, "avg_tx": None},
            )
            entry["delivery_failed"] += flow.delivery_failed
            entry["layout_mismatch"] += flow.layout_mismatch
            if flow.avg_tx_ewma is not None and flow.last_seen >= freshest.get(
                flow.instance, 0.0
            ):
                entry["avg_tx"] = round(flow.avg_tx_ewma, 2)
                freshest[flow.instance] = flow.last_seen
        return out

    def _update_avg_tx(self, flow: FlowState, values: list[int], window: float) -> None:
        """One avg_tx sample per counter window (§10 broadcast retry factor).

        avg_tx = (mac_tx_broadcast − modeled link-status TXs)
                 / (APS broadcasts + MTORR route discoveries)

        Link status is a radius-1 broadcast on a ~15 s cadence, transmitted
        once (no passive-ack retry), so it inflates the MAC count without
        belonging to the retried population. A missed harvest stretches the
        apparent window and over-subtracts slightly; the EWMA damps it.

        mac_tx_broadcast also counts the coordinator's *relays* of other
        nodes' NWK broadcasts (route requests and the like) — traffic that
        never crosses the EZSP boundary, so it cannot be subtracted. A window
        whose residual exceeds the passive-ack maximum of 3 transmissions per
        originated broadcast is therefore provably relay-contaminated and is
        discarded rather than clamped: a pinned ceiling would silently
        inflate every groupcast airtime figure. Quiet windows (residual ≤ 3)
        remain honest retry-factor samples; on meshes with steady relay
        traffic the modeled default simply stays in force, visibly.
        """
        if not AVG_TX_MIN_WINDOW_SECONDS <= window <= AVG_TX_MAX_WINDOW_SECONDS:
            return
        if len(values) <= _IDX_ROUTE_DISCOVERY:
            return
        mac_tx = values[_IDX_MAC_TX_BROADCAST]
        originated = values[_IDX_APS_TX_BROADCAST] + values[_IDX_ROUTE_DISCOVERY]
        link_status_est = window / LINK_STATUS_INTERVAL_SECONDS
        if originated < AVG_TX_MIN_BROADCASTS or mac_tx <= link_status_est:
            return
        raw = (mac_tx - link_status_est) / originated
        detail = {
            "raw": round(raw, 3),
            "mac_tx_broadcast": mac_tx,
            "aps_tx_broadcast": values[_IDX_APS_TX_BROADCAST],
            "route_discoveries": values[_IDX_ROUTE_DISCOVERY],
            "link_status_estimate": round(link_status_est, 1),
            "window_seconds": round(window, 1),
        }
        if raw > AVG_TX_PROTOCOL_MAX:
            flow.avg_tx_rejected += 1
            flow.avg_tx_last = {
                **detail,
                "accepted": False,
                "reason": "relay_contaminated",
            }
            return
        sample = max(1.0, raw)
        if flow.avg_tx_ewma is None:
            flow.avg_tx_ewma = sample
        else:
            flow.avg_tx_ewma += AVG_TX_EWMA_ALPHA * (sample - flow.avg_tx_ewma)
        flow.avg_tx_samples += 1
        flow.avg_tx_last = {**detail, "sample": round(sample, 3), "accepted": True}

    def _update_retry_rate(self, flow: FlowState, values: list[int]) -> None:
        """One per-hop MAC retry-rate sample per clearing counter window.

        retry_rate = mac_tx_unicast_retry / mac_tx_unicast_success — the share
        of unicast transmissions the coordinator's own radio had to repeat.
        Retries for eventually-failed frames count in the numerator (their
        airtime burned all the same), so the ratio can exceed the per-success
        retry budget on very lossy links; RETRY_RATE_MAX bounds anomalies.
        Feeds the §10 unicast (1 + retry_rate) term for this flow's TX cost;
        the multiplier reflects the coordinator hop only.
        """
        if len(values) <= _IDX_MAC_TX_UNICAST_RETRY:
            return
        success = values[_IDX_MAC_TX_UNICAST_SUCCESS]
        retries = values[_IDX_MAC_TX_UNICAST_RETRY]
        if success < RETRY_RATE_MIN_UNICASTS:
            return
        sample = min(retries / success, RETRY_RATE_MAX)
        if flow.retry_rate_ewma is None:
            flow.retry_rate_ewma = sample
        else:
            flow.retry_rate_ewma += RETRY_RATE_EWMA_ALPHA * (sample - flow.retry_rate_ewma)
        flow.retry_rate_samples += 1
        flow.retry_rate_last = {
            "sample": round(sample, 4),
            "mac_tx_unicast_success": success,
            "mac_tx_unicast_retry": retries,
        }

    def _track_pending(self, flow: FlowState, tag: int, ts: float, kind: str) -> None:
        flow.pending[tag] = (ts, kind)
        if len(flow.pending) > PENDING_MAX:
            cutoff = ts - PENDING_TTL_SECONDS
            for stale_tag in [t for t, (t0, _) in flow.pending.items() if t0 < cutoff]:
                del flow.pending[stale_tag]

    def stats(self) -> dict:
        flows = []
        for flow in self._flows.values():
            flows.append(
                {
                    "instance": flow.instance,
                    "coordinator": f"{flow.coordinator[0]}:{flow.coordinator[1]}",
                    "protocol_version": flow.ezsp.protocol_version,
                    "data_frames": flow.data_frames,
                    "ezsp_frames": dict(
                        sorted(flow.ezsp_frames.items(), key=lambda kv: -kv[1])
                    ),
                    "to_coord_ash": dict(flow.to_coord.ash.stats.frames),
                    "from_coord_ash": dict(flow.from_coord.ash.stats.frames),
                    "crc_errors": (
                        flow.to_coord.ash.stats.crc_errors
                        + flow.from_coord.ash.stats.crc_errors
                    ),
                    "retransmits": (
                        flow.to_coord.ash.stats.retransmits
                        + flow.from_coord.ash.stats.retransmits
                    ),
                    "last_seen": flow.last_seen,
                    "wire": {
                        "delivery_ok": flow.delivery_ok,
                        "delivery_failed": flow.delivery_failed,
                        "statuses": dict(flow.statuses),
                        "route_records": flow.route_records,
                        "route_errors": dict(flow.route_errors),
                        "loopbacks": flow.loopbacks,
                        "layout_mismatch": flow.layout_mismatch,
                        "incoming_trailing": dict(flow.incoming_trailing),
                        "lqi_ewma": None if flow.lqi_ewma is None else round(flow.lqi_ewma, 1),
                        "rssi_ewma": (
                            None if flow.rssi_ewma is None else round(flow.rssi_ewma, 1)
                        ),
                        "pending_sends": len(flow.pending),
                        # Z2M itself polls readAndClearCounters; we harvest the
                        # responses passively and label them (spike S2).
                        "counters_at": flow.counters_at,
                        "counters": (
                            None
                            if flow.counters_last is None
                            else counters.label_counters(flow.counters_last)
                        ),
                        "counters_provenance": counters.PROVENANCE,
                        # §10 broadcast retry factor, measured passively from
                        # the coordinator's own TX counters (supersedes the
                        # §11 groupcast stage).
                        "avg_tx": (
                            None if flow.avg_tx_ewma is None else round(flow.avg_tx_ewma, 2)
                        ),
                        "avg_tx_samples": flow.avg_tx_samples,
                        "avg_tx_rejected": flow.avg_tx_rejected,
                        "avg_tx_last": flow.avg_tx_last,
                        "avg_tx_provenance": (
                            "measured (coordinator tx, generalized to relays)"
                            if flow.avg_tx_ewma is not None
                            else f"modeled (default {airtime.DEFAULT_AVG_TX})"
                            + (
                                f"; {flow.avg_tx_rejected} relay-contaminated "
                                "windows discarded"
                                if flow.avg_tx_rejected
                                else ""
                            )
                        ),
                        # §10 unicast (1 + retry_rate) term, from the same
                        # harvested counter windows.
                        "retry_rate": (
                            None
                            if flow.retry_rate_ewma is None
                            else round(flow.retry_rate_ewma, 4)
                        ),
                        "retry_rate_samples": flow.retry_rate_samples,
                        "retry_rate_last": flow.retry_rate_last,
                        "retry_rate_provenance": (
                            "measured (coordinator hop, MAC counters)"
                            if flow.retry_rate_ewma is not None
                            else "default (0 — awaiting counter windows)"
                        ),
                    },
                }
            )
        return {
            "agents": len(self.agents),
            # The footprint page lists every foothold (DESIGN.md P2) — tap
            # agents included, with the hello metadata they self-reported.
            "agent_details": [
                {
                    "meta": agent["meta"],
                    "connected_at": agent["connected_at"],
                    "bytes": agent["bytes"],
                    "segments": agent["segments"],
                }
                for agent in self.agents.values()
            ],
            "flows": flows,
            "airtime": self.airtime.snapshot(),
            "latency": self.latency.snapshot(),
        }


def _ewma(current: float | None, sample: float) -> float:
    if current is None:
        return float(sample)
    return current + _EWMA_ALPHA * (sample - current)
