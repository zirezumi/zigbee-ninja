"""Runtime settings: retention knobs, client labels, API validation (§12)."""

import time

SETUP = {"username": "admin", "password": "correct-horse"}


def authed(client):
    client.post("/api/setup", json=SETUP)
    return client


def test_settings_require_auth(client):
    assert client.get("/api/settings").status_code == 401
    assert client.post("/api/settings", json={}).status_code == 401


def test_defaults_and_partial_update(client):
    authed(client)
    settings = client.get("/api/settings").json()
    assert settings == {
        "retention_rollup_days": 14,
        "retention_chains_hours": 48,
        "retention_topology_snapshots": 20,
        "raw_event_quota_mb": 4096,
        "raw_event_horizon_hours": 48,
        "client_labels": {},
    }

    updated = client.post(
        "/api/settings", json={"retention_rollup_days": 7}
    ).json()
    assert updated["retention_rollup_days"] == 7
    assert updated["retention_chains_hours"] == 48  # untouched


def test_values_clamp_to_sane_ranges(client):
    authed(client)
    updated = client.post(
        "/api/settings",
        json={"retention_rollup_days": 9999, "retention_chains_hours": 0},
    ).json()
    assert updated["retention_rollup_days"] == 365
    assert updated["retention_chains_hours"] == 1


def test_client_labels_roundtrip_and_cleaning(client):
    authed(client)
    updated = client.post(
        "/api/settings",
        json={"client_labels": {"ha-core": "Home Assistant", "  ": "x", "y": "  "}},
    ).json()
    assert updated["client_labels"] == {"ha-core": "Home Assistant"}

    # Labels annotate attribution rows without replacing the raw client id.
    conn = client.app.state.db.connect()
    conn.execute(
        "INSERT INTO chains (instance, target, verb, opened_at, client, "
        "payload_size, echo_count, first_echo_ms, redundant) "
        "VALUES ('z2m-test', 'lamp', 'set', ?, 'ha-core', 2, 0, NULL, 0)",
        (time.time(),),
    )
    conn.commit()
    summary = client.get("/api/attribution/summary?seconds=60").json()
    row = next(r for r in summary["top_clients"] if r["client"] == "ha-core")
    assert row["label"] == "Home Assistant"


def test_retention_knob_drives_rollup_pruning(client):
    authed(client)
    engine = client.app.state.engine
    db = client.app.state.db

    client.post("/api/settings", json={"retention_rollup_days": 1})
    now = int(time.time())
    conn = db.connect()
    conn.execute(
        "INSERT INTO series_10s (ts, instance, kind, count) VALUES (?, ?, ?, ?)",
        (now - 2 * 86400, "z2m-test", "state", 5),
    )
    conn.execute(
        "INSERT INTO series_10s (ts, instance, kind, count) VALUES (?, ?, ?, ?)",
        (now - 3600, "z2m-test", "state", 7),
    )
    conn.commit()

    # The prune only runs when a flush writes rows; seed one completed 10 s
    # window directly so the flush is deterministic.
    window_ts = (now // 10) * 10 - 30
    engine.rates._buckets[("z2m-test", "state")] = {window_ts: 3}
    engine.rates._drained = window_ts - 10
    engine.flush_rollups()

    remaining = [
        row["ts"]
        for row in conn.execute(
            "SELECT ts FROM series_10s WHERE instance = 'z2m-test' ORDER BY ts"
        )
    ]
    assert now - 2 * 86400 not in remaining
    assert now - 3600 in remaining
