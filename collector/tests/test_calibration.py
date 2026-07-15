import asyncio
import json

import pytest

from zigbee_ninja.calibration import benchmark
from zigbee_ninja.calibration.benchmark import CalibrationManager, CalibrationRejected
from zigbee_ninja.store.db import Database

INSTANCE = "z2m-test"

# Sanitized fixtures only — no real IEEE addresses.
DEVICES = [
    {
        "ieee_address": "0xa1",
        "friendly_name": "plug-a",
        "type": "Router",
        "power_source": "Mains (single phase)",
        "vendor": "ExampleCo",
        "model": "PLUG-1",
        "network_address": 10,
        "get_attribute": "state",
        "published_measurements": ["power"],
        "binding_count": 1,
    },
    {
        "ieee_address": "0xb2",
        "friendly_name": "bulb-b",
        "type": "Router",
        "power_source": "Mains (single phase)",
        "vendor": "ExampleCo",
        "model": "BULB-1",
        "network_address": 11,
        "get_attribute": "state",
        "published_measurements": [],
        "binding_count": 0,
    },
    {
        "ieee_address": "0xc3",
        "friendly_name": "sensor-c",
        "type": "EndDevice",
        "power_source": "Battery",
        "vendor": "ExampleCo",
        "model": "SENSE-1",
        "network_address": 12,
        "get_attribute": None,
        "published_measurements": [],
        "binding_count": 0,
    },
    {
        "ieee_address": "0xd4",
        "friendly_name": "mute-d",
        "type": "Router",
        "power_source": "Mains (single phase)",
        "vendor": "ExampleCo",
        "model": "RELAY-1",
        "network_address": 13,
        "get_attribute": None,
        "published_measurements": [],
        "binding_count": 0,
    },
]

GROUPS = [{"id": 1, "friendly_name": "g1", "member_count": 1, "member_ieee": ["0xa1"]}]

INSTANCES = [
    {
        "base_topic": INSTANCE,
        "version": "2.10.1",
        "channel": 15,
        "coordinator_type": "EmberZNet",
        "coordinator_revision": "8.1.0",
    },
]

TOPOLOGY = {
    "pulled_at": 900.0,
    "unresponsive_nodes": [],
    "raw": {
        "nodes": [],
        "links": [
            {"sourceIeeeAddr": "0xa1", "targetIeeeAddr": "0x00", "lqi": 200},
            {"sourceIeeeAddr": "0xb2", "targetIeeeAddr": "0x00", "lqi": 120},
        ],
    },
}


class Harness:
    """Fake time + fake mesh: sleep advances the clock and services reads.

    `service_time` is the simulated read round-trip; `answer` decides at send
    time whether a read will ever be answered (given the active step's rate).
    """

    def __init__(
        self,
        tmp_path,
        service_time=0.05,
        answer=None,
        wire_covers=False,
        wire_steps=None,
    ):
        self.now = 1000.0
        self.published: list[tuple[str, str]] = []
        self.pending: list[float] = []
        self.dropped = 0
        self.service_time = service_time
        self.answer = answer or (lambda rate, index: True)
        self.on_advance = None
        self.wire_steps = list(wire_steps or [])
        self._wire_calls = 0
        self.db = Database(tmp_path)
        self.manager = CalibrationManager(
            self.db,
            publisher=self._publish,
            devices=lambda base: DEVICES if base == INSTANCE else [],
            groups=lambda base: GROUPS if base == INSTANCE else [],
            instances=lambda: INSTANCES,
            topology_latest=lambda base: TOPOLOGY if base == INSTANCE else {},
            wire_covers=(lambda _base: wire_covers),
            wire_latency_mark=lambda _base: 0.0,
            wire_latency_since=self._wire_since,
            wire_delivery_totals=lambda _base: (0, 0),
            clock=lambda: self.now,
            sleep=self._sleep,
        )

    async def _publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))
        status = self.manager.status()["active"]
        rate = status["current"]["rate_eps"] if status and status["current"] else 0.0
        index = len(self.published) - 1
        if self.answer(rate, index):
            self.pending.append(self.now)
        else:
            self.dropped += 1

    async def _sleep(self, seconds: float) -> None:
        self.now += seconds
        while self.pending and self.now - self.pending[0] >= self.service_time:
            self.pending.pop(0)
            self.manager.on_state(INSTANCE, "plug-a")
        if self.on_advance is not None:
            self.on_advance(self.now)

    def _wire_since(self, _base: str, _mark: float) -> list[float]:
        if not self.wire_steps:
            return []
        index = min(self._wire_calls, len(self.wire_steps) - 1)
        self._wire_calls += 1
        return list(self.wire_steps[index])

    async def run_to_completion(self, target="plug-a"):
        preview = self.manager.preview(INSTANCE, target)
        await self.manager.start(INSTANCE, target, preview["authorization"])
        assert self.manager._task is not None
        try:
            await self.manager._task
        except asyncio.CancelledError:
            pass
        return self.manager.history()[0]


# -- candidates & preview -------------------------------------------------------


def test_candidates_rank_and_eligibility(tmp_path):
    harness = Harness(tmp_path)
    view = harness.manager.candidates(INSTANCE)
    assert view["topology_pulled_at"] == 900.0
    rows = view["candidates"]
    names = [row["friendly_name"] for row in rows]
    # Routers only; the battery end-device is not a candidate at all.
    assert "sensor-c" not in names
    # plug-a: lqi 200 - 10 (one binding) - 5 (one group) = 185 beats bulb-b's 120.
    assert names[:2] == ["plug-a", "bulb-b"]
    assert rows[0]["score"] == 185
    assert rows[1]["score"] == 120
    # mute-d exposes nothing gettable → ineligible, sorted last, with a reason.
    mute = next(row for row in rows if row["friendly_name"] == "mute-d")
    assert mute["eligible"] is False
    assert "no gettable attribute" in mute["reasons"]


def test_preview_plan_shape_and_warnings(tmp_path):
    harness = Harness(tmp_path)
    plan = harness.manager.preview(INSTANCE, "plug-a")
    assert plan["topic"] == f"{INSTANCE}/plug-a/get"
    assert json.loads(plan["payload"]) == {"state": ""}
    assert plan["total_reads"] == sum(step["reads"] for step in plan["steps"])
    assert plan["rtt_source"] == "echo"  # wire_covers=False in this harness
    assert any("wire tap" in warning for warning in plan["warnings"])
    assert any("power" in warning for warning in plan["warnings"])
    assert plan["environment"]["z2m_version"] == "2.10.1"
    assert plan["authorization"]

    with pytest.raises(ValueError, match="Unknown device"):
        harness.manager.preview(INSTANCE, "nope")
    with pytest.raises(ValueError, match="not a router"):
        harness.manager.preview(INSTANCE, "sensor-c")
    with pytest.raises(ValueError, match="no gettable attribute"):
        harness.manager.preview(INSTANCE, "mute-d")


def test_run_requires_fresh_matching_single_use_authorization(tmp_path):
    harness = Harness(tmp_path)

    async def scenario():
        with pytest.raises(CalibrationRejected, match="Unknown or already-used"):
            await harness.manager.start(INSTANCE, "plug-a", "bogus")
        assert harness.published == []

        # Token minted for one target must not start another.
        token_a = harness.manager.preview(INSTANCE, "plug-a")["authorization"]
        with pytest.raises(CalibrationRejected, match="does not match"):
            await harness.manager.start(INSTANCE, "bulb-b", token_a)

        # Expired token.
        token_b = harness.manager.preview(INSTANCE, "plug-a")["authorization"]
        harness.now += benchmark.AUTHORIZATION_TTL_SECONDS + 1
        with pytest.raises(CalibrationRejected, match="expired"):
            await harness.manager.start(INSTANCE, "plug-a", token_b)
        assert harness.published == []

        # A used token is gone: complete a run, cool down, then retry it.
        token_c = harness.manager.preview(INSTANCE, "plug-a")["authorization"]
        await harness.manager.start(INSTANCE, "plug-a", token_c)
        await harness.manager._task
        harness.now += benchmark.COOLDOWN_SECONDS + 1
        with pytest.raises(CalibrationRejected, match="Unknown or already-used"):
            await harness.manager.start(INSTANCE, "plug-a", token_c)

    asyncio.run(scenario())


# -- the ramp ---------------------------------------------------------------------


def test_full_ramp_completes_with_censored_knee(tmp_path):
    harness = Harness(tmp_path)

    async def scenario():
        record = await harness.run_to_completion()
        assert record["status"] == "completed"
        steps = record["steps"]
        assert len(steps) == len(benchmark.RAMP_RATES_EPS)
        for step, rate in zip(steps, benchmark.RAMP_RATES_EPS, strict=True):
            assert step["rate_eps"] == rate
            assert step["breach"] is None
            # Paced sends: rate × 20 s, allowing warmup slack.
            assert step["sent"] >= int(rate * step["duration_s"] * 0.9)
            assert step["timeouts"] == 0
        assert record["knee"]["censored"] is True
        assert record["knee_eps"] == steps[-1]["achieved_eps"]
        assert record["environment"]["z2m_version"] == "2.10.1"
        # Idle again afterwards, with cooldown armed.
        status = harness.manager.status()
        assert status["active"] is None
        assert status["cooldown_until"] > harness.now

    asyncio.run(scenario())


def test_timeout_ratio_finds_knee(tmp_path):
    # Above 8 eps the mesh drops every other read → >10% timeouts at 16 eps.
    harness = Harness(
        tmp_path,
        answer=lambda rate, index: rate <= 8 or index % 2 == 0,
    )

    async def scenario():
        record = await harness.run_to_completion()
        assert record["status"] == "completed"
        breached = [step for step in record["steps"] if step["breach"]]
        assert breached[0]["breach"] == "timeout_ratio"
        assert breached[0]["rate_eps"] == 16.0
        assert record["knee"]["censored"] is False
        assert record["knee"]["breach_rate_eps"] == 16.0
        # Knee = the last clean step (8 eps), by achieved rate.
        assert 6.0 <= record["knee_eps"] <= 8.5

    asyncio.run(scenario())


def test_saturation_stops_ramp_when_driver_cannot_reach_rate(tmp_path):
    # 500 ms service time: the outstanding bound caps throughput near 8 eps,
    # so the 16 eps step can't be driven → "saturated".
    harness = Harness(tmp_path, service_time=0.5)

    async def scenario():
        record = await harness.run_to_completion()
        assert record["status"] == "completed"
        breached = [step for step in record["steps"] if step["breach"]]
        assert breached[0]["breach"] == "saturated"
        assert breached[0]["rate_eps"] == 16.0
        assert breached[0]["deferred"] > 0

    asyncio.run(scenario())


def test_wire_rtt_breach_finds_knee(tmp_path):
    # Wire p95 jumps at the 4th step: 3× the 50 ms baseline and above the
    # 300 ms wire floor → rtt_p95 breach at 8 eps, knee at 4 eps.
    harness = Harness(
        tmp_path,
        wire_covers=True,
        wire_steps=[[48.0, 52.0], [50.0, 55.0], [51.0, 60.0], [390.0, 430.0]],
    )

    async def scenario():
        record = await harness.run_to_completion()
        assert record["status"] == "completed"
        assert record["rtt_source"] == "wire"
        breached = [step for step in record["steps"] if step["breach"]]
        assert breached[0]["breach"] == "rtt_p95"
        assert breached[0]["rate_eps"] == 8.0
        assert breached[0]["rtt_source"] == "wire"
        assert record["knee"]["eps"] == pytest.approx(4.0, abs=0.5)

    asyncio.run(scenario())


# -- watchdog & abort ----------------------------------------------------------------


def test_uninvolved_device_offline_aborts(tmp_path):
    harness = Harness(tmp_path)

    def trip(now):
        if now > 1030 and harness.manager.active:
            harness.manager.on_availability(
                INSTANCE, "other-device/availability", b'{"state": "offline"}'
            )

    harness.on_advance = trip

    async def scenario():
        record = await harness.run_to_completion()
        assert record["status"] == "aborted"
        assert "uninvolved device other-device" in record["abort_reason"]
        assert record["knee"] is None
        # No further reads after the abort took effect.
        sent_at_abort = len(harness.published)
        harness.now += 5
        assert len(harness.published) == sent_at_abort

    asyncio.run(scenario())


def test_bridge_log_error_spike_aborts(tmp_path):
    harness = Harness(tmp_path)

    def trip(now):
        if now > 1025 and harness.manager.active:
            harness.manager.on_bridge_log(
                INSTANCE, b'{"level": "error", "message": "boom"}'
            )

    harness.on_advance = trip

    async def scenario():
        record = await harness.run_to_completion()
        assert record["status"] == "aborted"
        assert "error log lines" in record["abort_reason"]

    asyncio.run(scenario())


def test_stall_aborts_when_nothing_ever_replies(tmp_path):
    harness = Harness(tmp_path, answer=lambda rate, index: False)

    async def scenario():
        record = await harness.run_to_completion()
        assert record["status"] == "aborted"
        assert "no replies at all" in record["abort_reason"]

    asyncio.run(scenario())


def test_manual_abort_and_cooldown(tmp_path):
    harness = Harness(tmp_path)

    def trip(now):
        if now > 1030 and harness.manager.active:
            harness.manager.abort()

    harness.on_advance = trip

    async def scenario():
        record = await harness.run_to_completion()
        assert record["status"] == "aborted"
        assert record["abort_reason"] == "manual abort"

        # Cooldown blocks the next run until it lapses.
        harness.on_advance = None
        token = harness.manager.preview(INSTANCE, "plug-a")["authorization"]
        with pytest.raises(CalibrationRejected, match="Cooling down"):
            await harness.manager.start(INSTANCE, "plug-a", token)
        harness.now += benchmark.COOLDOWN_SECONDS + 1
        token = harness.manager.preview(INSTANCE, "plug-a")["authorization"]
        await harness.manager.start(INSTANCE, "plug-a", token)
        await harness.manager._task
        assert harness.manager.history()[0]["status"] == "completed"

    asyncio.run(scenario())


def test_abort_with_no_active_run_is_rejected(tmp_path):
    harness = Harness(tmp_path)
    with pytest.raises(CalibrationRejected, match="No calibration run"):
        harness.manager.abort()


# -- engine-facing hooks ---------------------------------------------------------------


def test_hooks_only_engage_during_a_run(tmp_path):
    harness = Harness(tmp_path)
    manager = harness.manager
    assert manager.active is False
    assert manager.owns_command(INSTANCE, "plug-a", "get") is False
    assert manager.on_state(INSTANCE, "plug-a") is False

    async def scenario():
        seen = {}

        def probe(now):
            if manager.active and "owns" not in seen:
                seen["owns"] = manager.owns_command(INSTANCE, "plug-a", "get")
                seen["other_target"] = manager.owns_command(INSTANCE, "bulb-b", "get")
                seen["set_verb"] = manager.owns_command(INSTANCE, "plug-a", "set")
                # A target state publish with nothing outstanding is NOT consumed.
                harness.pending.clear()
                manager._active.outstanding.clear()
                seen["idle_state"] = manager.on_state(INSTANCE, "plug-a")

        harness.on_advance = probe
        await harness.run_to_completion()
        assert seen == {
            "owns": True,
            "other_target": False,
            "set_verb": False,
            "idle_state": False,
        }

    asyncio.run(scenario())
