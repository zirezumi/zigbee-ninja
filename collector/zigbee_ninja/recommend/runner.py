"""Detector orchestration (V2_PROPOSAL.md §V2-5).

Detector runs ride a slow cadence off the engine's flush loop: the first
pass a few minutes after start (letting the registries populate from
retained topics), then hourly. Detectors read the persisted stores (chains,
ledger, rollups, calibrations) and registry snapshots; they never touch
live mesh state and never publish anything. Each detector is isolated: one
crashing is recorded in the run status and must not stop the others, and a
crashed detector's rows are left exactly as its last good run wrote them.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..store.db import Database
from . import groupcast, pacing, rebalance, redundancy, reporting
from .context import DetectorContext
from .store import Finding, RecommendationStore

RUN_INTERVAL_SECONDS = 3600.0
FIRST_RUN_DELAY_SECONDS = 300.0
LOOKBACK_SECONDS = 24 * 3600.0


class RecommendationEngine:
    """Owns the detector roster, the run cadence, and the store."""

    def __init__(
        self,
        db: Database,
        registry,
        pricing: Callable[[str], tuple[float | None, float | None]],
        clock: Callable[[], float] = time.time,
        events_log=None,
        topology_latest: Callable[[str], dict] | None = None,
    ):
        self._db = db
        self._registry = registry
        self._pricing = pricing
        self._events_log = events_log
        self._topology_latest = topology_latest
        self._clock = clock
        self.store = RecommendationStore(db, clock=clock)
        # Ordered detector roster: modules exposing NAME and detect(ctx).
        self._detectors: list = [pacing, groupcast, redundancy, reporting, rebalance]
        self._started_at = clock()
        self._last_run_at: float | None = None
        self._last_result: dict | None = None
        # A manual run-now can land while the hourly pass is mid-flight;
        # serializing them keeps sync's read-reconcile-write races out.
        self._run_lock = threading.Lock()

    def due(self) -> bool:
        now = self._clock()
        if now - self._started_at < FIRST_RUN_DELAY_SECONDS:
            return False
        if self._last_run_at is None:
            return True
        return now - self._last_run_at >= RUN_INTERVAL_SECONDS

    def _context(self) -> DetectorContext:
        from ..capacity import headroom

        snapshot = self._registry.snapshot()
        return DetectorContext(
            conn=self._db.connect(),
            now=self._clock(),
            lookback_seconds=LOOKBACK_SECONDS,
            instances=[i["base_topic"] for i in snapshot],
            instance_info={i["base_topic"]: i for i in snapshot},
            knees=headroom.latest_knees(self._db),
            is_group=self._registry.is_group,
            group_members=self._registry.group_members,
            groups=self._registry.groups,
            devices=self._registry.devices,
            router_count_for=self._registry.router_count_for,
            pricing=self._pricing,
            db=self._db,
            registry=self._registry,
            events_log=self._events_log,
            topology_latest=self._topology_latest,
        )

    def run(self) -> dict:
        """One full detector pass; safe on a worker thread (thread-local DB
        connections, snapshot-style registry reads, serialized passes)."""
        with self._run_lock:
            started = self._clock()
            ctx = self._context()
            detectors: dict[str, dict] = {}
            for detector in self._detectors:
                name = detector.NAME
                try:
                    findings: list[Finding] = detector.detect(ctx)
                except Exception as exc:  # isolate: one crash must not stop the rest
                    detectors[name] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                counts = self.store.sync(name, findings)
                detectors[name] = {"findings": len(findings), **counts}
            self._last_run_at = started
            self._last_result = {
                "ran_at": started,
                "duration_ms": round((self._clock() - started) * 1000.0, 1),
                "detectors": detectors,
            }
            return self._last_result

    def status(self) -> dict:
        return {
            "last_run_at": self._last_run_at,
            "next_run_due": (
                self._started_at + FIRST_RUN_DELAY_SECONDS
                if self._last_run_at is None
                else self._last_run_at + RUN_INTERVAL_SECONDS
            ),
            "detectors": [detector.NAME for detector in self._detectors],
            "last_result": self._last_result,
        }
