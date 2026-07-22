from zigbee_ninja.capacity import ledger
from zigbee_ninja.recommend import redundancy
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.store.db import Database

NOW = 4_000_000.0
LOOKBACK = 3600.0


def _context(tmp_path, groups=(), routers=20, utilization=None):
    """utilization: {instance: {...}} pressure, used for significance banding."""
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
        utilization=utilization or {},
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


def test_dedupe_credits_the_publishes_it_removes(tmp_path):
    # A duplicate the source never sends costs nothing anywhere, so this
    # lowers both currencies; the cost block credits that rather than
    # staying silent and reading as an unpriced trade.
    ctx = _context(tmp_path)
    _insert(ctx, 40, target="lamp")
    _insert(ctx, 10, target="strip")

    (finding,) = redundancy.detect(ctx)
    assert finding.cost["raises_load"] is False
    assert finding.cost["publishes_before"] == 50
    assert finding.cost["publishes_after"] == 0
    assert finding.cost["delta_eps_mean"] < 0
    assert finding.cost["delta_commands_per_day"] < 0


def test_idle_channel_bands_the_dedupe_low(tmp_path):
    ctx = _context(
        tmp_path,
        utilization={"z2m-1": {"channel_budget_pct": 0.6, "max_eps": 4.0}},
    )
    _insert(ctx, 40)

    (finding,) = redundancy.detect(ctx)
    assert finding.significance["band"] == "low"
    assert finding.significance["denominator"] == "channel airtime"
    assert "not under pressure" in finding.significance["rationale"]


def test_busy_channel_bands_the_dedupe_by_the_share_it_frees(tmp_path):
    # Above the pressure floor the band follows relief, and a few dozen µs/s
    # off a channel running at 60% is real but nowhere near a tenth of it.
    ctx = _context(tmp_path, utilization={"z2m-1": {"channel_budget_pct": 60.0}})
    _insert(ctx, 40)

    (finding,) = redundancy.detect(ctx)
    assert finding.significance["band"] == "moderate"
    assert finding.significance["utilization_pct"] == 60.0
    assert finding.significance["relief_pct"] < 10.0


def test_unmeasured_channel_reports_unknown_not_idle(tmp_path):
    ctx = _context(tmp_path)
    _insert(ctx, 40)

    (finding,) = redundancy.detect(ctx)
    assert finding.significance["band"] == "unknown"
    assert "not measured yet" in finding.significance["rationale"]
