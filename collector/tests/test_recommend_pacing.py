import json
from types import SimpleNamespace

from zigbee_ninja.recommend import pacing
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.store.db import Database

NOW = 2_000_000.0
ENVIRONMENT = {"z2m_version": "2.12.1", "coordinator_revision": "8.0.2"}


def _context(tmp_path, knees=None, info=None):
    db = Database(tmp_path)
    return DetectorContext(
        conn=db.connect(),
        now=NOW,
        lookback_seconds=86400.0,
        instances=list(knees or {}),
        instance_info=info
        or {
            instance: {
                "base_topic": instance,
                "version": ENVIRONMENT["z2m_version"],
                "coordinator_revision": ENVIRONMENT["coordinator_revision"],
            }
            for instance in (knees or {})
        },
        knees=knees or {},
        is_group=lambda base, target: False,
        group_members=lambda base, target: [],
        groups=lambda base: [],
        devices=lambda base: [],
        router_count_for=lambda base: 20,
        pricing=lambda base: (None, None),
    )


def _knee(eps, mode, environment=None):
    return {
        "eps": eps,
        "kind": "pipeline_ceiling",
        "mode": mode,
        "breach": "saturated",
        "censored": False,
        "rtt_source": "wire",
        "target": "some_router",
        "measured_at": NOW - 86400,
        "environment": environment or dict(ENVIRONMENT),
    }


def _insert_chains(ctx, instance, commands):
    """commands: iterable of (opened_at, target, client)."""
    ctx.conn.executemany(
        "INSERT INTO chains (instance, target, verb, opened_at, client, payload_size, "
        "echo_count, first_echo_ms, redundant) VALUES (?, ?, 'set', ?, ?, 10, 1, 100, 0)",
        [(instance, target, opened_at, client) for opened_at, target, client in commands],
    )
    ctx.conn.commit()


def _insert_calibration(ctx, instance, mode, steps):
    """steps: list of (achieved_eps, wire_p95_ms, echo_p95_ms)."""
    detail = {
        "plan": {"mode": mode, "target": "router"},
        "steps": [
            {"achieved_eps": eps, "wire_p95_ms": wire, "echo_p95_ms": echo}
            for eps, wire, echo in steps
        ],
        "knee": {"breach": "saturated"},
        "environment": dict(ENVIRONMENT),
    }
    ctx.conn.execute(
        "INSERT INTO calibrations (instance, target, started_at, finished_at, status, "
        "knee_eps, detail) VALUES (?, 'router', ?, ?, 'completed', ?, ?)",
        (instance, NOW - 90000, NOW - 89000, steps[-1][0], json.dumps(detail)),
    )
    ctx.conn.commit()


def _burst(start, n, spacing_s, client="automation: Lights", target_prefix="light"):
    return [
        (start + i * spacing_s, f"{target_prefix}_{i}", client) for i in range(n)
    ]


def test_burst_near_capacity_limit_yields_pace_finding(tmp_path):
    knees = {"z2m-1": {"spread": _knee(30.0, "spread"), "single": _knee(16.0, "single")}}
    ctx = _context(tmp_path, knees)
    # Three recurring 30-command bursts at ~30/s (spacing 33 ms), 0.8x30=24 breached.
    for occurrence in range(3):
        _insert_chains(
            ctx, "z2m-1", _burst(NOW - 3600 - occurrence * 600, 30, 0.033)
        )

    findings = pacing.detect(ctx)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.detector == "pacing"
    assert finding.subject == "automation: Lights"
    assert finding.action["kind"] == "pace"
    # 30 commands paced to 15/s (half of 30) need at least 2 s.
    assert finding.action["stagger_ms"] == 2000
    assert finding.confidence == "high"
    assert finding.saving["us_per_s"] == 0.0
    windows = [entry for entry in finding.evidence if entry["kind"] == "window"]
    assert len(windows) == 3
    assert any(entry["kind"] == "capacity_limit" for entry in finding.evidence)
    assert finding.fingerprint["bursts"] == 3


def test_paced_traffic_stays_quiet(tmp_path):
    knees = {"z2m-1": {"spread": _knee(30.0, "spread"), "single": _knee(16.0, "single")}}
    ctx = _context(tmp_path, knees)
    # 20 commands spread over 3 s (~7/s peak): the PASCL stagger pattern.
    _insert_chains(ctx, "z2m-1", _burst(NOW - 3600, 20, 0.15))
    assert pacing.detect(ctx) == []


def test_single_device_overload_flags_without_aggregate_pressure(tmp_path):
    knees = {"z2m-1": {"spread": _knee(30.0, "spread"), "single": _knee(16.0, "single")}}
    ctx = _context(tmp_path, knees)
    # 20 commands to ONE device in one second: over its 16/s ceiling, but the
    # aggregate peak (20/s) stays under 0.8 x 30.
    _insert_chains(
        ctx,
        "z2m-1",
        [(NOW - 3600 + i * 0.05, "hall_dimmer", "automation: Dial") for i in range(20)],
    )
    findings = pacing.detect(ctx)
    assert len(findings) == 1
    assert "hall_dimmer" in findings[0].finding
    assert "queue" in findings[0].finding


def test_no_measured_knees_means_no_findings(tmp_path):
    ctx = _context(tmp_path, {"z2m-1": {}})
    _insert_chains(ctx, "z2m-1", _burst(NOW - 3600, 40, 0.02))
    assert pacing.detect(ctx) == []


def test_p95_prediction_interpolates_measured_curve(tmp_path):
    knees = {"z2m-1": {"spread": _knee(30.0, "spread")}}
    ctx = _context(tmp_path, knees)
    _insert_calibration(
        ctx,
        "z2m-1",
        "spread",
        [(8.0, 40.0, None), (16.0, 41.0, None), (32.0, 124.0, None), (41.0, 164.0, None)],
    )
    # Peak ~28/s sits inside the measured range; paced 15/s likewise.
    _insert_chains(ctx, "z2m-1", _burst(NOW - 3600, 28, 0.035))

    (finding,) = pacing.detect(ctx)
    assert finding.saving["p95_ms"] > 0
    assert finding.saving["provenance"] == "measured"
    assert "curve" in finding.saving["basis"]


def test_p95_beyond_measured_range_is_modeled_floor(tmp_path):
    knees = {"z2m-1": {"spread": _knee(30.0, "spread")}}
    ctx = _context(tmp_path, knees)
    _insert_calibration(
        ctx, "z2m-1", "spread", [(8.0, 40.0, None), (16.0, 41.0, None), (32.0, 124.0, None)]
    )
    # 60 commands in ~1 s: far beyond the last measured point (32 eps).
    _insert_chains(ctx, "z2m-1", _burst(NOW - 3600, 60, 0.016))

    (finding,) = pacing.detect(ctx)
    assert finding.saving["provenance"] == "modeled"
    assert "beyond" in finding.saving["basis"]


def test_stale_calibration_environment_downgrades_confidence(tmp_path):
    old_environment = {"z2m_version": "2.10.1", "coordinator_revision": "8.0.2"}
    knees = {
        "z2m-1": {"spread": _knee(30.0, "spread", environment=old_environment)}
    }
    ctx = _context(tmp_path, knees)
    for occurrence in range(3):
        _insert_chains(ctx, "z2m-1", _burst(NOW - 3600 - occurrence * 600, 30, 0.033))

    (finding,) = pacing.detect(ctx)
    assert finding.confidence == "medium"
    assert "recalibrat" in finding.finding


def test_mixed_commanders_group_under_multiple_label(tmp_path):
    knees = {"z2m-1": {"spread": _knee(30.0, "spread")}}
    ctx = _context(tmp_path, knees)
    commands = []
    for i in range(30):
        client = f"automation: Room {i % 3}"  # three even shares, no majority
        commands.append((NOW - 3600 + i * 0.03, f"light_{i}", client))
    _insert_chains(ctx, "z2m-1", commands)

    (finding,) = pacing.detect(ctx)
    assert finding.subject == pacing.MULTIPLE_COMMANDERS
    windows = [entry for entry in finding.evidence if entry["kind"] == "window"]
    assert len(windows[0]["commanders"]) == 3


def test_curve_interpolation_helper():
    points = [(8.0, 40.0), (16.0, 41.0), (32.0, 124.0)]
    value, beyond = pacing.p95_at(points, 24.0)
    assert 41.0 < value < 124.0
    assert not beyond
    value, beyond = pacing.p95_at(points, 50.0)
    assert value == 124.0
    assert beyond
    assert pacing.p95_at([], 10.0) is None


def test_runner_wires_pacing_into_the_roster(tmp_path):
    from zigbee_ninja.recommend.runner import RecommendationEngine

    engine = RecommendationEngine(
        Database(tmp_path),
        registry=SimpleNamespace(snapshot=lambda: []),
        pricing=lambda instance: (None, None),
    )
    assert any(module.NAME == "pacing" for module in engine._detectors)
