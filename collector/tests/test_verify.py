"""Applied-recommendation verification: §V2-6 journal-detected application
and measured before/after verdicts (V2_PROPOSAL.md)."""

import json

from zigbee_ninja.capacity.ledger import utc_day
from zigbee_ninja.recommend import verify
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.recommend.store import Finding, RecommendationStore
from zigbee_ninja.store.db import Database
from zigbee_ninja.store.events import RawEventLog

NOW = 1_700_000_000.0
DAY = 86400.0


def clock() -> float:
    return NOW


def make_store(tmp_path):
    db = Database(tmp_path)
    return db, RecommendationStore(db, clock=clock)


def context(db, events=None):
    return DetectorContext(
        conn=db.connect(),
        now=NOW,
        lookback_seconds=DAY,
        instances=["z2m-a", "z2m-b"],
        instance_info={},
        knees={},
        is_group=lambda base, target: False,
        group_members=lambda base, target: [],
        groups=lambda base: [],
        devices=lambda base: [],
        router_count_for=lambda base: 0,
        pricing=lambda base: (None, None),
        db=db,
        events_log=events,
    )


def finding(detector, subject, instance="z2m-a", action=None, evidence=None):
    return Finding(
        detector=detector,
        instance=instance,
        subject=subject,
        finding="test finding",
        action=action or {},
        saving={"us_per_s": 1.0},
        confidence="medium",
        evidence=evidence or [],
        fingerprint={"n": 1},
    )


def add_device_spend(db, device, day_offsets_us):
    conn = db.connect()
    for offset, us in day_offsets_us.items():
        conn.execute(
            "INSERT INTO ledger_device_daily (instance, day, device, publishes, "
            "autonomous_us, provenance) VALUES ('z2m-a', ?, ?, 1, ?, '')",
            (utc_day(NOW + offset * DAY), device, us),
        )
    conn.commit()


def applied_reporting_rec(store, boundary):
    store.sync("reporting", [finding("reporting", "sensor_1")])
    rec_id = store.queue("open")[0]["id"]
    conn = store._db.connect()
    conn.execute(
        "UPDATE recommendations SET state = 'applied', state_changed_at = ? "
        "WHERE id = ?",
        (boundary, rec_id),
    )
    conn.commit()
    return rec_id


def test_spend_verdict_improved(tmp_path):
    db, store = make_store(tmp_path)
    boundary = NOW - 3 * DAY
    rec_id = applied_reporting_rec(store, boundary)
    # Before days spend 1000 µs/day; the two completed days after: 100.
    add_device_spend(
        db, "sensor_1", {-6: 1000, -5: 1000, -4: 1000, -2: 100, -1: 100}
    )
    result = verify.run(store, context(db))
    assert result.get("verified") == 1
    rec = store.get(rec_id)
    assert rec["state"] == "verified"
    receipts = rec["verification"]
    assert receipts["verdict"] == "improved"
    assert receipts["before_us_per_day"] == 1000
    assert receipts["after_us_per_day"] == 100
    assert receipts["after_days"] == 2


def test_spend_verdict_regressed_and_reopen(tmp_path):
    db, store = make_store(tmp_path)
    boundary = NOW - 3 * DAY
    rec_id = applied_reporting_rec(store, boundary)
    add_device_spend(
        db, "sensor_1", {-6: 1000, -5: 1000, -4: 1000, -2: 2000, -1: 2000}
    )
    result = verify.run(store, context(db))
    assert result.get("regressed") == 1
    rec = store.get(rec_id)
    assert rec["state"] == "regressed"
    # A regressed row reopens with its result attached (§V2-6).
    reopened = store.set_state(rec_id, "open")
    assert reopened["state"] == "open"
    assert reopened["verification"]["verdict"] == "regressed"


def test_spend_pending_without_enough_days(tmp_path):
    db, store = make_store(tmp_path)
    boundary = NOW - 1.5 * DAY  # only one completed day after
    rec_id = applied_reporting_rec(store, boundary)
    add_device_spend(db, "sensor_1", {-6: 1000, -5: 1000, -1: 100})
    result = verify.run(store, context(db))
    assert result.get("pending") == 1
    rec = store.get(rec_id)
    assert rec["state"] == "applied"
    assert rec["verification"]["verdict"] == "pending"


def test_no_material_change_finalizes_at_horizon(tmp_path):
    db, store = make_store(tmp_path)
    boundary = NOW - 15 * DAY
    rec_id = applied_reporting_rec(store, boundary)
    spend = {offset: 1000 for offset in range(-21, -15)}
    spend.update({offset: 950 for offset in range(-14, 0)})
    add_device_spend(db, "sensor_1", spend)
    result = verify.run(store, context(db))
    assert result.get("no_material_change_final") == 1
    rec = store.get(rec_id)
    assert rec["state"] == "applied"
    assert rec["verification"]["finalized"] is True
    # The next pass leaves it alone.
    result = verify.run(store, context(db))
    assert result.get("finalized") == 1


def test_journal_detects_applied_rebalancing_moves(tmp_path):
    db, store = make_store(tmp_path)
    action = {
        "kind": "rebalance",
        "moves": [
            {
                "kind": "device",
                "subject": "hot_1",
                "from_instance": "z2m-a",
                "to_instance": "z2m-b",
            }
        ],
    }
    store.sync("rebalancing", [finding("rebalancing", "z2m-a", action=action)])
    conn = db.connect()
    # The finding predates the move: journal matching requires it.
    conn.execute(
        "UPDATE recommendations SET created_at = ?", (NOW - 5 * DAY,)
    )
    moved_at = NOW - 2 * DAY
    conn.execute(
        "INSERT INTO journal (ts, instance, kind, subject, detail) "
        "VALUES (?, 'z2m-b', 'device_added', 'hot_1', ?)",
        (moved_at, json.dumps({"ieee": "0x01", "moved_from": "z2m-a"})),
    )
    conn.commit()

    result = verify.run(store, context(db))
    assert result["auto_applied"] == 1
    rec = store.queue("applied")[0]
    assert rec["state_changed_at"] == moved_at
    assert "change journal" in rec["state_note"]


def test_journal_ignores_moves_from_elsewhere(tmp_path):
    db, store = make_store(tmp_path)
    action = {
        "kind": "rebalance",
        "moves": [
            {
                "kind": "device",
                "subject": "hot_1",
                "from_instance": "z2m-a",
                "to_instance": "z2m-b",
            }
        ],
    }
    store.sync("rebalancing", [finding("rebalancing", "z2m-a", action=action)])
    conn = db.connect()
    conn.execute(
        "UPDATE recommendations SET created_at = ?", (NOW - 5 * DAY,)
    )
    conn.execute(
        "INSERT INTO journal (ts, instance, kind, subject, detail) "
        "VALUES (?, 'z2m-b', 'device_added', 'hot_1', ?)",
        (NOW - DAY, json.dumps({"ieee": "0x01", "moved_from": "z2m-c"})),
    )
    conn.commit()
    result = verify.run(store, context(db))
    assert result["auto_applied"] == 0


def test_rebalancing_burst_verdict_improved(tmp_path):
    db, store = make_store(tmp_path)
    events = RawEventLog(tmp_path, clock=clock)
    evidence = [
        {"kind": "burst_pressure", "instance": "z2m-a", "peak_eps": 12.0},
        {"kind": "capacity_limit", "instance": "z2m-a", "eps": 8.0},
    ]
    store.sync(
        "rebalancing", [finding("rebalancing", "z2m-a", evidence=evidence)]
    )
    rec_id = store.queue("open")[0]["id"]
    conn = db.connect()
    conn.execute(
        "UPDATE recommendations SET state = 'applied', state_changed_at = ? "
        "WHERE id = ?",
        (NOW - 2 * DAY, rec_id),
    )
    conn.commit()
    # The trailing day's worst second: 3 commands, well under the 8/s limit.
    for i in range(3):
        events.record(
            NOW - 3600 + i * 0.2, "mqtt", "z2m-a", "command", "in", "lamp/set", 10
        )
    events.flush()

    result = verify.run(store, context(db, events))
    assert result.get("verified") == 1
    receipts = store.get(rec_id)["verification"]
    assert receipts["verdict"] == "improved"
    assert receipts["after_peak_eps"] == 3.0
    assert receipts["sustained_limit_eps"] == 8.0


def test_pacing_verdict_regressed_when_bursts_continue(tmp_path):
    db, store = make_store(tmp_path)
    action = {"kind": "pace", "target_eps": 4.0}
    evidence = [{"kind": "window", "peak_eps": 10.0}]
    store.sync(
        "pacing", [finding("pacing", "Kitchen Lifecycle", action=action, evidence=evidence)]
    )
    rec_id = store.queue("open")[0]["id"]
    conn = db.connect()
    boundary = NOW - 2 * DAY
    conn.execute(
        "UPDATE recommendations SET state = 'applied', state_changed_at = ? "
        "WHERE id = ?",
        (boundary, rec_id),
    )
    for i in range(10):
        conn.execute(
            "INSERT INTO chains (instance, target, verb, opened_at, client, "
            "payload_size, echo_count, redundant) "
            "VALUES ('z2m-a', 'lamp', 'set', ?, 'Kitchen Lifecycle', 10, 0, 0)",
            (NOW - 3600 + i * 0.05,),
        )
    conn.commit()

    result = verify.run(store, context(db))
    assert result.get("regressed") == 1
    receipts = store.get(rec_id)["verification"]
    assert receipts["verdict"] == "regressed"
    assert receipts["after_peak_eps"] == 10.0
