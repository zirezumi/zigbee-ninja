"""Change journal: registry diffs become regime-boundary records (§V2-3)."""

import json
import time

SETUP = {"username": "admin", "password": "correct-horse"}

INFO = {
    "version": "2.10.1",
    "network": {"channel": 15},
    "coordinator": {"type": "ember", "meta": {"revision": "8.0.2 [GA]"}},
    "config": {"serial": {"port": "tcp://x:1"}},
}


def _device(ieee, name, nwk=None, device_type="Router"):
    return {
        "ieee_address": ieee,
        "friendly_name": name,
        "type": device_type,
        "power_source": "Mains",
        "network_address": nwk,
        "definition": {"vendor": "V", "model": "M"},
    }


def _group(group_id, name, member_ieee):
    return {
        "id": group_id,
        "friendly_name": name,
        "members": [{"ieee_address": ieee} for ieee in member_ieee],
    }


def _publish(engine, base, suffix, data):
    engine.on_message(f"{base}/{suffix}", json.dumps(data).encode())


def _journal(client):
    client.app.state.engine.flush_rollups()
    return [
        dict(row)
        for row in client.app.state.db.connect().execute(
            "SELECT ts, instance, kind, subject, detail FROM journal ORDER BY id"
        )
    ]


def test_first_sight_is_baseline_and_noop_republish_is_silent(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    _publish(engine, "z2m-test", "bridge/info", INFO)
    _publish(engine, "z2m-test", "bridge/devices", [_device("0x01", "lamp", 100)])
    _publish(engine, "z2m-test", "bridge/groups", [_group(1, "room", ["0x01"])])
    assert _journal(client) == []

    # Retained topics refresh constantly; identical payloads must not journal.
    _publish(engine, "z2m-test", "bridge/info", INFO)
    _publish(engine, "z2m-test", "bridge/devices", [_device("0x01", "lamp", 100)])
    _publish(engine, "z2m-test", "bridge/groups", [_group(1, "room", ["0x01"])])
    assert _journal(client) == []


def test_device_group_and_info_changes_journal(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    _publish(engine, "z2m-test", "bridge/info", INFO)
    _publish(
        engine,
        "z2m-test",
        "bridge/devices",
        [_device("0x01", "lamp", 100), _device("0x02", "strip", 200)],
    )
    _publish(engine, "z2m-test", "bridge/groups", [_group(1, "room", ["0x01"])])

    # Added + removed + renamed + rejoined, in one registry refresh.
    _publish(
        engine,
        "z2m-test",
        "bridge/devices",
        [_device("0x01", "desk_lamp", 300), _device("0x03", "sensor", 400)],
    )
    # Group membership and rename.
    _publish(engine, "z2m-test", "bridge/groups", [_group(1, "lounge", ["0x01", "0x03"])])
    # Version, channel, and coordinator firmware move.
    changed = dict(INFO)
    changed["version"] = "2.11.0"
    changed["network"] = {"channel": 20}
    changed["coordinator"] = {"type": "ember", "meta": {"revision": "8.1.0 [GA]"}}
    _publish(engine, "z2m-test", "bridge/info", changed)

    rows = _journal(client)
    kinds = {row["kind"]: row for row in rows}
    assert kinds["device_added"]["subject"] == "sensor"
    assert kinds["device_removed"]["subject"] == "strip"
    renamed = json.loads(kinds["device_renamed"]["detail"])
    assert renamed == {"ieee": "0x01", "from": "lamp", "to": "desk_lamp"}
    rejoined = json.loads(kinds["device_rejoined"]["detail"])
    assert rejoined == {"ieee": "0x01", "from": 100, "to": 300}
    assert kinds["group_renamed"]["subject"] == "lounge"
    membership = json.loads(kinds["group_membership_changed"]["detail"])
    assert membership["added"] == ["sensor"]
    assert membership["removed"] == []
    assert membership["size"] == 2
    assert json.loads(kinds["z2m_version_changed"]["detail"]) == {
        "from": "2.10.1",
        "to": "2.11.0",
    }
    assert json.loads(kinds["channel_changed"]["detail"]) == {"from": 15, "to": 20}
    assert json.loads(kinds["coordinator_firmware_changed"]["detail"]) == {
        "from": "8.0.2 [GA]",
        "to": "8.1.0 [GA]",
    }


def test_cross_instance_move_is_annotated(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    _publish(engine, "z2m-a", "bridge/info", INFO)
    _publish(engine, "z2m-b", "bridge/info", INFO)
    _publish(engine, "z2m-a", "bridge/devices", [_device("0x0a", "mover", 1)])
    _publish(engine, "z2m-b", "bridge/devices", [_device("0x0b", "anchor", 2)])

    _publish(engine, "z2m-a", "bridge/devices", [])
    _publish(
        engine,
        "z2m-b",
        "bridge/devices",
        [_device("0x0b", "anchor", 2), _device("0x0a", "mover", 3)],
    )

    rows = _journal(client)
    added = next(row for row in rows if row["kind"] == "device_added")
    assert added["instance"] == "z2m-b"
    assert json.loads(added["detail"])["moved_from"] == "z2m-a"


def test_journal_retention_prunes_old_rows(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    conn = client.app.state.db.connect()
    conn.execute(
        "INSERT INTO journal (ts, instance, kind, subject, detail) "
        "VALUES (?, 'z2m-test', 'device_added', 'ancient', '{}')",
        (time.time() - 91 * 86400,),
    )
    conn.commit()
    _publish(engine, "z2m-test", "bridge/info", INFO)
    _publish(engine, "z2m-test", "bridge/devices", [_device("0x01", "lamp", 100)])
    _publish(engine, "z2m-test", "bridge/devices", [])

    rows = _journal(client)
    assert [row["subject"] for row in rows] == ["lamp"]
