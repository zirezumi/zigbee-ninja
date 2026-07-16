"""T0 command-chain builder and redundant-command detection.

DESIGN.md paragraph 9: an MQTT /set|/get opens a chain; state publishes for the
target (or, for a group target, any member) inside an adaptive window are
`provoked`; state publishes matching no open chain are `autonomous`. At T0 the
only visible consequence of a command is the state echo: frame-level provoked
traffic arrives with the T1/T2 tiers.
"""

from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

CHAIN_WINDOWS = {"set": 1.5, "get": 3.0}
FINALIZE_LATENESS = 2.0
REDUNDANT_WINDOW = 5.0


def parse_command(suffix: str) -> tuple[str, str] | None:
    """Split a base-relative topic suffix into (target, verb) for /set|/get forms.

    Handles both `<target>/set` and attribute forms like `<target>/set/state`.
    """
    parts = suffix.split("/")
    if len(parts) >= 2 and parts[-1] in ("set", "get"):
        return "/".join(parts[:-1]), parts[-1]
    if len(parts) >= 3 and parts[-2] in ("set", "get"):
        return "/".join(parts[:-2]), parts[-2]
    return None


@dataclass
class Chain:
    instance: str
    target: str
    verb: str
    opened_at: float
    payload_size: int
    payload_digest: str
    client: str | None = None
    redundant: bool = False
    echoes: int = 0
    first_echo_ms: float | None = None
    finalized: bool = False

    def window(self) -> float:
        return CHAIN_WINDOWS.get(self.verb, CHAIN_WINDOWS["set"])

    def expires_at(self) -> float:
        return self.opened_at + self.window() + FINALIZE_LATENESS


class ChainTracker:
    """Tracks open chains per (instance, target) and finalizes them lazily.

    `resolve_members(instance, target)` lets a group command claim its members'
    state echoes; it returns an empty list for non-group targets.
    """

    def __init__(
        self,
        resolve_members: Callable[[str, str], list[str]] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._clock = clock
        self._resolve_members = resolve_members or (lambda _instance, _target: [])
        self._open: dict[tuple[str, str], deque[Chain]] = {}
        self._claims: dict[tuple[str, str], deque[Chain]] = {}
        self._finalized: list[Chain] = []
        self._recent_payloads: dict[tuple[str, str], tuple[float, str, Chain]] = {}

    # -- intake ---------------------------------------------------------------

    def on_command(
        self, instance: str, target: str, verb: str, payload: bytes, client: str | None = None
    ) -> Chain:
        now = self._clock()
        digest = hashlib.sha1(payload).hexdigest()[:12]
        chain = Chain(
            instance=instance,
            target=target,
            verb=verb,
            opened_at=now,
            payload_size=len(payload),
            payload_digest=digest,
            client=client,
        )
        if verb == "set":
            key = (instance, target)
            previous = self._recent_payloads.get(key)
            if previous is not None:
                prev_ts, prev_digest, _prev_chain = previous
                if prev_digest == digest and now - prev_ts <= REDUNDANT_WINDOW:
                    chain.redundant = True
            self._recent_payloads[key] = (now, digest, chain)

        self._open.setdefault((instance, target), deque()).append(chain)
        for member in self._resolve_members(instance, target):
            self._claims.setdefault((instance, member), deque()).append(chain)
        self._expire(now)
        return chain

    def on_state(self, instance: str, name: str) -> str:
        """Classify a state publish: 'provoked' if an open chain claims it."""
        now = self._clock()
        self._expire(now)
        for key in ((instance, name),):
            for registry in (self._open, self._claims):
                chains = registry.get(key)
                if not chains:
                    continue
                for chain in reversed(chains):
                    if not chain.finalized and now - chain.opened_at <= chain.window():
                        chain.echoes += 1
                        if chain.first_echo_ms is None:
                            chain.first_echo_ms = (now - chain.opened_at) * 1000.0
                        return "provoked"
        return "autonomous"

    def attribute_client(self, instance: str, target: str, client: str) -> bool:
        """Backfill the client id onto the newest unattributed chain for a target."""
        chains = self._open.get((instance, target))
        if not chains:
            return False
        for chain in reversed(chains):
            if chain.client is None:
                chain.client = client
                return True
        return False

    # -- finalization ---------------------------------------------------------

    def _expire(self, now: float) -> None:
        for registry in (self._open, self._claims):
            for key in list(registry):
                chains = registry[key]
                while chains and chains[0].expires_at() <= now:
                    chain = chains.popleft()
                    if registry is self._open and not chain.finalized:
                        chain.finalized = True
                        self._finalized.append(chain)
                if not chains:
                    del registry[key]
        stale = [
            key
            for key, (ts, _digest, _chain) in self._recent_payloads.items()
            if now - ts > REDUNDANT_WINDOW * 4
        ]
        for key in stale:
            del self._recent_payloads[key]

    def drain_finalized(self) -> list[Chain]:
        self._expire(self._clock())
        drained = self._finalized
        self._finalized = []
        return drained

    def open_count(self) -> int:
        return sum(len(chains) for chains in self._open.values())
