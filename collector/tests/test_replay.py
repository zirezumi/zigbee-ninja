"""Controlled replay: recorded and recomposed burst shapes reproduced with
benign reads on the calibration rails (V2_PROPOSAL.md §V2-5 detector 5c).

Reuses the fake-time/fake-mesh calibration harness; fixtures are sanitized.
"""

import asyncio

import pytest

from tests.test_calibration import INSTANCE, Harness
from zigbee_ninja.capacity import headroom
from zigbee_ninja.store.events import RawEventLog


def replay_harness(tmp_path, commands=None, **kw):
    """Harness plus a raw event store seeded with a recorded burst. The
    events land well before the harness clock (1000.0), so replay windows
    reference them as history."""
    harness = None

    def clock():
        return harness.now if harness is not None else 1000.0

    events = RawEventLog(tmp_path, clock=clock)
    harness = Harness(tmp_path, events_log=events, **kw)
    for ts, target in commands or []:
        events.record(ts, "mqtt", INSTANCE, "command", "in", f"{target}/set", 10)
    events.flush()
    harness.events = events
    return harness


def burst(start=900.0, count=10, spacing=0.15):
    return [(start + i * spacing, f"light_{i % 3}") for i in range(count)]


async def run_replay(harness, source):
    preview = harness.manager.preview_replay(INSTANCE, source)
    await harness.manager.start_replay(INSTANCE, preview["authorization"])
    assert harness.manager._task is not None
    try:
        await harness.manager._task
    except asyncio.CancelledError:
        pass
    return harness.manager.history()[0]


# -- previews -------------------------------------------------------------------


def test_preview_builds_the_recorded_shape(tmp_path):
    harness = replay_harness(tmp_path, commands=burst())
    plan = harness.manager.preview_replay(
        INSTANCE, {"kind": "window", "start": 899.0, "end": 903.0}
    )
    assert plan["mode"] == "replay"
    assert plan["total_reads"] == 10
    step = plan["steps"][0]
    assert step["offsets"][0] == 0.0
    assert step["offsets"][-1] == pytest.approx(1.35, abs=0.01)
    assert plan["replay"]["variant"] == "as_recorded"
    assert plan["replay"]["requested_peak_1s_eps"] == 7.0
    assert plan["replay"]["source"]["commands_recorded"] == 10
    assert "no capacity limit is recorded" in plan["traffic"]
    assert "note" in plan["stop_rules"]
    assert plan["authorization"]


def test_preview_validation(tmp_path):
    harness = replay_harness(tmp_path, commands=burst(count=3))
    with pytest.raises(ValueError, match="at least"):
        harness.manager.preview_replay(
            INSTANCE, {"kind": "window", "start": 899.0, "end": 903.0}
        )
    with pytest.raises(ValueError, match="at most"):
        harness.manager.preview_replay(
            INSTANCE, {"kind": "window", "start": 800.0, "end": 900.0}
        )
    with pytest.raises(ValueError, match="kind"):
        harness.manager.preview_replay(INSTANCE, {"kind": "vibes"})
    with pytest.raises(ValueError, match="variant"):
        harness.manager.preview_replay(
            INSTANCE,
            {"kind": "window", "start": 899.0, "end": 903.0, "variant": "faster"},
        )


def test_preview_refuses_windows_overlapping_benchmarks(tmp_path):
    harness = replay_harness(tmp_path, commands=burst())
    conn = harness.db.connect()
    conn.execute(
        "INSERT INTO calibrations (instance, target, started_at, finished_at, "
        "status, knee_eps, detail) VALUES (?, 't', 898.0, 901.0, 'completed', 1.0, '{}')",
        (INSTANCE,),
    )
    conn.commit()
    with pytest.raises(ValueError, match="overlaps a benchmark"):
        harness.manager.preview_replay(
            INSTANCE, {"kind": "window", "start": 899.0, "end": 903.0}
        )


# -- runs -----------------------------------------------------------------------


def test_replay_reproduces_schedule_and_records_no_knee(tmp_path):
    harness = replay_harness(tmp_path, commands=burst())
    record = asyncio.run(
        run_replay(harness, {"kind": "window", "start": 899.0, "end": 903.0})
    )
    assert record["status"] == "completed"
    assert record["knee_eps"] is None
    assert record["knee"] is None
    assert record["mode"] == "replay"
    replay = record["replay"]
    assert replay["achieved"]["sent"] == 10
    assert replay["achieved"]["timeouts"] == 0
    assert replay["shape_reproduced"] is True
    # The achieved sends land on the requested schedule under fake time.
    sent = record["steps"][0]["sent_offsets"]
    assert len(sent) == 10
    assert sent[-1] == pytest.approx(1.35, abs=0.05)
    # Reads spread across the eligible-router roster.
    read_topics = {topic for topic, _payload in harness.published}
    assert len(read_topics) >= 2
    # No knee anywhere: the capacity tables never see replay rows.
    assert headroom.latest_knees(harness.db) == {}


def test_compressed_variant_sends_everything_under_backpressure(tmp_path):
    harness = replay_harness(
        tmp_path, commands=burst(count=30, spacing=0.2), service_time=0.4
    )
    record = asyncio.run(
        run_replay(
            harness,
            {"kind": "window", "start": 899.0, "end": 906.0, "variant": "compressed"},
        )
    )
    assert record["status"] == "completed"
    replay = record["replay"]
    assert replay["variant"] == "compressed"
    assert replay["achieved"]["sent"] == 30
    # Backpressure stretched the burst: the slip is the recorded story.
    assert replay["achieved"]["first_to_last_s"] > 0.0
    assert replay["shape_reproduced"] is True
    assert record["knee_eps"] is None


def test_pacer_stall_refuses_the_shape(tmp_path):
    harness = replay_harness(tmp_path, commands=burst())
    stalled = {"done": False}

    def stall_once(now):
        if not stalled["done"] and now > 1000.5:
            stalled["done"] = True
            harness.now += 1.2  # a stall-sized oversleep on a shape-driven wait

    harness.on_advance = stall_once
    record = asyncio.run(
        run_replay(harness, {"kind": "window", "start": 899.0, "end": 903.0})
    )
    assert record["status"] == "completed"
    assert record["verdict"] is not None
    assert "not trustworthy" in record["verdict"]
    assert record["replay"]["shape_reproduced"] is False


def test_replay_token_and_ramp_token_do_not_cross(tmp_path):
    from zigbee_ninja.calibration.benchmark import CalibrationRejected

    harness = replay_harness(tmp_path, commands=burst())
    replay_plan = harness.manager.preview_replay(
        INSTANCE, {"kind": "window", "start": 899.0, "end": 903.0}
    )
    ramp_plan = harness.manager.preview(INSTANCE, "plug-a")
    with pytest.raises(CalibrationRejected, match="replay"):
        asyncio.run(
            harness.manager.start(INSTANCE, "plug-a", replay_plan["authorization"])
        )
    with pytest.raises(CalibrationRejected, match="ramp"):
        asyncio.run(
            harness.manager.start_replay(INSTANCE, ramp_plan["authorization"])
        )


def test_replay_requires_the_event_store(tmp_path):
    harness = Harness(tmp_path)
    with pytest.raises(ValueError, match="raw event store"):
        harness.manager.preview_replay(
            INSTANCE, {"kind": "window", "start": 899.0, "end": 903.0}
        )


# -- scenario source -------------------------------------------------------------


def test_scenario_source_replays_the_recomposed_stream(tmp_path):
    from tests.test_calibration import DEVICES, DEVICES_TWO, GROUPS, INSTANCE_TWO
    from tests.test_scenario import FakeRegistry

    registry = FakeRegistry(
        instances=[
            {"base_topic": INSTANCE, "channel": 15, "version": "2.12.1"},
            {"base_topic": INSTANCE_TWO, "channel": 25, "version": "2.12.1"},
            {"base_topic": "z2m-empty", "channel": 11, "version": "2.12.1"},
        ],
        devices={INSTANCE: list(DEVICES), INSTANCE_TWO: list(DEVICES_TWO)},
        groups={INSTANCE: list(GROUPS), INSTANCE_TWO: []},
    )
    # The destination's own commands plus the moved device's burst arriving
    # from the other coordinator: the recomposed stream is their merge.
    commands = [(900.0 + i * 0.5, "light_a") for i in range(4)]
    harness = replay_harness(
        tmp_path,
        commands=commands,
        multi=True,
        registry=registry,
        pricing=lambda base: (None, None),
    )
    for i in range(6):
        harness.events.record(
            900.2 + i * 0.1, "mqtt", INSTANCE_TWO, "command", "in", "relay-x/set", 10
        )
    harness.events.flush()

    move = {
        "kind": "device",
        "subject": "relay-x",
        "from_instance": INSTANCE_TWO,
        "to_instance": INSTANCE,
    }
    plan = harness.manager.preview_replay(
        INSTANCE, {"kind": "scenario", "moves": [move]}
    )
    replay = plan["replay"]
    assert replay["source"]["kind"] == "scenario"
    # Merge of 4 own commands and 6 re-homed arrivals inside the pad window.
    assert replay["source"]["commands_recomposed"] == 10
    assert replay["predicted"]["peak_1s_eps"] >= 7.0
    assert replay["predicted"]["verdict"] == "no_limits"
    assert plan["total_reads"] == 10

    # Replaying a coordinator the moves never touch is refused.
    with pytest.raises(ValueError, match="do not touch"):
        harness.manager.preview_replay(
            INSTANCE_TWO,
            {
                "kind": "scenario",
                "moves": [
                    {
                        "kind": "device",
                        "subject": "plug-a",
                        "from_instance": INSTANCE,
                        "to_instance": "z2m-empty",
                    }
                ],
            },
        )


# -- API paths -------------------------------------------------------------------

SETUP = {"username": "admin", "password": "correct-horse"}


def test_replay_api_validation(client):
    client.post("/api/setup", json=SETUP)
    bad_kind = client.post(
        "/api/calibration/replay/preview",
        json={"instance": "z2m-x", "source": {"kind": "vibes"}},
    )
    assert bad_kind.status_code == 400
    bogus_run = client.post(
        "/api/calibration/replay/run",
        json={"instance": "z2m-x", "authorization": "nope"},
    )
    assert bogus_run.status_code == 409
