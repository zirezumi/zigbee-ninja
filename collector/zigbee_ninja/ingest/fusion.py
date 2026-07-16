"""T1/T2 frame fusion (DESIGN.md §8): incoming radio frames observed twice.

One physical incoming frame is seen at the wire (T2 incomingMessageHandler:
sender short address + the ZCL transaction sequence in the message header) and
at the Z2M boundary (T1 probe deviceMessage: probe v0.4 emits the same ZCL
sequence). Records fuse on (instance, sender nwk, zcl seq) inside a short
watermark. The disagreement counters are the point of the exercise:
**wire-only** frames quantify what Z2M-level observation misses (frames Z2M
consumes without emitting a device event: default responses, interview
traffic, unknown devices); **probe-only** frames flag wire-capture gaps.
Matched pairs also yield a per-instance probe↔pcap clock-offset estimate,
the §8 cross-source alignment signal.

Outgoing frames have no Z2M-boundary *frame* event to fuse with (commands are
observed as MQTT messages, not radio frames), so fusion is incoming-only.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

WATERMARK_SECONDS = 5.0
WINDOW_SECONDS = 300.0
MAX_PENDING = 2048  # per instance per side; overflow drops the oldest, counted
OFFSET_EWMA_ALPHA = 0.1


_UNMATCHED_SENDERS_CAP = 512
_EXPIRED_ALIGN_WINDOW_SECONDS = 8.0
_EXPIRED_RING = 64


def _record_seq_delta(state: _InstanceState, side: int, arrival_ts: float, key: tuple) -> None:
    """Align an expired-unmatched entry with a near-in-time expired entry from
    the same sender on the OTHER side and histogram (probe_seq − wire_seq)
    mod 256. A histogram dominated by one value = a systematic sequence shift
    between the tiers; a flat histogram = the unmatched entries are simply
    frames the other tier never saw."""
    nwk, seq = key
    own = state.expired_wire if side == 0 else state.expired_probe
    other = state.expired_probe if side == 0 else state.expired_wire
    # Closest-in-time counterpart from the same sender, consumed one-to-one so
    # a burst of expiries pairs up cleanly instead of smearing onto one entry.
    best: tuple[float, int, int] | None = None
    for index, (other_ts, other_nwk, other_seq) in enumerate(other):
        if other_nwk != nwk:
            continue
        gap = abs(other_ts - arrival_ts)
        if gap > _EXPIRED_ALIGN_WINDOW_SECONDS:
            continue
        if best is None or gap < best[0]:
            best = (gap, index, other_seq)
    if best is not None:
        _gap, index, other_seq = best
        del other[index]
        probe_seq, wire_seq = (other_seq, seq) if side == 0 else (seq, other_seq)
        delta = (probe_seq - wire_seq) % 256
        state.seq_delta_histogram[delta] = state.seq_delta_histogram.get(delta, 0) + 1
        return
    own.append((arrival_ts, nwk, seq))
    while len(own) > _EXPIRED_RING:
        own.popleft()


@dataclass
class _InstanceState:
    # (nwk, seq) -> deque of (arrival_ts, source_ts); a device can legally
    # reuse a sequence inside the watermark only after 256 messages, so the
    # deque is nearly always length 1.
    pending_wire: dict[tuple[int, int], deque] = field(default_factory=dict)
    pending_probe: dict[tuple[int, int], deque] = field(default_factory=dict)
    outcomes: deque = field(default_factory=deque)  # (arrival_ts, kind)
    # nwk -> [wire_only, probe_only] cumulative expiry counts: the diagnostic
    # that says WHICH devices fail to fuse, and from which side. A device
    # failing equally on both sides points at a join-key disagreement (its
    # sequences differ between tiers); one-sided failure points at
    # visibility (frames one tier never sees).
    unmatched_by_nwk: dict[int, list[int]] = field(default_factory=dict)
    matched_by_nwk: dict[int, int] = field(default_factory=dict)
    # Expired-unmatched entries kept briefly per side so a probe expiry can be
    # aligned with a near-in-time wire expiry from the same sender: the
    # (probe_seq − wire_seq) mod 256 histogram exposes any systematic
    # sequence shift between the tiers in one glance.
    expired_wire: deque = field(default_factory=deque)  # (arrival_ts, nwk, seq)
    expired_probe: deque = field(default_factory=deque)
    seq_delta_histogram: dict[int, int] = field(default_factory=dict)
    offset_ewma_ms: float | None = None
    offset_samples: int = 0
    overflow_drops: int = 0
    last_wire_at: float | None = None
    last_probe_seq_at: float | None = None


class FusionTracker:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._instances: dict[str, _InstanceState] = {}

    # -- intake -----------------------------------------------------------------

    def on_wire(self, instance: str, nwk: int, zcl_seq: int, pcap_ts: float) -> None:
        now = self._clock()
        with self._lock:
            state = self._instances.setdefault(instance, _InstanceState())
            state.last_wire_at = now
            self._sweep(state, now)
            key = (nwk, zcl_seq)
            waiting = state.pending_probe.get(key)
            if waiting:
                _arrival, probe_ts = waiting.popleft()
                if not waiting:
                    del state.pending_probe[key]
                self._record_match(state, now, nwk, probe_ts, pcap_ts)
                return
            self._enqueue(state.pending_wire, key, (now, pcap_ts), state)

    def on_probe(self, instance: str, nwk: int, zcl_seq: int, probe_ts: float) -> None:
        now = self._clock()
        with self._lock:
            state = self._instances.setdefault(instance, _InstanceState())
            state.last_probe_seq_at = now
            self._sweep(state, now)
            key = (nwk, zcl_seq)
            waiting = state.pending_wire.get(key)
            if waiting:
                _arrival, pcap_ts = waiting.popleft()
                if not waiting:
                    del state.pending_wire[key]
                self._record_match(state, now, nwk, probe_ts, pcap_ts)
                return
            self._enqueue(state.pending_probe, key, (now, probe_ts), state)

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _enqueue(
        pending: dict, key: tuple[int, int], entry: tuple, state: _InstanceState
    ) -> None:
        pending.setdefault(key, deque()).append(entry)
        if sum(len(q) for q in pending.values()) > MAX_PENDING:
            oldest_key = min(pending, key=lambda k: pending[k][0][0])
            pending[oldest_key].popleft()
            if not pending[oldest_key]:
                del pending[oldest_key]
            state.overflow_drops += 1

    def _record_match(
        self, state: _InstanceState, now: float, nwk: int, probe_ts: float, pcap_ts: float
    ) -> None:
        state.outcomes.append((now, "matched"))
        state.matched_by_nwk[nwk] = state.matched_by_nwk.get(nwk, 0) + 1
        offset_ms = (probe_ts - pcap_ts) * 1000.0
        if state.offset_ewma_ms is None:
            state.offset_ewma_ms = offset_ms
        else:
            state.offset_ewma_ms += OFFSET_EWMA_ALPHA * (offset_ms - state.offset_ewma_ms)
        state.offset_samples += 1

    @staticmethod
    def _sweep(state: _InstanceState, now: float) -> None:
        for pending, kind, side in (
            (state.pending_wire, "wire_only", 0),
            (state.pending_probe, "probe_only", 1),
        ):
            expired = []
            for key, queue in pending.items():
                while queue and now - queue[0][0] > WATERMARK_SECONDS:
                    entry = queue.popleft()
                    state.outcomes.append((now, kind))
                    if (
                        key[0] in state.unmatched_by_nwk
                        or len(state.unmatched_by_nwk) < _UNMATCHED_SENDERS_CAP
                    ):
                        counts = state.unmatched_by_nwk.setdefault(key[0], [0, 0])
                        counts[side] += 1
                    _record_seq_delta(state, side, entry[0], key)
                if not queue:
                    expired.append(key)
            for key in expired:
                del pending[key]
        while state.outcomes and now - state.outcomes[0][0] > WINDOW_SECONDS:
            state.outcomes.popleft()

    # -- read side -----------------------------------------------------------------

    def snapshot(self) -> dict[str, dict]:
        now = self._clock()
        result: dict[str, dict] = {}
        with self._lock:
            for instance, state in self._instances.items():
                self._sweep(state, now)
                counts = {"matched": 0, "wire_only": 0, "probe_only": 0}
                for _ts, kind in state.outcomes:
                    counts[kind] += 1
                wire_recent = state.last_wire_at is not None and now - state.last_wire_at < 60
                probe_recent = (
                    state.last_probe_seq_at is not None
                    and now - state.last_probe_seq_at < 120
                )
                if probe_recent and wire_recent:
                    fusion_state = "fusing"
                elif wire_recent:
                    # Wire frames flow but no sequenced probe events: the
                    # deployed probe predates v0.4 (or no probe at all).
                    fusion_state = "awaiting probe v0.4"
                elif probe_recent:
                    fusion_state = "no wire coverage"
                else:
                    fusion_state = "idle"
                top_unmatched = sorted(
                    (
                        {
                            "nwk": nwk,
                            "wire_only": wire_count,
                            "probe_only": probe_count,
                            "matched": state.matched_by_nwk.get(nwk, 0),
                        }
                        for nwk, (wire_count, probe_count) in state.unmatched_by_nwk.items()
                    ),
                    key=lambda row: -(row["wire_only"] + row["probe_only"]),
                )[:8]
                result[instance] = {
                    "state": fusion_state,
                    "matched_5m": counts["matched"],
                    "wire_only_5m": counts["wire_only"],
                    "probe_only_5m": counts["probe_only"],
                    "clock_offset_ms": (
                        None
                        if state.offset_ewma_ms is None
                        else round(state.offset_ewma_ms, 1)
                    ),
                    "offset_samples": state.offset_samples,
                    "overflow_drops": state.overflow_drops,
                    # Cumulative since start: which senders fail to fuse, and
                    # from which side: the fusion-quality drill-down.
                    "top_unmatched": top_unmatched,
                    "seq_delta_histogram": dict(
                        sorted(
                            state.seq_delta_histogram.items(),
                            key=lambda kv: -kv[1],
                        )[:8]
                    ),
                }
        return result
