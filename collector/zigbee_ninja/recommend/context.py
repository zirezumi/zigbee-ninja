"""The read-only context a detector pass runs against."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass


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

    def window_start(self) -> float:
        return self.now - self.lookback_seconds
