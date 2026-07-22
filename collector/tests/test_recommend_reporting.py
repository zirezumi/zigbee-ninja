import json

from zigbee_ninja.capacity import ledger
from zigbee_ninja.recommend import reporting
from zigbee_ninja.recommend.context import DetectorContext
from zigbee_ninja.store.db import Database

NOW = 5_000_000.0
LOOKBACK = 86400.0


def _device(name, vendor="Acme", model="Sensor-1"):
    # Mirrors the registry's flattened device shape (registry._on_devices),
    # which is what ctx.devices() actually serves: vendor/model live on the
    # device dict, not under a nested Z2M-style `definition`.
    return {
        "friendly_name": name,
        "vendor": vendor,
        "model": model,
    }


def _context(tmp_path, devices, utilization=None):
    """utilization: {instance: {...}} pressure, used for significance banding."""
    db = Database(tmp_path)
    ctx = DetectorContext(
        conn=db.connect(),
        now=NOW,
        lookback_seconds=LOOKBACK,
        instances=["z2m-1"],
        instance_info={},
        knees={},
        is_group=lambda base, target: False,
        group_members=lambda base, target: [],
        groups=lambda base: [],
        devices=lambda base: devices if base == "z2m-1" else [],
        router_count_for=lambda base: 20,
        pricing=lambda base: (None, None),
        utilization=utilization or {},
    )
    ctx.conn.execute(
        "INSERT INTO settings (key, value) VALUES ('ledger_since', ?)",
        (json.dumps(NOW - LOOKBACK),),
    )
    ctx.conn.commit()
    return ctx


def _spend(ctx, device, us_per_s, publishes=1000):
    day = ledger.utc_day(NOW)
    ctx.conn.execute(
        "INSERT INTO ledger_device_daily (instance, day, device, publishes, "
        "autonomous_us, provenance) VALUES ('z2m-1', ?, ?, ?, ?, 'modeled')",
        (day, device, publishes, us_per_s * LOOKBACK),
    )
    ctx.conn.commit()


def test_peer_outlier_is_high_confidence(tmp_path):
    devices = [_device(f"plug_{i}") for i in range(4)]
    ctx = _context(tmp_path, devices)
    _spend(ctx, "plug_0", 900.0, publishes=20000)
    for i in range(1, 4):
        _spend(ctx, f"plug_{i}", 60.0, publishes=1200)

    findings = reporting.detect(ctx)
    outliers = [f for f in findings if f.subject == "plug_0"]
    assert len(outliers) == 1
    finding = outliers[0]
    assert finding.confidence == "high"
    assert finding.action["kind"] == "reconfigure_reporting"
    assert finding.evidence[0]["kind"] == "ledger"
    assert finding.evidence[0]["compared_to"] == "peers"
    assert finding.evidence[0]["peers"] == 3
    # Saving replays the recorded volume against the peer median.
    assert finding.saving["us_per_s"] == round(900.0 - 60.0, 1)
    assert "peers median" in finding.saving["basis"] or "peer" in finding.saving["basis"]


def test_presence_device_downgrades_to_low_confidence(tmp_path):
    devices = [_device("laundry_presence_dimmer", vendor="Inovelli", model="VZM32-SN")]
    devices += [_device(f"bulb_{i}", model="Bulb-2") for i in range(6)]
    ctx = _context(tmp_path, devices)
    _spend(ctx, "laundry_presence_dimmer", 950.0, publishes=28000)
    for i in range(6):
        _spend(ctx, f"bulb_{i}", 20.0, publishes=300)

    findings = reporting.detect(ctx)
    (finding,) = [f for f in findings if f.subject == "laundry_presence_dimmer"]
    assert finding.confidence == "low"
    assert "presence sensing" in finding.finding


def test_unknown_hardware_never_forms_a_peer_group(tmp_path):
    # Devices whose vendor/model the registry could not resolve must not be
    # lumped into one giant "same hardware" cohort; the loud one falls back
    # to the fleet comparison (medium confidence), never a peers claim.
    devices = [_device(f"mystery_{i}", vendor=None, model=None) for i in range(8)]
    ctx = _context(tmp_path, devices)
    _spend(ctx, "mystery_0", 600.0, publishes=15000)
    for i in range(1, 8):
        _spend(ctx, f"mystery_{i}", 15.0, publishes=200)

    findings = reporting.detect(ctx)
    (finding,) = [f for f in findings if f.subject == "mystery_0"]
    assert finding.evidence[0]["compared_to"] == "fleet"
    assert finding.confidence == "medium"


def test_fleet_branch_uses_model_for_presence_hint(tmp_path):
    # A presence-hardware device that lands in the FLEET branch (too few
    # same-model peers) still gets the presence downgrade from its model.
    devices = [_device("closet_sensor", vendor="Aqara", model="RTCZCGQ11LM mmWave")]
    devices += [_device(f"bulb_{i}", model="Bulb-2") for i in range(6)]
    ctx = _context(tmp_path, devices)
    _spend(ctx, "closet_sensor", 800.0, publishes=24000)
    for i in range(6):
        _spend(ctx, f"bulb_{i}", 20.0, publishes=300)

    findings = reporting.detect(ctx)
    (finding,) = [f for f in findings if f.subject == "closet_sensor"]
    assert finding.evidence[0]["compared_to"] == "fleet"
    assert finding.confidence == "low"
    assert "presence sensing" in finding.finding


def test_quiet_fleet_yields_nothing(tmp_path):
    devices = [_device(f"plug_{i}") for i in range(4)]
    ctx = _context(tmp_path, devices)
    for i in range(4):
        _spend(ctx, f"plug_{i}", 20.0)
    assert reporting.detect(ctx) == []


def test_loud_device_among_silent_peers_still_flags(tmp_path):
    devices = [_device(f"plug_{i}") for i in range(4)]
    ctx = _context(tmp_path, devices)
    _spend(ctx, "plug_0", 400.0, publishes=9000)  # peers exist but never report

    findings = reporting.detect(ctx)
    (finding,) = [f for f in findings if f.subject == "plug_0"]
    assert finding.evidence[0]["peer_median_us_per_s"] == 0.0
    assert finding.saving["us_per_s"] == 400.0


def test_reporting_cost_is_staleness_not_load(tmp_path):
    # Slowing a device's reports removes traffic: nothing on the mesh rises.
    # What it spends is freshness, and the cost has to quote that in the
    # currency the owner feels rather than claiming the change is free.
    devices = [_device(f"plug_{i}") for i in range(4)]
    ctx = _context(tmp_path, devices)
    _spend(ctx, "plug_0", 900.0, publishes=20000)
    for i in range(1, 4):
        _spend(ctx, f"plug_{i}", 60.0, publishes=1200)

    (finding,) = [f for f in reporting.detect(ctx) if f.subject == "plug_0"]
    cost = finding.cost
    assert cost["denominator"] == reporting.STALENESS
    assert cost["raises_load"] is False
    # 20000 reports a day is one every 4.3 s; reporting like its 60 µs/s
    # peers means one every 64.8 s, so a state change lands a minute later.
    assert cost["reports_per_day_now"] == 20000
    assert cost["mean_interval_s_now"] == 4.3
    assert cost["mean_interval_s_at_reference"] == 64.8
    assert cost["added_delay_s"] == 60.5
    assert "later" in cost["note"]


def test_silent_peers_leave_the_reference_interval_unquotable(tmp_path):
    # Peers that never report give a zero reference rate: there is no interval
    # to move toward, and the cost says so rather than dividing by zero or
    # quoting a fabricated one.
    devices = [_device(f"plug_{i}") for i in range(4)]
    ctx = _context(tmp_path, devices)
    _spend(ctx, "plug_0", 400.0, publishes=9000)

    (finding,) = [f for f in reporting.detect(ctx) if f.subject == "plug_0"]
    assert finding.cost["mean_interval_s_at_reference"] is None
    assert finding.cost["added_delay_s"] is None
    assert finding.cost["raises_load"] is False
    assert "report essentially never" in finding.cost["note"]


def test_presence_hardware_carries_the_delay_warning_into_the_cost(tmp_path):
    devices = [_device("closet_sensor", vendor="Aqara", model="RTCZCGQ11LM mmWave")]
    devices += [_device(f"bulb_{i}", model="Bulb-2") for i in range(6)]
    ctx = _context(tmp_path, devices)
    _spend(ctx, "closet_sensor", 800.0, publishes=24000)
    for i in range(6):
        _spend(ctx, f"bulb_{i}", 20.0, publishes=300)

    (finding,) = [f for f in reporting.detect(ctx) if f.subject == "closet_sensor"]
    assert finding.cost["presence_hardware"] is True
    assert "most likely to be felt" in finding.cost["note"]


def test_idle_channel_bands_the_reporting_finding_low(tmp_path):
    # The largest recoverable cost on many installations is still worth
    # nothing today if the channel it would free is barely used.
    devices = [_device(f"plug_{i}") for i in range(4)]
    ctx = _context(
        tmp_path, devices, utilization={"z2m-1": {"channel_budget_pct": 0.9}}
    )
    _spend(ctx, "plug_0", 900.0, publishes=20000)
    for i in range(1, 4):
        _spend(ctx, f"plug_{i}", 60.0, publishes=1200)

    (finding,) = [f for f in reporting.detect(ctx) if f.subject == "plug_0"]
    assert finding.significance["band"] == "low"
    assert finding.significance["denominator"] == "channel airtime"
    assert "not under pressure" in finding.significance["rationale"]


def test_busy_channel_bands_the_reporting_finding_by_relief(tmp_path):
    devices = [_device(f"plug_{i}") for i in range(4)]
    ctx = _context(
        tmp_path, devices, utilization={"z2m-1": {"channel_budget_pct": 70.0}}
    )
    _spend(ctx, "plug_0", 900.0, publishes=20000)
    for i in range(1, 4):
        _spend(ctx, f"plug_{i}", 60.0, publishes=1200)

    (finding,) = [f for f in reporting.detect(ctx) if f.subject == "plug_0"]
    assert finding.significance["band"] == "moderate"
    assert finding.significance["utilization_pct"] == 70.0
    assert finding.significance["relief_pct"] < 10.0


def test_no_recording_mark_means_no_findings(tmp_path):
    devices = [_device("plug_0")]
    db = Database(tmp_path)
    ctx = DetectorContext(
        conn=db.connect(),
        now=NOW,
        lookback_seconds=LOOKBACK,
        instances=["z2m-1"],
        instance_info={},
        knees={},
        is_group=lambda base, target: False,
        group_members=lambda base, target: [],
        groups=lambda base: [],
        devices=lambda base: devices,
        router_count_for=lambda base: 20,
        pricing=lambda base: (None, None),
    )
    assert reporting.detect(ctx) == []
