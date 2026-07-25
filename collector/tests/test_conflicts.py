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
    assert result["total_pairs_same_key_different_value"] == 0
    assert result["pairs_examined"] == 3  # they were compared, and cleared


def test_two_writers_disagreeing_about_one_key_is_a_conflict(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp", client="automation: Lifecycle", opened_at=now,
           payload=b'{"brightness": 200, "transition": 0.2}')
    insert(db, target="lamp", client="automation: Rendering", opened_at=now + 0.3,
           payload=b'{"brightness": 120, "transition": 0.2}')

    result = conflicts(db, seconds=600, window=2.0)
    assert result["novel_pairs"] == 1
    assert result["novel_cross_commander_pairs"] == 1
    entry = result["conflicts"][0]
    assert entry["key"] == "brightness"  # transition agreed, so it is not listed
    assert entry["kind"] == "novel"
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
    assert result["novel_pairs"] == 1
    assert result["novel_cross_commander_pairs"] == 0
    assert result["conflicts"][0]["same_commander"] is True


def test_alternating_state_machine_is_not_the_headline(tmp_path):
    """The real false positive this classification exists for.

    Measured live: an LED bar cycling 56 -> 0 -> 56 -> 0, and an indicator
    timeout cycling `Stay Off` -> `3 Seconds`. Every transition sets the same
    key to a different value inside the window, and every one of them is
    intentional. They must not sit in the headline count.
    """
    db = make_db(tmp_path)
    now = time.time()
    for i, bri in enumerate([56, 0, 56, 0, 56]):
        insert(db, target="bar", client="script: publish", opened_at=now + i * 0.5,
               payload=b'{"ledIntensityWhenOn": %d}' % bri)

    result = conflicts(db, seconds=600, window=2.0)
    # Exactly one novel pair: the very first sighting of 0 on this key really is
    # new information. Every transition after that is the cycle repeating, and
    # the cycle is what used to drown the signal.
    assert result["novel_pairs"] == 1
    assert result["alternating_pairs"] >= 3
    assert result["alternating"][0]["kind"] == "alternating"


def test_lli_sandwich_shape_lands_in_alternating(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    cycle = ["Stay Off", "3 Seconds", "Stay Off", "3 Seconds", "Stay Off", "3 Seconds"]
    for i, val in enumerate(cycle):
        insert(db, target="dimmer", client="script: silent sync",
               opened_at=now + i * 0.4,
               payload=b'{"loadLevelIndicatorTimeout": "%s"}' % val.encode())
    result = conflicts(db, seconds=600, window=2.0)
    assert result["novel_pairs"] == 1  # first sighting of the second value only
    assert result["alternating_pairs"] >= 5


def test_writer_diversity_is_reported_even_without_a_novel_pair(tmp_path):
    """Divided ownership is the precondition for the bug and is intent-free, so
    it survives where the pair classification cannot."""
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp", client="automation: A", opened_at=now,
           payload=b'{"brightness": 10}')
    insert(db, target="lamp", client="automation: B", opened_at=now + 60.0,
           payload=b'{"brightness": 10}')

    result = conflicts(db, seconds=600, window=2.0)
    assert result["novel_pairs"] == 0  # never collided
    entry = next(e for e in result["writer_diversity"] if e["key"] == "brightness")
    assert entry["writer_count"] == 2
    assert entry["writers"] == ["automation: A", "automation: B"]


def test_identical_payloads_are_redundant_not_conflicting(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    for offset in (0.0, 0.4):
        insert(db, target="lamp", client="a", opened_at=now + offset,
               payload=b'{"brightness": 200}')
    result = conflicts(db, seconds=600, window=2.0)
    assert result["total_pairs_same_key_different_value"] == 0


def test_commands_outside_the_window_do_not_pair(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp", client="a", opened_at=now, payload=b'{"brightness": 1}')
    insert(db, target="lamp", client="b", opened_at=now + 30.0,
           payload=b'{"brightness": 2}')
    assert conflicts(db, seconds=600, window=2.0)["novel_pairs"] == 0


def test_different_targets_do_not_pair(tmp_path):
    db = make_db(tmp_path)
    now = time.time()
    insert(db, target="lamp_a", client="a", opened_at=now, payload=b'{"brightness": 1}')
    insert(db, target="lamp_b", client="b", opened_at=now + 0.1,
           payload=b'{"brightness": 2}')
    assert conflicts(db, seconds=600, window=2.0)["novel_pairs"] == 0


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
    assert result["novel_pairs"] == 0
    assert result["pairs_skipped_for_clock_skew"] == 1
