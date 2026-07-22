"""The read-only context a detector pass runs against."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class DetectorContext:
    """Everything a detector may read. Registry accessors are snapshot-style
    (whole-list replacement on refresh), safe to call from the worker thread."""

    conn: sqlite3.Connection
    now: float
    lookback_seconds: float
    instances: list[str]
    instance_info: dict[str, dict]  # registry snapshot rows by base topic
    knees: dict  # headroom.latest_knees(): {instance: {mode: {...}}}
    is_group: Callable[[str, str], bool]
    group_members: Callable[[str, str], list[str]]
    groups: Callable[[str], list[dict]]
    devices: Callable[[str], list[dict]]
    router_count_for: Callable[[str], int]
    pricing: Callable[[str], tuple[float | None, float | None]]
    # Scenario-engine dependencies (the rebalancing advisor prices its
    # proposals through capacity/scenario.py); absent in minimal harnesses,
    # and a detector that needs them must degrade to no findings.
    db: object | None = None
    registry: object | None = None
    events_log: object | None = None
    topology_latest: Callable[[str], dict] | None = None
    # headroom.utilization(): {instance: {channel_budget_pct, knee_eps, ...}}.
    # How contended each denominator currently is, so a detector can weigh what
    # a saving is worth instead of reporting its size alone (significance.py).
    # Empty in minimal harnesses; significance then reports band "unknown".
    utilization: dict = field(default_factory=dict)

    def window_start(self) -> float:
        return self.now - self.lookback_seconds
