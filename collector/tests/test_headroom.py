import json

from zigbee_ninja.capacity import headroom
from zigbee_ninja.store.db import Database


def seed(db: Database, spread: bool = False):
    conn = db.connect()
    # Load windows for z2m-a: 2, 4, and 16 eps of TX plus some RX airtime.
    for ts, tx_frames, p95 in ((1000, 20, 50.0), (1010, 40, 60.0), (1020, 160, 300.0)):
        conn.execute(
            "INSERT INTO airtime_10s (ts, instance, bucket, airtime_us, frames) "
            "VALUES (?, 'z2m-a', 'tx_unicast', ?, ?)",
            (ts, tx_frames * 1000.0, tx_frames),
        )
        conn.execute(
            "INSERT INTO airtime_10s (ts, instance, bucket, airtime_us, frames) "
            "VALUES (?, 'z2m-a', 'rx', 5000.0, 10)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO latency_10s (ts, instance, count, p50_ms, p95_ms, max_ms) "
            "VALUES (?, 'z2m-a', 10, 40.0, ?, 500.0)",
            (ts, p95),
        )

    def record(instance, started, knee_eps, breach, censored, env, mode="single"):
        detail = {
            "plan": {"target": f"router-{instance}", "rtt_source": "wire", "mode": mode},
            "steps": [],
            "knee": {
                "eps": knee_eps,
                "censored": censored,
                "breach": breach,
                "breach_rate_eps": None,
                "rtt_source": "wire",
            },
            "abort_reason": None,
            "environment": env,
        }
        conn.execute(
            "INSERT INTO calibrations (instance, target, started_at, finished_at, "
            "status, knee_eps, detail) VALUES (?, ?, ?, ?, 'completed', ?, ?)",
            (instance, f"router-{instance}", started, started + 150, knee_eps,
             json.dumps(detail)),
        )

    env_a = {"z2m_version": "2.10.1", "coordinator_type": "EmberZNet",
             "coordinator_revision": "8.0.2"}
    record("z2m-a", 100, 10.0, "saturated", False, env_a)  # superseded
    record("z2m-a", 500, 16.0, "saturated", False, env_a)  # latest wins
    record("z2m-b", 400, 30.0, None, True, {"z2m_version": "2.9.0",
                                            "coordinator_type": "EmberZNet",
                                            "coordinator_revision": "7.4.4"})
    # Aborted runs never contribute a knee: but their window (1021–1029,
    # overlapping the ts=1020 rollup) is still self-traffic and must be
    # excluded from the headroom aggregates like any benchmark window.
    conn.execute(
        "INSERT INTO calibrations (instance, target, started_at, finished_at, "
        "status, knee_eps, detail) VALUES ('z2m-a', 'x', 1021, 1029, 'aborted', NULL, '{}')"
    )
    if spread:
        # A spread ramp that ended on a wire-RTT breach: the measured NCP knee.
        record("z2m-a", 700, 55.0, "rtt_p95", False, env_a, mode="spread")
    conn.commit()


INSTANCES_INFO = [
    {"base_topic": "z2m-a", "version": "2.10.1", "coordinator_revision": "8.0.2"},
    {"base_topic": "z2m-b", "version": "2.10.1", "coordinator_revision": "8.0.2"},
]


def test_summarize_joins_knees_rates_and_scatter(tmp_path):
    db = Database(tmp_path)
    seed(db)
    view = headroom.summarize(db, 3600, INSTANCES_INFO, clock=lambda: 2000.0)

    a = view["instances"]["z2m-a"]
    # Latest calibration wins; a saturated ramp is the pipeline ceiling and
    # only lower-bounds the NCP knee.
    assert a["knee"]["eps"] == 16.0
    assert a["knee"]["kind"] == "pipeline_ceiling"
    assert a["knee"]["stale_environment"] is False
    assert a["denominators"]["ncp_knee"] == {"eps": 16.0, "provenance": "lower_bound"}
    assert a["denominators"]["pipeline"] == {"eps": 16.0, "provenance": "measured"}
    assert a["denominators"]["channel_budget"]["pct"] > 0

    # The 16 eps window falls inside the aborted run's benchmark window and is
    # excluded from every aggregate (§11.5): rates come from 2 and 4 only.
    assert a["benchmark_windows_excluded"] == 1
    assert a["rates"] == {"p50_eps": 4.0, "p95_eps": 4.0, "max_eps": 4.0, "windows": 2}
    assert a["headroom"]["steady_eps"] == 12.0
    assert a["headroom"]["knee_utilization_pct"] == 25.0

    # Scatter joins load and latency windows one to one (benchmark excluded).
    assert a["scatter"] == [
        {"eps": 2.0, "p95_ms": 50.0},
        {"eps": 4.0, "p95_ms": 60.0},
    ]

    # z2m-b: knee exists but no traffic in the window; censored → lower bound;
    # its environment predates the current firmware → stale.
    b = view["instances"]["z2m-b"]
    assert b["knee"]["kind"] == "lower_bound"
    assert b["knee"]["stale_environment"] is True
    assert b["denominators"]["pipeline"] is None
    assert b["rates"] is None
    assert b["headroom"] is None


def test_spread_knee_takes_the_headline_and_ncp_denominator(tmp_path):
    db = Database(tmp_path)
    seed(db, spread=True)
    view = headroom.summarize(db, 3600, INSTANCES_INFO, clock=lambda: 2000.0)
    a = view["instances"]["z2m-a"]
    # The spread measurement becomes the headline knee and a measured
    # denominator 2; the single-target result keeps denominator 3 (per device).
    assert a["knee"]["eps"] == 55.0
    assert a["knee"]["mode"] == "spread"
    assert a["knee"]["kind"] == "mesh_knee"
    assert a["denominators"]["ncp_knee"] == {"eps": 55.0, "provenance": "measured"}
    assert a["denominators"]["pipeline"] == {"eps": 16.0, "provenance": "measured"}
    assert a["headroom"]["knee_utilization_pct"] == round(4.0 / 55.0 * 100.0, 1)


def test_scatter_aggregates_to_minutes_on_wide_windows(tmp_path):
    db = Database(tmp_path)
    seed(db)
    view = headroom.summarize(db, 86400, INSTANCES_INFO, clock=lambda: 2000.0)
    scatter = view["instances"]["z2m-a"]["scatter"]
    # ts 1000+1010 share minute 16 (averaged load, worst p95); the ts=1020
    # window is a benchmark window and never reaches the scatter.
    assert scatter == [{"eps": 3.0, "p95_ms": 60.0}]
