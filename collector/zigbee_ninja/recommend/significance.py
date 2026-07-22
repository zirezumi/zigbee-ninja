"""Whether a finding is worth acting on, given what is actually scarce.

A detector measures how much of a resource a change would free. That is a
different question from whether freeing it helps anyone: half a percent of a
channel budget running at one percent utilization relieves nothing at all.
§V2-5 originally ordered the queue by `modeled saving × confidence`, which
ranks a large saving on an idle mesh above a small one on a saturated mesh.
This module supplies the missing term.

It does not hide anything. Per the detector-honesty doctrine the owner still
sees every finding; significance ranks and labels them, and the GUI collapses
the low band by default rather than dropping it. The intent is that a card
reads as a plain sentence:

    "Frees 0.47% of this coordinator's channel airtime budget, which is
     currently 0.9% used."

after which a reader with only a cursory grasp of network engineering can see
for themselves that there is nothing to win, without having to weigh a score.

**Two rules, both deliberately explainable:**

1. A denominator below `PRESSURE_FLOOR_PCT` is not contended, so anything it
   frees lands in the `low` band no matter how large the saving is. This rule
   dominates, and it has to: a finding can remove nearly all of what a mesh
   spends while that spend is still a rounding error. "You would remove most
   of what is being spent, but almost nothing is being spent" is the honest
   reading, and band `low` with a `relief_pct` near 100 says exactly that.
2. Above the floor, a finding that removes at least `STRONG_RELIEF` of current
   spend on that denominator is `high`, otherwise `moderate`.

Unmeasured denominators report band `unknown`: the engine says it does not
know rather than assuming an idle mesh (which would silently demote real
findings on installations that have never run a calibration).
"""

from __future__ import annotations

from . import cost

# Below this, a denominator has nothing worth relieving.
PRESSURE_FLOOR_PCT = 25.0
# Above the floor, removing this share of current spend is a strong result.
STRONG_RELIEF = 0.10

BAND_HIGH = "high"
BAND_MODERATE = "moderate"
BAND_LOW = "low"
BAND_UNKNOWN = "unknown"

# Ordering for the queue: higher sorts first.
BAND_RANK = {BAND_HIGH: 3, BAND_MODERATE: 2, BAND_UNKNOWN: 1, BAND_LOW: 0}

# Re-exported so a detector can name a denominator from whichever of the two
# modules it already imports; `cost` is the single definition (its docstring
# explains why the vocabulary has one home).
CHANNEL_AIRTIME = cost.CHANNEL_AIRTIME
COMMAND_RATE = cost.COMMAND_RATE


def assess(
    *,
    saving_pct: float | None,
    utilization_pct: float | None,
    denominator: str = CHANNEL_AIRTIME,
) -> dict:
    """Band a saving against how contended its denominator actually is.

    `saving_pct` and `utilization_pct` are both percentages of the same
    denominator, so their ratio is the share of current spend the change
    would remove.
    """
    if saving_pct is None or utilization_pct is None:
        return {
            "band": BAND_UNKNOWN,
            "denominator": denominator,
            "utilization_pct": utilization_pct,
            "relief_pct": None,
            "rationale": (
                f"frees about {saving_pct:.2f}% of the {denominator} budget; "
                f"current utilization is not measured yet"
                if saving_pct is not None
                else f"{denominator} utilization is not measured yet"
            ),
        }

    relief = (saving_pct / utilization_pct) if utilization_pct > 0 else None
    if utilization_pct < PRESSURE_FLOOR_PCT:
        band = BAND_LOW
    elif relief is not None and relief >= STRONG_RELIEF:
        band = BAND_HIGH
    else:
        band = BAND_MODERATE

    rationale = (
        f"frees about {saving_pct:.2f}% of the {denominator} budget, "
        f"which is currently {utilization_pct:.1f}% used"
    )
    if band == BAND_LOW:
        rationale += "; that budget is not under pressure, so this relieves nothing today"

    return {
        "band": band,
        "denominator": denominator,
        "utilization_pct": round(utilization_pct, 3),
        "relief_pct": None if relief is None else round(relief * 100.0, 1),
        "rationale": rationale,
    }


def for_airtime(saving: dict, utilization: dict | None) -> dict:
    """Significance of an airtime saving against the channel budget."""
    return assess(
        saving_pct=saving.get("pct_of_budget"),
        utilization_pct=(utilization or {}).get("channel_budget_pct"),
        denominator=CHANNEL_AIRTIME,
    )


# Queue ordering deliberately lives in ONE place, `store._rank`, which reads
# BAND_RANK above. An earlier draft of this module carried its own `rank_key`
# alongside it; two implementations of queue order are two things to keep in
# agreement, and the store's is the one the API actually calls.
