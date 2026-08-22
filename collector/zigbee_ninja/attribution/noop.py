"""Tells a publish that changes something apart from one that does not.

The redundancy detector next door answers "did these bytes go to this target
twice inside five seconds". That catches a command duplicated in flight and
misses the larger class: a command that is perfectly unique on the wire and
still asks the device for the value it already holds. Those are invisible to a
duplicate test at any window, because there is nothing to duplicate.

Answering it needs the device's own reported state, which the chain tracker
never kept: `ChainTracker.on_state` is handed the topic suffix and never the
payload, so no echoed value is stored anywhere.

**The question is asked at command time against PRIOR state, and that is what
makes it cheap.** A "which write won" analysis has to wait for the echo that
settles a command, which on this fleet is a losing game: 42.9% of post-command
state publishes arrive after the 1.5s echo window and 30.1% after 3.5s, so the
settled value routinely outlives the chain that would hold it. Comparing a
command against the state *already* known needs no settle window, no quiet
period, and no third-writer invalidation. The echo table feeding it is
maintained independently of chain lifetime for the same reason.

Three verdicts, and the third is not a rounding error:

  * `changing`  - at least one assessable key differs from known state.
  * `noop`      - every assessable key matched, and every one was known.
  * `unknown`   - not enough was known to say. Cold start, a device that has
                  not reported since the collector came up, a readback outage,
                  a group with an unseen member.

**`unknown` must stay loud.** A consumer trying to establish an absence will
read a small `noop` count as "no no-ops" when it may be "no data", which is
the failure this repo has already made once on a different metric. Hence
`resolution_coverage` in every summary, and hence the deliberate asymmetry
below: a partially-known payload can be called `changing` (one differing key
proves it changes something) but can never be called `noop`. That biases the
no-op count DOWN, so anyone claiming zero must clear the coverage bar first
rather than being flattered by ignorance.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

# Keys that say how to get somewhere rather than where to go. A payload of
# nothing but these has no assessable content: same set the conflict detector
# excludes, imported rather than redefined so the two cannot drift apart.
from .queries import MODIFIER_KEYS

VERDICT_NOOP = "noop"
VERDICT_CHANGING = "changing"
VERDICT_UNKNOWN = "unknown"

# Per-key absolute tolerance for numeric comparison. Deliberately EMPTY: device
# clamping and quantization are real (a dimmer floors brightness at its
# minimumLevel, xy round-trips through the gamut), but this repo's rule is to
# decode before concluding, and no tolerance here has been measured yet. Until
# one is, exact comparison plus the `near` counter below reports how often a
# tolerance would have changed the answer, which is the measurement that should
# decide the table's contents. Adding a guessed value would manufacture the
# result it is meant to test.
NUMERIC_TOLERANCE: dict[str, float] = {}
NEAR_RELATIVE = 0.02  # only for counting `near`, never for deciding a verdict

# The colour keys name a destination colour SPACE as well as a value. A bulb
# holding color_temp 368 in xy mode is NOT where `{"color_temp": 368}` sends
# it: the command flips the bulb's colour mode, a real and visible change the
# value comparison cannot see. Measured on a live fleet (2026-08-22): twice a
# day, palette anchor crossings re-published an unchanged mired to bulbs
# sitting in xy mode and every one was misfiled as `=color_temp` -- a
# recurring false-positive no-op class manufactured by value-only assessment.
# A colour key's value-match therefore stands only when the reported
# `color_mode` already matches the mode the command implies; a wrong mode
# makes the key DIFFER, and an unknown mode leaves it UNKNOWN (the doctrine
# above: partial knowledge can never produce a no-op).
COLOR_MODE_KEY = "color_mode"


def _implied_modes(name: str, parsed: dict) -> tuple[str, ...]:
    """Colour modes under which this key's value-match can stand, or () when
    the key implies no mode (every non-colour key, and a `color` payload whose
    shape is not recognised -- unrecognised shapes keep value-only assessment
    rather than inventing a mode requirement)."""
    if name == "color_temp":
        return ("color_temp",)
    if name == "color":
        value = parsed.get("color")
        if isinstance(value, dict):
            if "x" in value or "y" in value:
                return ("xy",)
            if "hue" in value or "saturation" in value:
                return ("hs",)
    return ()

MAX_TRACKED_DEVICES = 2048
MAX_TRACKED_KEYS = 48


def _normalize(value):
    """Compare 254 and 254.0 as equal without making 254 and '254' equal.

    Z2M round-trips numbers through JSON, so an int commanded can come back as
    a float. A string is left alone: `loadLevelIndicatorTimeout` is genuinely
    string-valued ("Stay Off" / "3 Seconds") and coercing it would silently
    equate a mode with a number.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _near(commanded, reported) -> bool:
    """Would a plausible tolerance have called these equal? Counted, not acted on."""
    if not isinstance(commanded, float) or not isinstance(reported, float):
        return False
    if commanded == reported:
        return False
    scale = max(abs(commanded), abs(reported), 1.0)
    return abs(commanded - reported) / scale <= NEAR_RELATIVE


@dataclass
class Verdict:
    verdict: str
    matched: list[str] = field(default_factory=list)
    differed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    near: list[str] = field(default_factory=list)
    # Why nothing could be assessed, when that is the case: 'no_keys' for a
    # payload of modifiers only, 'not_object' for the bare-scalar
    # `<target>/set/<attribute>` form, 'cold' for keys never reported.
    reason: str | None = None

    def basis(self) -> str | None:
        """Compact, value-free record of how the verdict was reached.

        Values are deliberately absent: the row is a verdict, not a copy of
        the payload, and the repo's existing chain columns keep the same
        discipline."""
        parts = []
        for label, keys in (
            ("=", self.matched),
            ("!", self.differed),
            ("?", self.unknown),
            # `+` marks a key that differed but only just, by NEAR_RELATIVE. A
            # near key is always also a differed key, so it appears twice on
            # purpose: `!brightness,+brightness` reads as "this is what made the
            # verdict `changing`, and a tolerance would have flipped it".
            # Recorded here rather than only in memory so the tolerance table
            # can be sized over a query window instead of over collector
            # uptime -- an in-process counter resets on every deploy, which is
            # exactly when someone is looking.
            ("+", self.near),
        ):
            parts.extend(f"{label}{key}" for key in keys)
        if self.reason:
            parts.append(f"~{self.reason}")
        return ",".join(parts) or None


class EchoState:
    """Last reported value per (instance, device, key).

    Fed from state publishes, which outnumber commands about 14:1 on this
    fleet, so the intake is gated twice before it parses anything:

      1. the device must be *tracked*, i.e. something has actually commanded a
         key on it. Sensors that only ever report are never parsed.
      2. a tracked key name must appear literally in the payload bytes.

    Measured on a real 139-key, 5.1 KB dimmer dump: `json.loads` is 15.1 us and
    the byte prefilter is 0.4 us on a miss. At the observed 7.11 state
    publishes/second, parsing everything unconditionally would cost 0.011% of
    the event loop, so the gate is not what makes this affordable: it was
    affordable already. The gate is here because the loop has a stall history
    and 35x cheaper for one dict lookup is worth taking, not because the parse
    was ever the problem. (An earlier design note asserted the parse would
    "load the exact event loop whose GC stalls are the known open defect";
    that was never measured and is wrong by orders of magnitude.)
    """

    def __init__(self):
        # Ingest runs on the event loop; summaries are read from API threads.
        self._lock = threading.Lock()
        self._values: dict[tuple[str, str], dict[str, object]] = {}
        self._tracked: dict[tuple[str, str], set[str]] = {}
        self._tracked_bytes: dict[tuple[str, str], tuple[bytes, ...]] = {}
        self.parses = 0
        self.prefilter_skips = 0
        self.untracked_skips = 0
        self.device_cap_hits = 0
        self.key_cap_hits = 0

    def track(self, instance: str, device: str, keys) -> None:
        """Register interest in keys seen commanded on a device."""
        key = (instance, device)
        with self._lock:
            if key not in self._tracked and len(self._tracked) >= MAX_TRACKED_DEVICES:
                self.device_cap_hits += 1
                return
            tracked = self._tracked.setdefault(key, set())
            for name in keys:
                if name in MODIFIER_KEYS:
                    continue
                if name in tracked:
                    continue
                if len(tracked) >= MAX_TRACKED_KEYS:
                    self.key_cap_hits += 1
                    continue
                tracked.add(name)
            # A colour key's assessment needs the device's colour mode too
            # (see _implied_modes), and `color_mode` is never itself
            # commanded, so interest in it rides along with the first colour
            # key rather than waiting for a command that will never come.
            if tracked & {"color_temp", "color"} and COLOR_MODE_KEY not in tracked:
                if len(tracked) < MAX_TRACKED_KEYS:
                    tracked.add(COLOR_MODE_KEY)
                else:
                    self.key_cap_hits += 1
            self._tracked_bytes[key] = tuple(
                name.encode("utf-8") for name in tracked
            )

    def note_state(self, instance: str, device: str, payload: bytes) -> bool:
        """Record reported values for tracked keys. Returns whether it parsed."""
        key = (instance, device)
        with self._lock:
            wanted = self._tracked_bytes.get(key)
            if not wanted:
                self.untracked_skips += 1
                return False
            if not any(name in payload for name in wanted):
                self.prefilter_skips += 1
                return False
            tracked = set(self._tracked.get(key, ()))
        try:
            parsed = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(parsed, dict):
            return False
        with self._lock:
            self.parses += 1
            values = self._values.setdefault(key, {})
            for name in tracked:
                if name in parsed:
                    values[name] = _normalize(parsed[name])
        return True

    def get(self, instance: str, device: str, name: str):
        """(known, value). `known` distinguishes an absent key from a null one."""
        with self._lock:
            values = self._values.get((instance, device))
            if values is None or name not in values:
                return False, None
            return True, values[name]

    def stats(self) -> dict:
        with self._lock:
            return {
                "tracked_devices": len(self._tracked),
                "devices_with_state": len(self._values),
                "parses": self.parses,
                "prefilter_skips": self.prefilter_skips,
                "untracked_skips": self.untracked_skips,
                "device_cap_hits": self.device_cap_hits,
                "key_cap_hits": self.key_cap_hits,
            }


class NoopDetector:
    """Classifies each command against the state known before it was sent."""

    def __init__(self, resolve_members=None):
        self.echoes = EchoState()
        # (members, complete). `complete` is False only when the target IS a
        # group whose roster could not be fully resolved; a plain device target
        # resolves to ([], True) and is assessed against itself.
        self._resolve_members = resolve_members or (lambda _instance, _target: ([], True))
        self.counts: dict[str, int] = {
            VERDICT_NOOP: 0,
            VERDICT_CHANGING: 0,
            VERDICT_UNKNOWN: 0,
        }

    def note_state(self, instance: str, device: str, payload: bytes) -> bool:
        return self.echoes.note_state(instance, device, payload)

    def classify(self, instance: str, target: str, payload: bytes) -> Verdict:
        try:
            parsed = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return self._record(Verdict(VERDICT_UNKNOWN, reason="not_object"))
        if not isinstance(parsed, dict):
            # The bare-scalar `<target>/set/<attribute>` form. Assessable in
            # principle (the attribute is in the topic) but it is a different
            # shape and is reported as its own reason rather than folded into
            # the general unknown pile, so its share stays visible.
            return self._record(Verdict(VERDICT_UNKNOWN, reason="not_object"))

        assessable = [key for key in parsed if key not in MODIFIER_KEYS]
        if not assessable:
            return self._record(Verdict(VERDICT_UNKNOWN, reason="no_keys"))

        # A group command is a no-op only if EVERY member already holds the
        # value: the group's own state topic is Z2M's synthetic optimistic
        # state, not an aggregate, so it cannot answer this. One unseen member
        # makes the whole command unknown rather than quietly assuming the
        # group is uniform.
        members, complete = self._resolve_members(instance, target)
        if not complete:
            # The roster itself is untrustworthy, which is worse than an unseen
            # member: falling back to `[target]` would compare against the
            # group's synthetic one-member state, and a short roster can
            # manufacture a `noop` outright by dropping the member that would
            # have disagreed. Stamp unknown so it lands in the coverage figure
            # instead of in the numerator.
            return self._record(Verdict(VERDICT_UNKNOWN, reason="group_unresolved"))
        members = members or [target]

        # Register interest AFTER reading, so this command's own keys start
        # being tracked for the next one without this command comparing
        # against state it just caused.
        verdict = self._assess(instance, members, parsed, assessable)
        for device in members:
            self.echoes.track(instance, device, assessable)
        return self._record(verdict)

    def _assess(self, instance, members, parsed, assessable) -> Verdict:
        matched: list[str] = []
        differed: list[str] = []
        unknown: list[str] = []
        near: list[str] = []
        for name in sorted(assessable):
            commanded = _normalize(parsed[name])
            states = [self.echoes.get(instance, device, name) for device in members]
            if any(not known for known, _ in states):
                unknown.append(name)
                continue
            reported = [value for _, value in states]
            tolerance = NUMERIC_TOLERANCE.get(name)
            if all(_equal(commanded, value, tolerance) for value in reported):
                implied = _implied_modes(name, parsed)
                if implied:
                    modes = [
                        self.echoes.get(instance, device, COLOR_MODE_KEY)
                        for device in members
                    ]
                    # A member in the wrong mode proves the command changes
                    # something, and that holds whether or not another
                    # member's mode is known -- same asymmetry as the
                    # verdict itself.
                    if any(known and value not in implied for known, value in modes):
                        differed.append(name)
                        continue
                    if any(not known for known, _ in modes):
                        unknown.append(name)
                        continue
                matched.append(name)
            else:
                differed.append(name)
                if any(_near(commanded, value) for value in reported):
                    near.append(name)
        if differed:
            # One differing key proves the command changes something, and that
            # holds whether or not the rest is known.
            return Verdict(VERDICT_CHANGING, matched, differed, unknown, near)
        if unknown:
            # Never claim a no-op on partial knowledge: the whole point of the
            # metric is that it survives someone trying to prove an absence.
            return Verdict(
                VERDICT_UNKNOWN, matched, differed, unknown, near, reason="cold"
            )
        return Verdict(VERDICT_NOOP, matched, differed, unknown, near)

    def _record(self, verdict: Verdict) -> Verdict:
        self.counts[verdict.verdict] = self.counts.get(verdict.verdict, 0) + 1
        return verdict

    def stats(self) -> dict:
        total = sum(self.counts.values())
        resolved = self.counts[VERDICT_NOOP] + self.counts[VERDICT_CHANGING]
        return {
            "counts": dict(self.counts),
            # Resolved over seen. A no-op count without this is unreadable:
            # a small numerator means "no no-ops" or "no data" and the two
            # want opposite responses.
            "resolution_coverage": round(resolved / total, 4) if total else None,
            "echoes": self.echoes.stats(),
        }


def _equal(commanded, reported, tolerance: float | None) -> bool:
    if tolerance is not None and isinstance(commanded, float) and isinstance(
        reported, float
    ):
        return abs(commanded - reported) <= tolerance
    return commanded == reported
