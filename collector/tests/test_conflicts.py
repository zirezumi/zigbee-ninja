"""Per-key conflict detection: telling a disagreement apart from a sequence.

The whole-payload digest cannot make that distinction, and an analysis built on
it reads a single writer's brightness-then-colour-then-config sequence as three
mutually "different payloads". These tests pin the difference.
"""
import time

from zigbee_ninja.attribution.chains import parse_key_digests, payload_key_digests
from zigbee_ninja.attribution.queries import conflicts
from zigbee_ninja.store.db import Database


def make_db(tmp_path) -> Database:
    return Database(tmp_path)


def insert(db, *, target, client, opened_at, payload, instance="z2m-1", skew_ms=0.0):
    db.connect().execute(
        "INSERT INTO chains (instance, target, verb, opened_at, client, payload_size, "
        "echo_count, first_echo_ms, redundant, payload_digest, clock_skew_ms, payload_keys) "
        "VALUES (?, ?, 'set', ?, ?, ?, 0, NULL, 0, 'd', ?, ?)",
        (
            instance,
            target,
            opened_at,
            client,
            len(payload),
            skew_ms,
            payload_key_digests(payload),
        ),
    )
    db.connect().commit()


def test_key_digests_roundtrip_and_ignore_key_order():
    a = payload_key_digests(b'{"brightness": 120, "transition": 0.2}')
    b = payload_key_digests(b'{"transition": 0.2, "brightness": 120}')
    assert a == b
    assert set(parse_key_digests(a)) == {"brightness", "transition"}


def test_key_digests_none_for_non_object_payloads():
    assert payload_key_digests(b"ON") is None
    assert payload_key_digests(b"[1,2]") is None
    assert payload_key_digests(b"\xff\xfe") is None
    assert parse_key_digests(None) == {}


def test_multi_key_sequence_from_one_writer_is_not_a_conflict(tmp_path):
    """The false positive that motivated this: one commander writing three
    different parameters in a row is a sequence, not a disagreement."""
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp", client="script: publish", opened_at=now,
           payload=b'{"ledIntensityWhenOn": 90}')
    insert(db, target="lamp", client="script: publish", opened_at=now + 0.2,
           payload=b'{"ledIntensityWhenOff": 40}')
    insert(db, target="lamp", client="script: publish", opened_at=now + 0.4,
           payload=b'{"ledColorWhenOn": 170}')

    result = conflicts(db, seconds=600, window=2.0)
    assert result["total_conflicting_pairs"] == 0
    assert result["pairs_examined"] == 3  # they were compared, and cleared


def test_two_writers_disagreeing_about_one_key_is_a_conflict(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp", client="automation: Lifecycle", opened_at=now,
           payload=b'{"brightness": 200, "transition": 0.2}')
    insert(db, target="lamp", client="automation: Rendering", opened_at=now + 0.3,
           payload=b'{"brightness": 120, "transition": 0.2}')

    result = conflicts(db, seconds=600, window=2.0)
    assert result["total_conflicting_pairs"] == 1
    assert result["cross_commander_pairs"] == 1
    entry = result["conflicts"][0]
    assert entry["key"] == "brightness"  # transition agreed, so it is not listed
    assert entry["same_commander"] is False
    assert entry["min_gap_ms"] == 300.0


def test_same_commander_conflict_is_reported_but_split(tmp_path):
    """A read-then-write race inside one automation, which needs a different
    fix from two owners fighting, so it is counted separately."""
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp", client="script: publish", opened_at=now,
           payload=b'{"brightness": 10}')
    insert(db, target="lamp", client="script: publish", opened_at=now + 0.1,
           payload=b'{"brightness": 250}')

    result = conflicts(db, seconds=600, window=2.0)
    assert result["total_conflicting_pairs"] == 1
    assert result["cross_commander_pairs"] == 0
    assert result["conflicts"][0]["same_commander"] is True


def test_identical_payloads_are_redundant_not_conflicting(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    for offset in (0.0, 0.4):
        insert(db, target="lamp", client="a", opened_at=now + offset,
               payload=b'{"brightness": 200}')
    assert conflicts(db, seconds=600, window=2.0)["total_conflicting_pairs"] == 0


def test_commands_outside_the_window_do_not_pair(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp", client="a", opened_at=now, payload=b'{"brightness": 1}')
    insert(db, target="lamp", client="b", opened_at=now + 30.0,
           payload=b'{"brightness": 2}')
    assert conflicts(db, seconds=600, window=2.0)["total_conflicting_pairs"] == 0


def test_different_targets_do_not_pair(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp_a", client="a", opened_at=now, payload=b'{"brightness": 1}')
    insert(db, target="lamp_b", client="b", opened_at=now + 0.1,
           payload=b'{"brightness": 2}')
    assert conflicts(db, seconds=600, window=2.0)["total_conflicting_pairs"] == 0


def test_clock_skew_suppresses_an_untrustworthy_pair(tmp_path):
    """Chain timestamps are processing time, so a stall can pull two unrelated
    commands into apparent proximity. The pair is dropped and counted, not
    silently reported as a conflict."""
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp", client="a", opened_at=now, payload=b'{"brightness": 1}')
    insert(db, target="lamp", client="b", opened_at=now + 0.1,
           payload=b'{"brightness": 2}', skew_ms=5000.0)

    result = conflicts(db, seconds=600, window=2.0)
    assert result["total_conflicting_pairs"] == 0
    assert result["pairs_skipped_for_clock_skew"] == 1
