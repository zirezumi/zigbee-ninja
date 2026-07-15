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


class TapIngest:
    def __init__(
        self,
        resolve_instance: Callable[[str, int], str | None],
        router_count: Callable[[str], int] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        # resolve_instance(ip, port) -> base topic, from discovery adapter endpoints.
        self._resolve_instance = resolve_instance
        self._router_count = router_count or (lambda _instance: 0)
        self._clock = clock
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

        if to_coord and not ezsp_frame.is_response:
            if name == "sendUnicast":
                sent = ezsp_params.parse_send_unicast(params)
                if sent is None:
                    flow.layout_mismatch += 1
                    return
                self._track_pending(flow, sent.tag, ts, "unicast")
                self.airtime.record(
                    instance, "tx_unicast", airtime.unicast_airtime_us(sent.payload_len)
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
                        sent.payload_len, self._router_count(instance)
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
                        sent.payload_len, self._router_count(instance)
                    ),
                )
            return

        if not ezsp_frame.is_callback:
            if name in ("readAndClearCounters", "readCounters") and ezsp_frame.is_response:
                counters = ezsp_params.parse_counters(params)
                if counters is not None:
                    flow.counters_last = counters
                    flow.counters_at = self._clock()
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
