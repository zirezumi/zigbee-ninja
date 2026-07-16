from zigbee_ninja.recommend import groupcast
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.store.db import Database

NOW = 3_000_000.0
LOOKBACK = 3600.0


def _context(tmp_path, groups=None, routers=20, pricing=(None, None)):
    """groups: {group_name: [member names]} for one instance 'z2m-1'."""
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
        devices=lambda base: [],
        router_count_for=lambda base: routers,
        pricing=lambda base: pricing,
    )


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
