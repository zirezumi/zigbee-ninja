"""A publish that changes nothing is invisible to a duplicate test, so the
detector's job is to be readable about what it could NOT decide as much as
about what it could."""

import json

from zigbee_ninja.attribution.noop import (
    VERDICT_CHANGING,
    VERDICT_NOOP,
    VERDICT_UNKNOWN,
    EchoState,
    NoopDetector,
)


def _state(**kw) -> bytes:
    return json.dumps(kw).encode()


def _cmd(**kw) -> bytes:
    return json.dumps(kw).encode()


def test_command_matching_reported_state_is_a_noop():
    d = NoopDetector()
    # Cold: nothing is known yet, so the first command cannot be judged.
    first = d.classify("z2m-1", "office_dimmer", _cmd(ledIntensityWhenOn=65))
    assert first.verdict == VERDICT_UNKNOWN
    assert first.reason == "cold"

    # The device reports; the same command is now demonstrably a no-op.
    d.note_state("z2m-1", "office_dimmer", _state(ledIntensityWhenOn=65, state="ON"))
    again = d.classify("z2m-1", "office_dimmer", _cmd(ledIntensityWhenOn=65))
    assert again.verdict == VERDICT_NOOP
    assert again.matched == ["ledIntensityWhenOn"]
    assert again.basis() == "=ledIntensityWhenOn"


def test_command_asking_for_a_different_value_is_changing():
    d = NoopDetector()
    d.classify("z2m-1", "office_dimmer", _cmd(ledIntensityWhenOn=65))
    d.note_state("z2m-1", "office_dimmer", _state(ledIntensityWhenOn=65))
    v = d.classify("z2m-1", "office_dimmer", _cmd(ledIntensityWhenOn=0))
    assert v.verdict == VERDICT_CHANGING
    assert v.differed == ["ledIntensityWhenOn"]


def test_one_differing_key_beats_partial_knowledge():
    """A command that provably changes something is `changing` even when the
    rest of the payload is unknown: the claim 'this did something' is already
    established and does not need full coverage."""
    d = NoopDetector()
    d.classify("z2m-1", "dev", _cmd(brightness=100, color_temp=370))
    d.note_state("z2m-1", "dev", _state(brightness=100))  # color_temp never seen
    v = d.classify("z2m-1", "dev", _cmd(brightness=200, color_temp=370))
    assert v.verdict == VERDICT_CHANGING
    assert v.differed == ["brightness"]
    assert v.unknown == ["color_temp"]


def test_partial_knowledge_never_yields_a_noop():
    """The asymmetry that makes the metric safe to quote: all-known-keys
    matched but something unknown is `unknown`, never `noop`. Otherwise
    ignorance would inflate the very number someone is trying to drive to
    zero."""
    d = NoopDetector()
    d.classify("z2m-1", "dev", _cmd(brightness=100, color_temp=370))
    d.note_state("z2m-1", "dev", _state(brightness=100))
    v = d.classify("z2m-1", "dev", _cmd(brightness=100, color_temp=370))
    assert v.verdict == VERDICT_UNKNOWN
    assert v.matched == ["brightness"]
    assert v.unknown == ["color_temp"]


def test_modifier_keys_are_not_assessable_state():
    """`transition` says how fast, not where. A payload of only modifiers has
    nothing to compare, and a transition difference must not make an otherwise
    identical command look like it changes something."""
    d = NoopDetector()
    v = d.classify("z2m-1", "dev", _cmd(transition=3))
    assert v.verdict == VERDICT_UNKNOWN
    assert v.reason == "no_keys"

    d.classify("z2m-1", "dev", _cmd(brightness=100))
    d.note_state("z2m-1", "dev", _state(brightness=100))
    same = d.classify("z2m-1", "dev", _cmd(brightness=100, transition=30))
    assert same.verdict == VERDICT_NOOP


def test_group_command_needs_every_member_to_hold_the_value():
    """A group's own state topic is Z2M's synthetic optimistic state, not an
    aggregate, so uniformity has to be checked per member."""
    members = {"z2m-1": {"office_bulbs": ["bulb_a", "bulb_b"]}}
    d = NoopDetector(
        resolve_members=lambda inst, target: (members.get(inst, {}).get(target, []), True)
    )
    d.classify("z2m-1", "office_bulbs", _cmd(brightness=100))
    d.note_state("z2m-1", "bulb_a", _state(brightness=100))
    d.note_state("z2m-1", "bulb_b", _state(brightness=100))
    assert d.classify("z2m-1", "office_bulbs", _cmd(brightness=100)).verdict == VERDICT_NOOP

    # One member drifts: the group command is doing real work again.
    d.note_state("z2m-1", "bulb_b", _state(brightness=40))
    assert (
        d.classify("z2m-1", "office_bulbs", _cmd(brightness=100)).verdict
        == VERDICT_CHANGING
    )


def test_group_with_an_unseen_member_is_unknown_not_a_noop():
    members = {"z2m-1": {"grp": ["bulb_a", "bulb_b"]}}
    d = NoopDetector(
        resolve_members=lambda inst, target: (members.get(inst, {}).get(target, []), True)
    )
    d.classify("z2m-1", "grp", _cmd(brightness=100))
    d.note_state("z2m-1", "bulb_a", _state(brightness=100))  # bulb_b never reports
    v = d.classify("z2m-1", "grp", _cmd(brightness=100))
    assert v.verdict == VERDICT_UNKNOWN
    assert v.unknown == ["brightness"]


def test_unresolved_group_roster_is_unknown_not_a_noop():
    """The false-`noop` case, and the reason `complete` exists.

    A roster that silently loses the member which would have disagreed leaves
    every remaining member matching, so the command reads as a no-op when it is
    doing real work. Stamping `unknown` keeps it in the coverage figure instead
    of in the numerator, which is the direction this detector is required to
    fail in."""
    d = NoopDetector(resolve_members=lambda inst, target: (["bulb_a"], False))
    d.classify("z2m-1", "grp", _cmd(brightness=100))
    d.note_state("z2m-1", "bulb_a", _state(brightness=100))
    v = d.classify("z2m-1", "grp", _cmd(brightness=100))
    assert v.verdict == VERDICT_UNKNOWN
    assert v.reason == "group_unresolved"
    assert v.basis() == "~group_unresolved"


def test_group_resolving_to_no_members_is_unknown_not_self_assessed():
    """An empty roster must not fall back to the group topic: that is Z2M's
    synthetic one-member state, which cannot answer the question."""
    d = NoopDetector(resolve_members=lambda inst, target: ([], False))
    d.note_state("z2m-1", "grp", _state(brightness=100))
    v = d.classify("z2m-1", "grp", _cmd(brightness=100))
    assert v.verdict == VERDICT_UNKNOWN
    assert v.reason == "group_unresolved"


def test_plain_device_target_still_assesses_against_itself():
    """Regression: a non-group resolves to ([], True) and must keep working."""
    d = NoopDetector(resolve_members=lambda inst, target: ([], True))
    d.classify("z2m-1", "dev", _cmd(brightness=100))
    d.note_state("z2m-1", "dev", _state(brightness=100))
    assert d.classify("z2m-1", "dev", _cmd(brightness=100)).verdict == VERDICT_NOOP


def test_int_float_round_trip_does_not_look_like_a_change():
    """Z2M round-trips numbers through JSON, so an int commanded can come back
    a float. Strings stay strings: a mode key must never compare equal to a
    number."""
    d = NoopDetector()
    d.classify("z2m-1", "dev", _cmd(brightness=254))
    d.note_state("z2m-1", "dev", b'{"brightness": 254.0}')
    assert d.classify("z2m-1", "dev", _cmd(brightness=254)).verdict == VERDICT_NOOP

    d.classify("z2m-1", "dev2", _cmd(loadLevelIndicatorTimeout="3 Seconds"))
    d.note_state("z2m-1", "dev2", _state(loadLevelIndicatorTimeout="3 Seconds"))
    v = d.classify("z2m-1", "dev2", _cmd(loadLevelIndicatorTimeout="Stay Off"))
    assert v.verdict == VERDICT_CHANGING


def test_near_misses_are_counted_but_never_decide_a_verdict():
    """Clamping and quantization are real, but no tolerance here has been
    measured. Exact comparison decides; `near` records how often a tolerance
    would have mattered, so the table's contents get measured rather than
    guessed."""
    d = NoopDetector()
    d.classify("z2m-1", "dev", _cmd(brightness=254))
    d.note_state("z2m-1", "dev", _state(brightness=253))
    v = d.classify("z2m-1", "dev", _cmd(brightness=254))
    assert v.verdict == VERDICT_CHANGING  # not silently forgiven
    assert v.near == ["brightness"]


def test_bare_scalar_set_form_is_reported_as_its_own_reason():
    d = NoopDetector()
    v = d.classify("z2m-1", "dev", b"ON")
    assert v.verdict == VERDICT_UNKNOWN
    assert v.reason == "not_object"


def test_state_intake_is_gated_before_it_parses():
    """State publishes outnumber commands ~14:1 and most are from devices
    nothing commands. An untracked device must cost a dict lookup, and a
    tracked device whose payload lacks any tracked key must cost a substring
    scan: neither should reach json.loads."""
    echoes = EchoState()
    big = _state(**{f"k{i}": i for i in range(139)})

    assert echoes.note_state("z2m-1", "sensor", big) is False
    assert echoes.stats()["untracked_skips"] == 1
    assert echoes.stats()["parses"] == 0

    echoes.track("z2m-1", "dev", ["brightness"])
    assert echoes.note_state("z2m-1", "dev", big) is False
    assert echoes.stats()["prefilter_skips"] == 1
    assert echoes.stats()["parses"] == 0

    assert echoes.note_state("z2m-1", "dev", _state(brightness=5)) is True
    assert echoes.stats()["parses"] == 1
    assert echoes.get("z2m-1", "dev", "brightness") == (True, 5.0)


def test_absent_key_and_null_key_are_different_answers():
    echoes = EchoState()
    echoes.track("z2m-1", "dev", ["color_temp"])
    echoes.note_state("z2m-1", "dev", _state(color_temp=None))
    assert echoes.get("z2m-1", "dev", "color_temp") == (True, None)
    assert echoes.get("z2m-1", "dev", "never_seen") == (False, None)


def test_stats_report_coverage_so_a_small_count_cannot_read_as_an_absence():
    d = NoopDetector()
    d.classify("z2m-1", "dev", _cmd(brightness=1))  # unknown: cold
    d.note_state("z2m-1", "dev", _state(brightness=1))
    d.classify("z2m-1", "dev", _cmd(brightness=1))  # noop
    d.classify("z2m-1", "dev", _cmd(brightness=2))  # changing
    stats = d.stats()
    assert stats["counts"] == {VERDICT_NOOP: 1, VERDICT_CHANGING: 1, VERDICT_UNKNOWN: 1}
    assert stats["resolution_coverage"] == round(2 / 3, 4)


def test_tracking_a_command_does_not_let_it_judge_itself():
    """Interest is registered AFTER assessment. If a command's own keys were
    tracked first and its own echo arrived before the next command, nothing
    would break; but registering first and reading the state it caused would
    make a genuine change look like a no-op on the retry."""
    d = NoopDetector()
    v = d.classify("z2m-1", "dev", _cmd(brightness=100))
    assert v.verdict == VERDICT_UNKNOWN
    assert d.echoes.stats()["tracked_devices"] == 1


def test_ct_value_match_in_xy_mode_is_changing_not_a_noop():
    # The false-positive class measured live 2026-08-22: an unchanged mired
    # published to a bulb sitting in xy mode flips the bulb's colour MODE,
    # which the value comparison alone cannot see.
    d = NoopDetector()
    d.classify("z2m-1", "bulb", _cmd(color_temp=368))
    d.note_state("z2m-1", "bulb", _state(color_temp=368, color_mode="xy"))
    v = d.classify("z2m-1", "bulb", _cmd(color_temp=368))
    assert v.verdict == VERDICT_CHANGING
    assert v.differed == ["color_temp"]


def test_ct_value_match_in_ct_mode_stays_a_noop():
    d = NoopDetector()
    d.classify("z2m-1", "bulb", _cmd(color_temp=368))
    d.note_state("z2m-1", "bulb", _state(color_temp=368, color_mode="color_temp"))
    v = d.classify("z2m-1", "bulb", _cmd(color_temp=368))
    assert v.verdict == VERDICT_NOOP
    assert v.basis() == "=color_temp"


def test_ct_value_match_with_unknown_mode_is_unknown_not_a_noop():
    # The device has reported the value but never its colour mode: partial
    # knowledge, and partial knowledge can never produce a no-op.
    d = NoopDetector()
    d.classify("z2m-1", "bulb", _cmd(color_temp=368))
    d.note_state("z2m-1", "bulb", _state(color_temp=368))
    v = d.classify("z2m-1", "bulb", _cmd(color_temp=368))
    assert v.verdict == VERDICT_UNKNOWN
    assert v.unknown == ["color_temp"]


def test_xy_value_match_in_ct_mode_is_changing():
    d = NoopDetector()
    d.classify("z2m-1", "bulb", _cmd(color={"x": 0.47, "y": 0.408}))
    d.note_state(
        "z2m-1", "bulb",
        _state(color={"x": 0.47, "y": 0.408}, color_mode="color_temp"),
    )
    v = d.classify("z2m-1", "bulb", _cmd(color={"x": 0.47, "y": 0.408}))
    assert v.verdict == VERDICT_CHANGING
    assert v.differed == ["color"]


def test_hs_colour_command_requires_hs_mode():
    d = NoopDetector()
    d.classify("z2m-1", "bulb", _cmd(color={"hue": 10, "saturation": 50}))
    d.note_state(
        "z2m-1", "bulb",
        _state(color={"hue": 10, "saturation": 50}, color_mode="xy"),
    )
    v = d.classify("z2m-1", "bulb", _cmd(color={"hue": 10, "saturation": 50}))
    assert v.verdict == VERDICT_CHANGING


def test_non_colour_keys_do_not_grow_a_mode_requirement():
    # brightness has no implied colour space; a bulb whose mode is unknown
    # must still yield a clean no-op on a matched brightness.
    d = NoopDetector()
    d.classify("z2m-1", "bulb", _cmd(brightness=128))
    d.note_state("z2m-1", "bulb", _state(brightness=128))
    v = d.classify("z2m-1", "bulb", _cmd(brightness=128))
    assert v.verdict == VERDICT_NOOP


def test_group_ct_match_with_one_member_in_xy_mode_is_changing():
    d = NoopDetector(resolve_members=lambda inst, target: (["a", "b"], True))
    d.classify("z2m-1", "grp", _cmd(color_temp=368))
    d.note_state("z2m-1", "a", _state(color_temp=368, color_mode="color_temp"))
    d.note_state("z2m-1", "b", _state(color_temp=368, color_mode="xy"))
    v = d.classify("z2m-1", "grp", _cmd(color_temp=368))
    assert v.verdict == VERDICT_CHANGING
    # One wrong-mode member decides it even though the other is fine.
    assert v.differed == ["color_temp"]
