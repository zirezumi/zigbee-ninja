from zigbee_ninja.capacity import ledger
from zigbee_ninja.recommend import redundancy
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.store.db import Database

NOW = 4_000_000.0
LOOKBACK = 3600.0


def _context(tmp_path, groups=(), routers=20):
    db = Database(tmp_path)
    return DetectorContext(
        conn=db.connect(),
        now=NOW,
        lookback_seconds=LOOKBACK,
        instances=["z2m-1"],
        instance_info={},
        knees={},
        is_group=lambda base, target: target in groups,
        group_members=lambda base, target: [],
        groups=lambda base: [{"friendly_name": name} for name in groups],
        devices=lambda base: [],
        router_count_for=lambda base: routers,
        pricing=lambda base: (None, None),
    )


def _insert(ctx, count, target="lamp", client="automation: Chatty", echoes=1):
    ctx.conn.executemany(
        "INSERT INTO chains (instance, target, verb, opened_at, client, payload_size, "
        "echo_count, first_echo_ms, redundant) "
        "VALUES ('z2m-1', ?, 'set', ?, ?, 10, ?, 100, 1)",
        [(target, NOW - 1800 + i, client, echoes) for i in range(count)],
    )
    ctx.conn.commit()


def test_duplicates_grouped_by_commander_and_priced(tmp_path):
    ctx = _context(tmp_path)
    _insert(ctx, 40, target="lamp")
    _insert(ctx, 10, target="strip")

    (finding,) = redundancy.detect(ctx)
    assert finding.detector == "redundancy"
    assert finding.subject == "automation: Chatty"
    assert finding.action["kind"] == "dedupe"
    assert set(finding.action["targets"]) == {"lamp", "strip"}
    per_chain = ledger.price_chain(
        verb="set", group_target=False, n_routers=20, echo_count=1
    ).total_us
    assert finding.saving["us_per_s"] == round(50 * per_chain / LOOKBACK, 1)
    assert finding.confidence == "high"
    assert "replayed" in finding.saving["basis"]
    assert finding.evidence[0]["count"] == 50


def test_tiny_duplicate_volume_stays_quiet(tmp_path):
    ctx = _context(tmp_path)
    _insert(ctx, 2)
    assert redundancy.detect(ctx) == []


def test_group_duplicates_price_with_amplification(tmp_path):
    ctx = _context(tmp_path, groups={"room_group"}, routers=20)
    _insert(ctx, 10, target="room_group", echoes=5)

    (finding,) = redundancy.detect(ctx)
    per_chain = ledger.price_chain(
        verb="set", group_target=True, n_routers=20, echo_count=5
    ).total_us
    assert finding.saving["us_per_s"] == round(10 * per_chain / LOOKBACK, 1)


def test_unattributed_duplicates_get_their_own_row(tmp_path):
    ctx = _context(tmp_path)
    _insert(ctx, 40, client=None)
    (finding,) = redundancy.detect(ctx)
    assert finding.subject == redundancy.UNATTRIBUTED
