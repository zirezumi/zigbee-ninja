"""T0 command-chain builder and redundant-command detection.

DESIGN.md paragraph 9: an MQTT /set|/get opens a chain; state publishes for the
target (or, for a group target, any member) inside an adaptive window are
`provoked`; state publishes matching no open chain are `autonomous`. At T0 the
only visible consequence of a command is the state echo: frame-level provoked
traffic arrives with the T1/T2 tiers.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

CHAIN_WINDOWS = {"set": 1.5, "get": 3.0}
FINALIZE_LATENESS = 2.0
REDUNDANT_WINDOW = 5.0
# Per-key digests are capped so one pathological payload cannot bloat the row.
# Zigbee /set payloads are a handful of scalar parameters; anything past this is
# not a command this analysis has anything useful to say about.
MAX_PAYLOAD_KEYS = 24
KEY_DIGEST_CHARS = 8

# Stored as a chain's commander when an HA publish was seen for the command but
# cannot be told apart from another candidate. Deliberately distinct from a NULL
# client, which renders as "(unattributed)" and means no HA publish was seen at
# all: "nobody told us" and "several might have" are different failures and want
# different fixes. Consumers must not count this as a real commander.
AMBIGUOUS_COMMANDER = "(ambiguous)"


def command_digest(payload: bytes) -> str:
    """Fingerprint of a command's bytes, shared by both attribution sides.

    HA-side correlation digests the payload it is about to publish with this
    same function, so a chain and the service call that opened it join on equal
    bytes instead of on topic and timing alone. Two scripts writing different
    parameters to one device inside the correlation window are indistinguishable
    by topic, which is what made them swap names.
    """
    return hashlib.sha1(payload).hexdigest()[:12]


def payload_key_digests(payload: bytes) -> str | None:
    """Per-key value fingerprints for a command payload, as `key:digest` pairs.

    A whole-payload digest cannot tell "one commander wrote three different
    parameters" apart from "two commanders disagreed about one parameter": both
    read as different payloads. Digesting each top-level key separately makes
    that distinction answerable, and keeps the stored form bounded and free of
    the values themselves.

    Returns None for anything that is not a JSON object, which includes the
    bare-scalar `/set/<attribute>` form: there the attribute is already in the
    topic, so the chain's target carries it.
    """
    try:
        parsed = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    pairs = []
    for key in sorted(parsed)[:MAX_PAYLOAD_KEYS]:
        value = json.dumps(parsed[key], sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:KEY_DIGEST_CHARS]
        pairs.append(f"{key}:{digest}")
    return ",".join(pairs)


def parse_key_digests(stored: str | None) -> dict[str, str]:
    """Inverse of payload_key_digests, tolerant of NULL and of malformed rows."""
    if not stored:
        return {}
    out: dict[str, str] = {}
    for pair in stored.split(","):
        key, _, digest = pair.rpartition(":")
        if key:
            out[key] = digest
    return out


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
    # How far behind the event loop was running when this chain was opened.
    # `opened_at` is stamped at PROCESSING time, so this is the width of the
    # bracket around it, not a correction to it: a consumer comparing chain
    # timestamps to anything else must treat a chain with a large skew as
    # having an unknown position inside that bracket. See on_command.
    clock_skew_ms: float = 0.0
    # `key:digest` pairs for the payload's top-level keys; None when the payload
    # is not a JSON object. Lets a conflict (two writers disagreeing about ONE
    # parameter) be told apart from a normal multi-key write sequence.
    payload_keys: str | None = None

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
        loop_skew_ms: Callable[[], float] | None = None,
    ):
        self._clock = clock
        # Reports how far behind the event loop is running right now. Ingest is
        # inline on that loop, so this is the only handle we have on the gap
        # between a command reaching the broker and this code stamping it.
        self._loop_skew_ms = loop_skew_ms or (lambda: 0.0)
        self._resolve_members = resolve_members or (lambda _instance, _target: [])
        # Ingest runs on the event loop while drain_finalized is called from
        # flush and API worker threads; the mutex keeps _expire's rebuilds
        # atomic against per-message updates.
        self._mutex = threading.Lock()
        self._open: dict[tuple[str, str], deque[Chain]] = {}
        self._claims: dict[tuple[str, str], deque[Chain]] = {}
        self._finalized: list[Chain] = []
        self._recent_payloads: dict[tuple[str, str], tuple[float, str, Chain]] = {}

    # -- intake ---------------------------------------------------------------

    def on_command(
        self, instance: str, target: str, verb: str, payload: bytes, client: str | None = None
    ) -> Chain:
        now = self._clock()
        skew_ms = max(self._loop_skew_ms(), 0.0)
        digest = command_digest(payload)
        chain = Chain(
            instance=instance,
            target=target,
            verb=verb,
            opened_at=now,
            payload_size=len(payload),
            payload_digest=digest,
            client=client,
            clock_skew_ms=skew_ms,
            payload_keys=payload_key_digests(payload),
        )
        with self._mutex:
            if verb == "set":
                key = (instance, target)
                previous = self._recent_payloads.get(key)
                if previous is not None:
                    prev_ts, prev_digest, prev_chain = previous
                    gap = now - prev_ts
                    # `opened_at` is processing time and ingest is inline on the
                    # event loop, so a loop stall collapses the observed gap: two
                    # commands that really arrived seconds apart drain together and
                    # look simultaneous. Widen the gap by the skew in flight and
                    # require even that worst case to sit inside the window, so a
                    # stall can never MANUFACTURE a duplicate. With a healthy loop
                    # (sub-millisecond lag) this is the old test unchanged; the
                    # cost is under-reporting during a stall, which is the right
                    # direction for a measurement the fixes get judged against.
                    doubt = max(skew_ms, prev_chain.clock_skew_ms) / 1000.0
                    if prev_digest == digest and gap + doubt <= REDUNDANT_WINDOW:
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
        with self._mutex:
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

    def attribute_client(
        self, instance: str, target: str, client: str, digest: str | None = None
    ) -> bool:
        """Backfill the client id onto the chain this publisher actually opened.

        Most commands reach the broker before the HA event explaining them does
        (measured on this installation: the wire wins about 80% of the time, by
        a median of 1 ms), so this backfill, not `HaAttribution.name_for`, is
        where the majority of commanders get their name. Matching on target
        alone therefore carried the same collision as the lookup side: with two
        unattributed chains open on one device, the first name to arrive claimed
        the newest chain regardless of which command it explained.

        With a digest the chain is picked by the bytes it was opened with. The
        oldest matching chain wins so that repeated identical payloads pair up
        in arrival order. Callers with no payload in hand (the broker-log
        correlator) pass none and keep the newest-chain behaviour.
        """
        with self._mutex:
            chains = self._open.get((instance, target))
            if not chains:
                return False
            if digest is None:
                for chain in reversed(chains):
                    if chain.client is None:
                        chain.client = client
                        return True
                return False
            for chain in chains:
                if chain.client is None and chain.payload_digest == digest:
                    chain.client = client
                    return True
        return False

    # -- finalization ---------------------------------------------------------

    def _expire(self, now: float) -> None:
        # Callers hold self._mutex.
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
        with self._mutex:
            self._expire(self._clock())
            drained = self._finalized
            self._finalized = []
            return drained

    def open_count(self) -> int:
        with self._mutex:
            return sum(len(chains) for chains in self._open.values())
