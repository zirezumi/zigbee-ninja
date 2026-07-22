"""Every user-facing denominator must have a plain-language gloss in the GUI.

The Recommendations view keys its gloss map on the denominator string itself,
so a detector renaming one (or minting a new one) yields a silently empty
tooltip rather than an error. That is not hypothetical: the reporting advisor
emitted "state staleness" while the view glossed "state freshness", and the
tooltip was blank until the two were read side by side.

Keeping the vocabulary in `recommend/cost.py` gave it one definition; this
test is what makes a drift from it fail rather than degrade quietly. It is a
deliberately cheap cross-language check (the view is TypeScript), which is why
it asserts only that each string appears as a key.
"""

from __future__ import annotations

import pathlib

import pytest

from zigbee_ninja.recommend import cost

VIEW = (
    pathlib.Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "views"
    / "Recommendations.tsx"
)


def test_every_denominator_has_a_gloss():
    if not VIEW.exists():  # collector tests may run without the frontend tree
        pytest.skip("frontend source not present")
    source = VIEW.read_text(encoding="utf-8")
    missing = [name for name in cost.DENOMINATORS if f'"{name}":' not in source]
    assert not missing, (
        "denominators with no gloss in the Recommendations view: "
        f"{missing}. Add the gloss next to DENOMINATOR_GLOSS; a missing key "
        "renders an empty tooltip instead of failing."
    )


def test_denominator_constants_are_all_registered():
    """A constant that never reaches DENOMINATORS escapes the gloss check."""
    declared = {
        value
        for name, value in vars(cost).items()
        if name.isupper()
        and isinstance(value, str)
        and not name.startswith("KIND_")
    }
    assert declared == set(cost.DENOMINATORS)
