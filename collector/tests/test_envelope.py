import json

from zigbee_ninja.capacity import envelope
from zigbee_ninja.store.db import Database
from zigbee_ninja.store.events import RawEventLog

NOW = 1_000_000.0
WINDOW = 3600


def make_log(tmp_path) -> RawEventLog:
    return RawEventLog(tmp_path, clock=lambda: NOW)


def seed_spread_calibration(db: Database, instance: str, started: float, finished: float):
    detail = {
        "plan": {"target": "router-1", "rtt_source": "wire", "mode": "spread"},
        "steps": [{"achieved_eps": 8.0}, {"achieved_eps": 41.2}],
        "knee": {"eps": 31.0, "censored": False, "breach": "saturated",
                 "rtt_source": "wire"},
        "environment": {"z2m_version": "2.10.1", "coordinator_revision": "8.0.2"},
    }
    db.connect().execute(
        "INSERT INTO calibrations (instance, target, started_at, finished_at, "
        "status, knee_eps, detail) VALUES (?, 'router-1', ?, ?, 'completed', 31.0, ?)",
        (instance, started, finished, json.dumps(detail)),
    )
    db.connect().commit()


def add_chain(db: Database, instance: str, opened_at: float, client: str):
    db.connect().execute(
        "INSERT INTO chains (instance, target, verb, opened_at, client, "
        "payload_size, echo_count) VALUES (?, 'light', 'set', ?, ?, 12, 1)",
        (instance, opened_at, client),
    )


def test_wire_peaks_sliding_refinement_and_benchmark_exclusion(tmp_path):
    db = Database(tmp_path)
    log = make_log(tmp_path)

    # A burst of 10 sends straddling a fixed 1 s bin boundary: fixed bins see
    # 6 and 4; the sliding refinement must report 10.
    for i in range(6):
        log.record(999_000.70 + i * 0.05, "wire", "z2m-a", "sendUnicast", "out", None, 30)
    for i in range(4):
        log.record(999_001.00 + i * 0.05, "wire", "z2m-a", "sendMulticast", "out", None, 30)
    # Stray background sends.
    log.record(998_000.0, "wire", "z2m-a", "sendUnicast", "out", None, 30)
    log.record(998_500.0, "wire", "z2m-a", "sendUnicast", "out", None, 30)
    # Responses and incoming frames never count toward TX peaks.
    log.record(999_000.75, "wire", "z2m-a", "sendUnicast", "in", None, 10)
    for i in range(20):
        log.record(999_000.70 + i * 0.01, "wire", "z2m-a", "incomingMessageHandler", "in", None, 40)
    # A fat ramp inside the benchmark window must be excluded entirely.
    for i in range(30):
        log.record(999_150.0 + i * 0.02, "wire", "z2m-a", "sendUnicast", "out", None, 30)
    log.flush()

    seed_spread_calibration(db, "z2m-a", started=999_100.0, finished=999_200.0)

    view = envelope.summarize(
        log, db, WINDOW, [{"base_topic": "z2m-a"}], clock=lambda: NOW
    )
    a = view["instances"]["z2m-a"]
    assert a["coverage"] == "wire"
    assert a["provenance"] == envelope.WIRE_PROVENANCE
    assert a["peak"]["eps_1s"] == 10.0
    assert abs(a["peak"]["at"] - 999_000.70) < 0.01
    assert a["peak"]["eps_10s"] == 1.0
    assert a["benchmark_windows_excluded"] >= 1
    assert a["limits"]["sustained_eps"] == 31.0
    assert a["limits"]["sustained_kind"] == "pipeline_ceiling"
    assert a["limits"]["ceiling_eps"] == 41.2
    assert a["burst_utilization_pct"] == round(10.0 / 31.0 * 100.0, 1)
    assert a["top_bursts"][0]["eps_1s"] == 10.0
    assert view["fanouts"] == []


def test_kneeless_newer_ramp_supplies_neither_limit(tmp_path):
    # A ramp whose first step already breached completes without a knee; it
    # must not supply the hard ceiling while an older run supplies the
    # sustained limit: the two numbers must come from one measurement.
    db = Database(tmp_path)
    log = make_log(tmp_path)
    seed_spread_calibration(db, "z2m-a", started=999_100.0, finished=999_200.0)
    detail = {
        "plan": {"target": "6 routers (spread)", "rtt_source": "wire", "mode": "spread"},
        "steps": [{"achieved_eps": 6.3}],
        "knee": {"eps": None, "censored": False, "breach": "rtt_p95",
                 "rtt_source": "wire"},
        "environment": {"z2m_version": "2.12.1", "coordinator_revision": "8.0.2"},
    }
    db.connect().execute(
        "INSERT INTO calibrations (instance, target, started_at, finished_at, "
        "status, knee_eps, detail) VALUES ('z2m-a', 'x', ?, ?, 'completed', NULL, ?)",
        (999_300.0, 999_400.0, json.dumps(detail)),
    )
    db.connect().commit()
    log.record(998_000.0, "wire", "z2m-a", "sendUnicast", "out", None, 30)
    log.flush()

    view = envelope.summarize(
        log, db, WINDOW, [{"base_topic": "z2m-a"}], clock=lambda: NOW
    )
    limits = view["instances"]["z2m-a"]["limits"]
    assert limits["sustained_eps"] == 31.0
    assert limits["ceiling_eps"] == 41.2


def test_command_fallback_bursts_and_composition(tmp_path):
    db = Database(tmp_path)
    log = make_log(tmp_path)

    for i in range(6):
        add_chain(db, "z2m-b", 997_000.00 + i * 0.1, "Automation A")
    for i in range(5):
        add_chain(db, "z2m-b", 997_000.25 + i * 0.1, "Automation B")
    for i in range(4):
        add_chain(db, "z2m-b", 997_100.00 + i * 0.2, "Automation C")
    db.connect().commit()

    view = envelope.summarize(
        log, db, WINDOW, [{"base_topic": "z2m-b"}], clock=lambda: NOW
    )
    b = view["instances"]["z2m-b"]
    assert b["coverage"] == "commands"
    assert b["provenance"] == envelope.COMMANDS_PROVENANCE
    # All eleven overlapping commands land inside one sliding second.
    assert b["peak"]["eps_1s"] == 11.0
    assert b["limits"] is None
    assert b["burst_utilization_pct"] is None

    names = [entry["commander"] for entry in b["commanders"]]
    assert names == ["Automation A", "Automation B", "Automation C"]
    assert b["commanders"][0]["worst"]["peak_eps"] == 6.0
    assert b["commanders"][0]["worst"]["commands"] == 6

    composed = b["composed_worst"]
    assert composed["commanders"] == ["Automation A", "Automation B"]
    assert composed["eps"] == 11.0


def test_fanout_needs_observed_cross_instance_overlap(tmp_path):
    db = Database(tmp_path)
    log = make_log(tmp_path)

    # Tap Dial bursts on both instances at the same moment: a fan-out.
    for i in range(5):
        add_chain(db, "z2m-a", 998_800.00 + i * 0.1, "Tap Dial - All On")
    for i in range(4):
        add_chain(db, "z2m-b", 998_800.20 + i * 0.1, "Tap Dial - All On")
    # Solo bursts on both instances at different times: not a fan-out.
    for i in range(4):
        add_chain(db, "z2m-a", 998_900.00 + i * 0.2, "Solo")
    for i in range(4):
        add_chain(db, "z2m-b", 998_950.00 + i * 0.2, "Solo")
    db.connect().commit()

    view = envelope.summarize(
        log,
        db,
        WINDOW,
        [{"base_topic": "z2m-a"}, {"base_topic": "z2m-b"}],
        clock=lambda: NOW,
    )
    fanouts = view["fanouts"]
    assert len(fanouts) == 1
    assert fanouts[0]["commander"] == "Tap Dial - All On"
    assert fanouts[0]["combined_eps"] == 9.0
    assert fanouts[0]["instances"] == {"z2m-a": 5.0, "z2m-b": 4.0}


def test_no_traffic_instance_reports_none(tmp_path):
    db = Database(tmp_path)
    log = make_log(tmp_path)
    view = envelope.summarize(
        log, db, WINDOW, [{"base_topic": "z2m-quiet"}], clock=lambda: NOW
    )
    quiet = view["instances"]["z2m-quiet"]
    assert quiet["coverage"] == "none"
    assert quiet["peak"] is None
    assert quiet["commanders"] == []
    assert quiet["composed_worst"] is None


def test_envelope_endpoint_requires_auth_and_serves_shape(client):
    assert client.get("/api/envelope").status_code == 401
    client.post("/api/setup", json={"username": "admin", "password": "correct-horse"})
    view = client.get("/api/envelope").json()
    assert view["window_seconds"] == 86400
    assert view["instances"] == {}
    assert view["fanouts"] == []
