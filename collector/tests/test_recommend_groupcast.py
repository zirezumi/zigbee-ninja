from zigbee_ninja.recommend import groupcast
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.store.db import Database

NOW = 3_000_000.0
LOOKBACK = 3600.0


def _context(
    tmp_path,
    groups=None,
    routers=20,
    pricing=(None, None),
    devices=None,
    topology=None,
    utilization=None,
):
    """groups: {group_name: [member names]} for one instance 'z2m-1'.

    devices: registry rows, used for the binding-awareness guard.
    topology: a {"raw": <networkmap>} entry, used for hop pricing.
    utilization: {instance: {...}} pressure, used for significance banding.
    """
    groups = groups or {}
    db = Database(tmp_path)
    return DetectorContext(
        conn=db.connect(),
        now=NOW,
        lookback_seconds=LOOKBACK,
        instances=["z2m-1"],
        instance_info={"base_topic": {}},
        knees={},
        is_group=lambda base, target: target in groups,
        group_members=lambda base, target: list(groups.get(target, [])),
        groups=lambda base: [{"friendly_name": name} for name in groups],
        devices=lambda base: list(devices or []),
        router_count_for=lambda base: routers,
        pricing=lambda base: pricing,
        topology_latest=(lambda base: topology) if topology is not None else None,
        utilization=utilization or {},
    )


def _bound(name, count=1):
    """A registry row for a device carrying outbound bindings."""
    return {"friendly_name": name, "binding_count": count}


def _insert(ctx, commands):
    """commands: iterable of (opened_at, target, client, digest)."""
    ctx.conn.executemany(
        "INSERT INTO chains (instance, target, verb, opened_at, client, payload_size, "
        "echo_count, first_echo_ms, redundant, payload_digest) "
        "VALUES ('z2m-1', ?, 'set', ?, ?, 10, 1, 100, 0, ?)",
        [(target, opened_at, client, digest) for opened_at, target, client, digest in commands],
    )
    ctx.conn.commit()


def _fanout(start, targets, digest, client="automation: Hall"):
    return [(start + i * 0.1, target, client, digest) for i, target in enumerate(targets)]


TARGETS = ["hall_1", "hall_2", "hall_3", "hall_4", "hall_5", "hall_6"]


def test_fanout_retargets_to_exact_existing_group(tmp_path):
    # 3 routers: one amplified groupcast beats six unicasts.
    ctx = _context(tmp_path, groups={"hall_lights": TARGETS}, routers=3)
    for occurrence in range(12):
        _insert(ctx, _fanout(NOW - 1800 + occurrence * 60, TARGETS, "aa11"))

    findings = groupcast.detect(ctx)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.action["kind"] == "retarget"
    assert finding.action["group"] == "hall_lights"
    assert finding.confidence == "high"
    assert finding.saving["us_per_s"] > 0
    assert "replayed" in finding.saving["basis"]
    assert finding.saving["provenance"] == "modeled"


def test_fanout_without_matching_group_suggests_regroup(tmp_path):
    ctx = _context(tmp_path, groups={"other": ["x", "y"]}, routers=3)
    for occurrence in range(12):
        _insert(ctx, _fanout(NOW - 1800 + occurrence * 60, TARGETS, "aa11"))

    (finding,) = groupcast.detect(ctx)
    assert finding.action["kind"] == "regroup"
    assert finding.confidence == "medium"
    assert set(finding.action["members"]) == set(TARGETS)


def test_fanout_stays_quiet_when_unicasts_are_cheaper(tmp_path):
    # 25 routers: the amplified groupcast costs more than six unicasts.
    ctx = _context(tmp_path, groups={"hall_lights": TARGETS}, routers=25)
    for occurrence in range(4):
        _insert(ctx, _fanout(NOW - 1800 + occurrence * 60, TARGETS, "aa11"))
    assert groupcast.detect(ctx) == []


def test_fanout_beside_identical_group_command_is_double_delivery(tmp_path):
    # Even on a router-heavy mesh, unicasts duplicating a group command are
    # pure overhead regardless of which shape is cheaper.
    ctx = _context(tmp_path, groups={"hall_lights": TARGETS}, routers=25)
    for occurrence in range(12):
        start = NOW - 1800 + occurrence * 60
        _insert(ctx, [(start, "hall_lights", "automation: Hall", "aa11")])
        _insert(ctx, _fanout(start + 0.05, TARGETS, "aa11"))

    # The group itself also reads as an amplification loser on 25 routers;
    # the fan-out must surface as double delivery regardless.
    (finding,) = [f for f in groupcast.detect(ctx) if f.action["kind"] == "dedupe"]
    assert finding.action["drop"] == "per-device commands"
    assert "double delivery" in finding.finding
    assert finding.confidence == "high"


def test_varied_payloads_never_read_as_a_fanout(tmp_path):
    # Per-device renders carry different payloads; no group command collapses
    # them, so the detector must not propose one.
    ctx = _context(tmp_path, groups={"hall_lights": TARGETS}, routers=3)
    for occurrence in range(4):
        start = NOW - 1800 + occurrence * 60
        _insert(
            ctx,
            [
                (start + i * 0.1, target, "automation: Hall", f"digest{i}")
                for i, target in enumerate(TARGETS)
            ],
        )
    assert groupcast.detect(ctx) == []


def test_small_group_on_router_heavy_mesh_loses_to_unicast(tmp_path):
    ctx = _context(tmp_path, groups={"bed_pair": ["bed_l", "bed_r"]}, routers=29)
    _insert(
        ctx,
        [
            (NOW - 1800 + i * 30, "bed_pair", "automation: Bedroom", f"d{i}")
            for i in range(10)
        ],
    )

    (finding,) = groupcast.detect(ctx)
    assert finding.action["kind"] == "retarget"
    assert finding.action["to"] == "per-member commands"
    assert "no longer change in the same instant" in finding.finding
    assert finding.confidence == "medium"
    assert finding.fingerprint["members"] == 2


def test_single_member_group_does_not_claim_the_group_is_pointless(tmp_path):
    # One member: singular phrasing, and no simultaneity claim (there is
    # nothing to change "in the same instant"). The amplification is real, but
    # a one-member group is a normal way to address a device without reaching
    # what its bindings reach, so the finding must not assert the group buys
    # nothing: it says to check why the group exists first.
    ctx = _context(tmp_path, groups={"tos_phantom": ["tos_light"]}, routers=29)
    _insert(
        ctx,
        [
            (NOW - 1800 + i * 30, "tos_phantom", "automation: TOS", f"d{i}")
            for i in range(10)
        ],
    )

    (finding,) = groupcast.detect(ctx)
    assert "has 1 member," in finding.finding
    assert "1 individual command would cost" in finding.finding
    assert "same instant" not in finding.finding
    assert "gains nothing" not in finding.finding
    assert "before dissolving it" in finding.finding
    # Nothing in the registry says this member is bound, so the action is
    # still offered as behaviour-neutral.
    assert finding.action["behavior_neutral"] is True
    assert finding.action["bound_members"] == []
    assert finding.confidence == "medium"


def test_bound_member_makes_the_retarget_non_equivalent(tmp_path):
    # The member carries its own bindings, so addressing it directly traverses
    # them while a group command does not. That is a behaviour change, not a
    # cheaper way to do the same thing: flag it and drop confidence.
    ctx = _context(
        tmp_path,
        groups={"office_phantom": ["office_dimmer"]},
        routers=29,
        devices=[_bound("office_dimmer", count=3)],
    )
    _insert(
        ctx,
        [
            (NOW - 1800 + i * 30, "office_phantom", "automation: Office", f"d{i}")
            for i in range(10)
        ],
    )

    (finding,) = groupcast.detect(ctx)
    assert finding.confidence == "low"
    assert finding.action["behavior_neutral"] is False
    assert finding.action["bound_members"] == ["office_dimmer"]
    # One bound member reads in the singular; the plural form is a separate
    # branch because this text is the whole warning a reader acts on.
    assert "this member" in finding.finding
    assert "office_dimmer carries its own Zigbee bindings" in finding.finding
    assert "before retargeting" in finding.finding
    # A binding appearing or disappearing must reopen a dismissal.
    assert finding.fingerprint["bound_members"] == 1


def test_several_bound_members_read_in_the_plural(tmp_path):
    members = ["dimmer_a", "dimmer_b"]
    ctx = _context(
        tmp_path,
        groups={"hall_phantom": members},
        routers=29,
        devices=[_bound(name) for name in members],
    )
    _insert(
        ctx,
        [
            (NOW - 1800 + i * 30, "hall_phantom", "automation: Hall", f"d{i}")
            for i in range(10)
        ],
    )

    (finding,) = groupcast.detect(ctx)
    assert "these members" in finding.finding
    assert "carry their own Zigbee bindings" in finding.finding
    assert finding.fingerprint["bound_members"] == 2


def test_retarget_reports_the_command_load_it_would_add(tmp_path):
    # The recommendation buys airtime with pipeline commands. It has to say so:
    # one group command per render becomes four device commands.
    members = ["s_1", "s_2", "s_3", "s_4"]
    ctx = _context(
        tmp_path,
        groups={"sconces": members},
        routers=29,
        utilization={"z2m-1": {"channel_budget_pct": 0.51, "max_eps": 8.4, "knee_eps": 30.8}},
    )
    _insert(
        ctx,
        [(NOW - 1800 + i * 30, "sconces", "automation: Room", f"d{i}") for i in range(20)],
    )

    (finding,) = groupcast.detect(ctx)
    assert finding.cost["raises_load"] is True
    assert finding.cost["publish_multiplier"] == 4.0
    assert finding.cost["publishes_before"] == 20
    assert finding.cost["publishes_after"] == 80
    assert finding.cost["delta_eps_mean"] > 0
    # The measured capacity limit travels with the cost so a reader can judge.
    assert finding.cost["capacity_limit_eps"] == 30.8
    assert finding.cost["measured_peak_eps"] == 8.4


def test_idle_mesh_bands_the_retarget_low(tmp_path):
    ctx = _context(
        tmp_path,
        groups={"sconces": ["s_1", "s_2"]},
        routers=29,
        utilization={"z2m-1": {"channel_budget_pct": 0.51, "max_eps": 8.4, "knee_eps": 30.8}},
    )
    _insert(
        ctx,
        [(NOW - 1800 + i * 30, "sconces", "automation: Room", f"d{i}") for i in range(20)],
    )

    (finding,) = groupcast.detect(ctx)
    assert finding.significance["band"] == "low"
    assert "not under pressure" in finding.significance["rationale"]


def test_fanout_collapse_reports_lowering_command_load(tmp_path):
    # The other direction: collapsing n unicasts into one group command lowers
    # both currencies, and the cost block credits that rather than staying silent.
    ctx = _context(tmp_path, groups={"hall_lights": TARGETS}, routers=3)
    for occurrence in range(12):
        _insert(ctx, _fanout(NOW - 1800 + occurrence * 60, TARGETS, "aa11"))

    (finding,) = groupcast.detect(ctx)
    assert finding.cost["raises_load"] is False
    assert finding.cost["publishes_before"] == 12 * len(TARGETS)
    assert finding.cost["publishes_after"] == 12
    assert finding.cost["delta_eps_mean"] < 0


def test_large_group_keeps_its_groupcast(tmp_path):
    members = [f"light_{i}" for i in range(15)]
    ctx = _context(tmp_path, groups={"chandelier": members}, routers=3)
    _insert(
        ctx,
        [
            (NOW - 1800 + i * 30, "chandelier", "automation: LR", f"d{i}")
            for i in range(10)
        ],
    )
    assert groupcast.detect(ctx) == []


def test_cofired_subset_group_is_redundant(tmp_path):
    groups = {
        "inner": ["a", "b"],
        "outer": ["a", "b", "c", "d"],
    }
    ctx = _context(tmp_path, groups=groups, routers=20)
    commands = []
    for i in range(10):
        start = NOW - 1800 + i * 30
        digest = f"pair{i}"
        commands.append((start, "inner", "automation: LR", digest))
        commands.append((start + 0.2, "outer", "automation: LR", digest))
    _insert(ctx, commands)

    findings = groupcast.detect(ctx)
    cofired = [f for f in findings if f.action["kind"] == "regroup"]
    assert len(cofired) == 1
    finding = cofired[0]
    assert finding.action["drop_command_to"] == "inner"
    assert finding.action["covered_by"] == "outer"
    assert finding.confidence == "high"


def test_cofired_needs_containment(tmp_path):
    groups = {"inner": ["a", "b"], "outer": ["a", "c"]}  # not a subset
    ctx = _context(tmp_path, groups=groups, routers=20)
    commands = []
    for i in range(10):
        start = NOW - 1800 + i * 30
        commands.append((start, "inner", "automation: LR", f"pair{i}"))
        commands.append((start + 0.2, "outer", "automation: LR", f"pair{i}"))
    _insert(ctx, commands)
    # Both tiny groups read as amplification losers on 20 routers, but no
    # co-fire finding may appear without membership containment.
    assert [f for f in groupcast.detect(ctx) if "drop_command_to" in f.action] == []


def test_chains_without_digest_are_ignored(tmp_path):
    ctx = _context(tmp_path, groups={"hall_lights": TARGETS}, routers=3)
    ctx.conn.executemany(
        "INSERT INTO chains (instance, target, verb, opened_at, client, payload_size, "
        "echo_count, first_echo_ms, redundant, payload_digest) "
        "VALUES ('z2m-1', ?, 'set', ?, 'automation: Hall', 10, 1, 100, 0, NULL)",
        [(target, NOW - 1800 + i * 0.1) for i, target in enumerate(TARGETS)],
    )
    ctx.conn.commit()
    assert groupcast.detect(ctx) == []
