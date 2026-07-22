import json
import time
from types import SimpleNamespace

import pytest

from zigbee_ninja.recommend.runner import (
    FIRST_RUN_DELAY_SECONDS,
    RUN_INTERVAL_SECONDS,
    RecommendationEngine,
)
from zigbee_ninja.recommend.store import (
    Finding,
    RecommendationStore,
    materially_changed,
)
from zigbee_ninja.store.db import Database

SETUP = {"username": "admin", "password": "correct-horse"}


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _finding(subject="automation: Lights", us_per_s=120.0, fingerprint=None, **kwargs):
    defaults = dict(
        detector="redundancy",
        instance="z2m-test",
        subject=subject,
        finding=f"{subject} resends identical commands",
        action={"kind": "dedupe", "commander": subject},
        saving={
            "us_per_s": us_per_s,
            "pct_of_budget": 0.01,
            "basis": "replayed 24 h of recorded traffic",
            "provenance": "modeled",
        },
        confidence="high",
        evidence=[{"kind": "chains", "count": 12}],
        fingerprint=fingerprint or {"us_per_s": us_per_s},
    )
    defaults.update(kwargs)
    return Finding(**defaults)


def _store(tmp_path, clock=None):
    return RecommendationStore(Database(tmp_path), clock=clock or FakeClock())


# -- material-change rule ---------------------------------------------------------


def test_materially_changed_ratio_and_structure():
    assert not materially_changed({"rate": 10.0}, {"rate": 10.0})
    assert not materially_changed({"rate": 10.0}, {"rate": 13.0})  # under 1.5x
    assert materially_changed({"rate": 10.0}, {"rate": 15.0})  # at 1.5x
    assert materially_changed({"rate": 10.0}, {"rate": 4.0})  # shrink counts too
    assert materially_changed({"rate": 0.0}, {"rate": 1.0})  # off zero
    assert materially_changed({"rate": 10.0}, {"rate": -10.0})  # sign flip
    assert materially_changed({"rate": 10.0}, {"other": 10.0})  # structural
    assert materially_changed({"group": "a"}, {"group": "b"})  # non-numeric
    assert not materially_changed({"group": "a"}, {"group": "a"})


# -- store reconciliation ----------------------------------------------------------


def test_sync_inserts_refreshes_and_deletes_open_rows(tmp_path):
    clock = FakeClock()
    store = _store(tmp_path, clock)

    counts = store.sync("redundancy", [_finding(us_per_s=100.0)])
    assert counts["inserted"] == 1
    (row,) = store.queue("open")
    assert row["id"].startswith("rec-")
    assert row["created_at"] == clock.now
    created_at = row["created_at"]

    clock.now += 3600
    counts = store.sync("redundancy", [_finding(us_per_s=110.0)])
    assert counts["updated"] == 1
    (row,) = store.queue("open")
    assert row["created_at"] == created_at  # same row, no queue churn
    assert row["updated_at"] == clock.now
    assert row["saving"]["us_per_s"] == 110.0

    counts = store.sync("redundancy", [])
    assert counts["deleted"] == 1
    assert store.queue("open") == []


def test_sync_only_touches_its_own_detector(tmp_path):
    store = _store(tmp_path)
    store.sync("redundancy", [_finding()])
    store.sync("pacing", [_finding(detector="pacing", subject="automation: Other")])
    store.sync("redundancy", [])
    states = {row["detector"] for row in store.queue("open")}
    assert states == {"pacing"}


def test_dismissal_is_durable_until_inputs_change_materially(tmp_path):
    clock = FakeClock()
    store = _store(tmp_path, clock)
    store.sync("redundancy", [_finding(us_per_s=100.0, fingerprint={"us_per_s": 100.0})])
    (row,) = store.queue("open")
    store.set_state(row["id"], "dismissed", note="acceptable cost")

    # Re-detection at a similar magnitude never reopens.
    clock.now += 3600
    counts = store.sync(
        "redundancy", [_finding(us_per_s=120.0, fingerprint={"us_per_s": 120.0})]
    )
    assert counts["held"] == 1
    (dismissed,) = store.queue("dismissed")
    assert dismissed["state_note"] == "acceptable cost"
    assert dismissed["saving"]["us_per_s"] == 100.0  # dismissed content frozen

    # A materially larger input reopens with a note.
    clock.now += 3600
    counts = store.sync(
        "redundancy", [_finding(us_per_s=400.0, fingerprint={"us_per_s": 400.0})]
    )
    assert counts["reopened"] == 1
    (reopened,) = store.queue("open")
    assert reopened["state"] == "open"
    assert "materially" in reopened["state_note"]
    assert reopened["saving"]["us_per_s"] == 400.0


def test_dismissed_rows_survive_detector_silence(tmp_path):
    store = _store(tmp_path)
    store.sync("redundancy", [_finding()])
    (row,) = store.queue("open")
    store.set_state(row["id"], "dismissed")
    store.sync("redundancy", [])  # silence deletes open rows only
    assert store.queue("dismissed") != []


def test_applied_rows_are_never_touched_by_sync(tmp_path):
    store = _store(tmp_path)
    store.sync("redundancy", [_finding(us_per_s=100.0)])
    (row,) = store.queue("open")
    store.set_state(row["id"], "applied")
    store.sync("redundancy", [_finding(us_per_s=900.0)])
    (applied,) = store.queue("applied")
    assert applied["saving"]["us_per_s"] == 100.0
    store.sync("redundancy", [])
    assert store.queue("applied") != []


def test_set_state_validates_transitions(tmp_path):
    store = _store(tmp_path)
    store.sync("redundancy", [_finding()])
    (row,) = store.queue("open")
    rec_id = row["id"]

    assert store.set_state("rec-nonexistent", "dismissed") is None
    with pytest.raises(ValueError):
        store.set_state(rec_id, "verified")  # verdict states are V2.M4's
    store.set_state(rec_id, "dismissed")
    with pytest.raises(ValueError):
        store.set_state(rec_id, "applied")  # dismissed goes back to open first
    store.set_state(rec_id, "open")
    store.set_state(rec_id, "applied")
    assert store.get(rec_id)["state"] == "applied"
    store.set_state(rec_id, "open")  # undo an accidental applied mark


def test_queue_orders_by_saving_times_confidence(tmp_path):
    store = _store(tmp_path)
    store.sync(
        "redundancy",
        [
            _finding(subject="a", us_per_s=100.0, confidence="high"),  # 100
            _finding(subject="b", us_per_s=500.0, confidence="low"),  # 150
            _finding(subject="c", us_per_s=400.0, confidence="medium"),  # 240
            _finding(  # latency-only: ranks below any airtime saving
                subject="d",
                us_per_s=0.0,
                confidence="high",
                saving={"us_per_s": 0.0, "pct_of_budget": 0.0, "p95_ms": 250.0},
            ),
        ],
    )
    order = [row["subject"] for row in store.queue("open")]
    assert order == ["c", "b", "a", "d"]


def test_counts_by_state_and_instance(tmp_path):
    store = _store(tmp_path)
    store.sync(
        "redundancy",
        [
            _finding(subject="a", instance="z2m-1"),
            _finding(subject="b", instance="z2m-1"),
            _finding(subject="c", instance="z2m-2"),
        ],
    )
    row = store.queue("open")[0]
    store.set_state(row["id"], "dismissed")
    counts = store.counts()
    assert counts["by_state"]["open"] == 2
    assert counts["by_state"]["dismissed"] == 1
    assert sum(counts["open_by_instance"].values()) == 2


# -- runner cadence + isolation ------------------------------------------------------


def _fake_registry():
    return SimpleNamespace(
        snapshot=lambda: [],
        is_group=lambda base, target: False,
        group_members=lambda base, target: [],
        groups=lambda base: [],
        devices=lambda base: [],
        router_count_for=lambda base: 0,
    )


def test_runner_first_run_delay_and_interval(tmp_path):
    clock = FakeClock()
    engine = RecommendationEngine(
        Database(tmp_path),
        registry=_fake_registry(),
        pricing=lambda instance: (None, None),
        clock=clock,
    )
    assert not engine.due()
    clock.now += FIRST_RUN_DELAY_SECONDS
    assert engine.due()
    engine.run()
    assert not engine.due()
    clock.now += RUN_INTERVAL_SECONDS
    assert engine.due()


def test_runner_isolates_a_crashing_detector(tmp_path):
    clock = FakeClock()
    engine = RecommendationEngine(
        Database(tmp_path),
        registry=_fake_registry(),
        pricing=lambda instance: (None, None),
        clock=clock,
    )
    healthy = SimpleNamespace(NAME="redundancy", detect=lambda ctx: [_finding()])
    engine._detectors = [healthy]
    engine.run()
    assert engine.store.queue("open") != []

    # The healthy detector starts crashing: its rows must survive untouched.
    crashing = SimpleNamespace(
        NAME="redundancy",
        detect=lambda ctx: (_ for _ in ()).throw(RuntimeError("window scan failed")),
    )
    other = SimpleNamespace(
        NAME="pacing",
        detect=lambda ctx: [_finding(detector="pacing", subject="automation: Other")],
    )
    engine._detectors = [crashing, other]
    result = engine.run()
    assert "error" in result["detectors"]["redundancy"]
    assert result["detectors"]["pacing"]["findings"] == 1
    detectors = {row["detector"] for row in engine.store.queue("open")}
    assert detectors == {"redundancy", "pacing"}


def test_context_failure_is_reported_not_raised(tmp_path):
    # A pass that dies assembling its context (headroom, registry) must record
    # the failure and return, not raise into the flush loop where it would be
    # swallowed and look like a healthy quiet fleet.
    clock = FakeClock()
    engine = RecommendationEngine(
        Database(tmp_path),
        registry=_fake_registry(),
        pricing=lambda instance: (None, None),
        clock=clock,
    )
    engine._context = lambda: (_ for _ in ()).throw(RuntimeError("headroom exploded"))
    result = engine.run()
    assert "context assembly failed" in result["error"]
    assert "headroom exploded" in result["error"]
    assert engine.status()["last_result"]["error"]


def test_note_run_error_surfaces_on_status(tmp_path):
    # The flush loop calls this when run() raised before it could record
    # itself; the failure has to reach the API status rather than vanish.
    clock = FakeClock()
    engine = RecommendationEngine(
        Database(tmp_path),
        registry=_fake_registry(),
        pricing=lambda instance: (None, None),
        clock=clock,
    )
    engine.note_run_error(RuntimeError("thread pool rejected the pass"))
    last = engine.status()["last_result"]
    assert "thread pool rejected the pass" in last["error"]
    assert last["ran_at"] == clock.now


# -- API ---------------------------------------------------------------------------


def test_recommendations_api_requires_auth(client):
    assert client.get("/api/recommendations").status_code == 401
    assert client.post("/api/recommendations/run").status_code == 401


def test_recommendations_api_lifecycle(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine

    view = client.get("/api/recommendations").json()
    assert view["recommendations"] == []
    assert view["counts"]["by_state"]["open"] == 0
    assert view["run"]["last_run_at"] is None

    fake = SimpleNamespace(NAME="redundancy", detect=lambda ctx: [_finding()])
    engine.recommendations._detectors = [fake]
    run = client.post("/api/recommendations/run")
    assert run.status_code == 202
    assert run.json()["detectors"]["redundancy"]["inserted"] == 1

    view = client.get("/api/recommendations").json()
    (rec,) = view["recommendations"]
    for field in (
        "id",
        "detector",
        "instance",
        "finding",
        "action",
        "saving",
        "confidence",
        "evidence",
        "state",
    ):
        assert field in rec  # §V2-5 frozen shape served verbatim

    bad = client.post(f"/api/recommendations/{rec['id']}/state", json={"state": "verified"})
    assert bad.status_code == 400
    missing = client.post("/api/recommendations/rec-none/state", json={"state": "dismissed"})
    assert missing.status_code == 404

    ok = client.post(
        f"/api/recommendations/{rec['id']}/state",
        json={"state": "dismissed", "note": "fine as is"},
    )
    assert ok.status_code == 200
    assert ok.json()["state"] == "dismissed"
    assert client.get("/api/recommendations?state=dismissed").json()["recommendations"]
    assert client.get("/api/recommendations?state=bogus").status_code == 400


# -- chains gain the payload digest (groupcast detector's identity evidence) --------


def test_finalized_chains_persist_payload_digest(client):
    from zigbee_ninja.attribution.chains import ChainTracker

    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    clock = FakeClock(float(int(time.time() / 10) * 10))
    engine.chains = ChainTracker(resolve_members=engine._resolve_members, clock=clock)
    engine.on_message(
        "z2m-test/bridge/info",
        json.dumps({"version": "2.3.0", "network": {"channel": 15}, "config": {}}).encode(),
    )
    engine.on_message(
        "z2m-test/bridge/devices",
        json.dumps(
            [
                {
                    "ieee_address": "0x02",
                    "friendly_name": "lamp",
                    "type": "Router",
                    "power_source": "Mains",
                    "definition": {"vendor": "V", "model": "M"},
                }
            ]
        ).encode(),
    )
    engine.on_message("z2m-test/lamp/set", b'{"state":"ON"}')
    clock.now += 20
    engine.flush_rollups()
    row = client.app.state.db.connect().execute(
        "SELECT payload_digest FROM chains"
    ).fetchone()
    assert row["payload_digest"] is not None
    assert len(row["payload_digest"]) == 12
