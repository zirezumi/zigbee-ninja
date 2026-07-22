"""What a recommendation costs on the budgets it does not save on.

A detector measures how much of one resource a change would free. Acting on it
almost always spends a different one: replacing a group command with per-member
commands buys channel airtime with pipeline commands, spreading a burst buys
queue headroom with wall clock, raising a reporting interval buys airtime with
freshness, moving a device buys one coordinator's headroom with another's. If
the queue only ever reports the saving, it will happily recommend spending a
scarce resource to relieve an abundant one, which is precisely the failure this
module exists to prevent.

**Every block carries a `kind` discriminator.** The shapes are genuinely not
commensurable (commands, seconds, a peak on another instance, nothing at all),
so a single flat schema would be a union of optional fields with no way for a
consumer to know which of them are meaningful. `kind` lets the GUI and any
downstream consumer dispatch honestly instead of guessing from which keys
happen to be present. Three fields are common to every kind: `kind`,
`denominator`, and `note`, plus `raises_load` as the one-bit summary a queue
can filter on.

`note` is always authored by the detector in plain language, so a consumer that
has never seen a kind can still render something true.

An explicit `none` is not the same as an absent cost: it records that someone
worked out the trade and there was nothing on the other side of it. A missing
cost block means nobody assessed it.

This module is also the single home for the **denominator vocabulary**. These
strings are user-facing (the GUI glosses each one in plain language), so a
detector inventing its own wording silently produces an unglossed tooltip.
Add the constant here and the gloss in the Recommendations view together.
"""

from __future__ import annotations

# -- kinds ---------------------------------------------------------------------------

KIND_PUBLISH_DELTA = "publish_delta"
KIND_DESTINATION_LOAD = "destination_load"
KIND_COMPLETION_DELAY = "completion_delay"
KIND_STALENESS = "staleness"
KIND_NONE = "none"

# -- denominators (each needs a matching gloss in the Recommendations view) ----------

CHANNEL_AIRTIME = "channel airtime"
COMMAND_RATE = "command rate"
PEAK_COMMAND_RATE = "peak command rate"
DEVICE_SERVICE_RATE = "device service rate"
BURST_COMPLETION = "burst completion time"
STALENESS = "state staleness"

DENOMINATORS = (
    CHANNEL_AIRTIME,
    COMMAND_RATE,
    PEAK_COMMAND_RATE,
    DEVICE_SERVICE_RATE,
    BURST_COMPLETION,
    STALENESS,
)


def publish_delta(
    *,
    before: int,
    after: int,
    lookback_seconds: float,
    utilization: dict | None = None,
    note: str | None = None,
) -> dict:
    """Command count changes: the action sends more (or fewer) publishes.

    Shared by every detector whose action adds or removes commands, so the
    shape stays identical across them. Bursts scale worse than the mean,
    because a controller rendering a room as a unit emits its whole publish
    list at once, so a multiplier lands on the peak rather than spread across
    the day. The mean here is a floor, not a forecast, and the measured
    capacity limit travels alongside it so a reader can see what the peak is
    being judged against.
    """
    delta = after - before
    use = utilization or {}
    return {
        "kind": KIND_PUBLISH_DELTA,
        "denominator": COMMAND_RATE,
        "raises_load": delta > 0,
        "note": note
        or (
            "the mean spreads over the window; a burst multiplies at the peak, "
            "which is what the measured capacity limit binds"
        ),
        "publishes_before": before,
        "publishes_after": after,
        "publish_multiplier": round(after / before, 3) if before else None,
        "delta_commands_per_day": (
            round(delta / (lookback_seconds / 86400.0), 1) if lookback_seconds else None
        ),
        "delta_eps_mean": (
            round(delta / lookback_seconds, 4) if lookback_seconds else None
        ),
        "measured_peak_eps": use.get("max_eps"),
        "capacity_limit_eps": use.get("knee_eps"),
    }


def publish_delta_for(ctx, instance: str, *, before: int, after: int, note=None) -> dict:
    """`publish_delta` with the detector-context plumbing filled in.

    Lives here rather than in whichever detector needed it first: the rule
    that a command-count change must be declared is doctrine, not a property
    of any one detector.
    """
    return publish_delta(
        before=before,
        after=after,
        lookback_seconds=ctx.lookback_seconds,
        utilization=(ctx.utilization or {}).get(instance),
        note=note,
    )


def none(note: str) -> dict:
    """Nothing measurable on the other side of the trade.

    Distinct from omitting the block: this records that the trade was assessed
    and found free, which reads differently from nobody having checked.
    """
    return {
        "kind": KIND_NONE,
        "denominator": None,
        "raises_load": False,
        "note": note,
    }
