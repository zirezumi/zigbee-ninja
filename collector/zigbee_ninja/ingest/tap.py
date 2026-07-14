"""T2 wire-tap ingest: agent pcap stream → per-coordinator ASH/EZSP telemetry.

Each connected ninja-tap agent feeds a raw pcap byte stream (one long-lived
capture). This module reassembles the two directions of every coordinator TCP
flow and runs the S1-validated ASH + EZSP decoders, accumulating per-coordinator
frame stats. Coordinator endpoints come from discovery (registry adapter ports),
so a flow is attributed to its Z2M base topic.

Airtime and T1/T2 fusion build on these frames in later M4 steps; M4-live starts
with exact frame/byte/CRC/retransmit accounting at the NCP boundary.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..decode.ash import AshDecoder
from ..decode.ezsp import EzspStream
from ..decode.pcap import StreamBytes
from ..decode.pcap_stream import StreamingPcapReader


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


class TapIngest:
    def __init__(
        self,
        resolve_instance: Callable[[str, int], str | None],
        clock: Callable[[], float] = time.time,
    ):
        # resolve_instance(ip, port) -> base topic, from discovery adapter endpoints.
        self._resolve_instance = resolve_instance
        self._clock = clock
        self._readers: dict[str, StreamingPcapReader] = {}
        self._flows: dict[tuple, FlowState] = {}
        self.agents: dict[str, dict] = {}

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
                }
            )
        return {"agents": len(self.agents), "flows": flows}
