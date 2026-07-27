import json

import zigbee_ninja.api.app as app_module
from zigbee_ninja.attribution.chains import AMBIGUOUS_COMMANDER, command_digest
from zigbee_ninja.ingest.engine import Engine
from zigbee_ninja.ingest.hacontrol import HaAttribution, HaConfig, payload_fingerprint

SETUP = {"username": "admin", "password": "correct-horse"}


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def automation_event(name: str, context_id: str) -> dict:
    return {
        "event_type": "automation_triggered",
        "data": {"name": name, "entity_id": f"automation.{name.lower()}"},
        "context": {"id": context_id, "parent_id": None, "user_id": None},
    }


def publish_event(topic: str, context: dict, payload="{}") -> dict:
    service_data = {"topic": topic}
    if payload is not _ABSENT:
        service_data["payload"] = payload
    return {
        "event_type": "call_service",
        "data": {"domain": "mqtt", "service": "publish", "service_data": service_data},
        "context": context,
    }


class _Absent:
    """Sentinel for a service call that carries no payload key at all."""


_ABSENT = _Absent()


def wire(payload) -> str:
    """Digest of the bytes such a payload reaches the broker as."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode()
    return command_digest(raw)


def test_automation_context_names_a_publish():
    attribution = HaAttribution(clock=FakeClock())
    attribution.handle_event(automation_event("PASCL Office Lifecycle", "ctx-run-1"))
    result = attribution.handle_event(
        publish_event("z2m-1/office/set", {"id": "ctx-run-1", "parent_id": "ctx-trigger"})
    )
    assert result == (
        "z2m-1/office/set",
        "automation: PASCL Office Lifecycle",
        wire("{}"),
    )
    assert (
        attribution.name_for("z2m-1/office/set", wire("{}"))
        == "automation: PASCL Office Lifecycle"
    )


def test_fingerprint_matches_the_bytes_home_assistant_publishes():
    """The whole correlation rests on reproducing the wire bytes exactly.

    A template rendering to a mapping arrives as a native dict, and Home
    Assistant serialises it with json.dumps' DEFAULT separators. Compact
    separators produce different bytes and would break every match, silently,
    by degrading every command to unattributed.
    """
    rendered = {"brightness": 149, "color_temp": 326, "state": "ON"}
    on_the_wire = b'{"brightness": 149, "color_temp": 326, "state": "ON"}'
    assert payload_fingerprint(rendered) == command_digest(on_the_wire)
    assert payload_fingerprint(rendered) != command_digest(
        json.dumps(rendered, separators=(",", ":")).encode()
    )
    # A string payload is published verbatim.
    assert payload_fingerprint('{"state":"ON"}') == command_digest(b'{"state":"ON"}')


def test_parent_context_resolution_for_scripts():
    attribution = HaAttribution(clock=FakeClock())
    attribution.handle_event(
        {
            "event_type": "script_started",
            "data": {"name": "Drive Office Lights", "entity_id": "script.drive_office"},
            "context": {"id": "ctx-script", "parent_id": "ctx-run-1", "user_id": None},
        }
    )
    result = attribution.handle_event(
        publish_event("z2m-1/office/set", {"id": "ctx-other", "parent_id": "ctx-script"})
    )
    assert result[1] == "script: Drive Office Lights"


def test_ui_publish_labeled_user():
    attribution = HaAttribution(clock=FakeClock())
    result = attribution.handle_event(
        publish_event("z2m-1/lamp/set", {"id": "x", "parent_id": None, "user_id": "u123"})
    )
    assert result[1] == "user (UI/API)"


def test_correlation_window_expires():
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event(automation_event("A", "c1"))
    attribution.handle_event(publish_event("z2m-1/lamp/set", {"id": "c1"}))
    assert attribution.name_for("z2m-1/lamp/set", wire("{}")) == "automation: A"
    clock.now += 10.0
    assert attribution.name_for("z2m-1/lamp/set", wire("{}")) is None


def test_correlation_window_widens_by_the_loop_stall():
    """A stall must not permanently lose the commander name.

    Both the remembered publish and the command it explains are stamped on the
    event loop; a stall longer than the fixed tolerance pushed a genuine pair
    apart and dropped the row to "(unattributed)" for good.
    """
    clock = FakeClock()
    skew = [0.0]
    attribution = HaAttribution(clock=clock, loop_skew_ms=lambda: skew[0])
    attribution.handle_event(automation_event("A", "c1"))
    attribution.handle_event(publish_event("z2m-1/lamp/set", {"id": "c1"}))

    clock.now += 8.0
    assert attribution.name_for("z2m-1/lamp/set", wire("{}")) is None  # fixed 3 s bound

    skew[0] = 7000.0
    assert attribution.name_for("z2m-1/lamp/set", wire("{}")) == "automation: A"


def test_context_ttl_prunes():
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event(automation_event("A", "c1"))
    clock.now += 700.0  # beyond CONTEXT_TTL
    result = attribution.handle_event(publish_event("z2m-1/lamp/set", {"id": "c1"}))
    assert result[1] == "ha (unresolved context)"


def test_non_mqtt_service_calls_ignored():
    attribution = HaAttribution(clock=FakeClock())
    result = attribution.handle_event(
        {
            "event_type": "call_service",
            "data": {"domain": "light", "service": "turn_on", "service_data": {}},
            "context": {"id": "x"},
        }
    )
    assert result is None


def script_event(name: str, context_id: str) -> dict:
    return {
        "event_type": "script_started",
        "data": {"name": name, "entity_id": f"script.{name.lower()}"},
        "context": {"id": context_id, "parent_id": None, "user_id": None},
    }


def test_two_publishers_on_one_topic_keep_their_own_names():
    """The bug this file did not cover, and so shipped.

    A room's phantom-sync script and its LED-state script write different
    parameters to the same device inside the window. Matching on topic alone
    returned whichever was recorded last for BOTH commands, crediting each
    script with a parameter it has no code path to publish.
    """
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    timeout = '{"loadLevelIndicatorTimeout": "Stay Off"}'
    intensity = '{"ledIntensityWhenOn": 99}'

    attribution.handle_event(script_event("Silent Phantom Sync", "ctx-sync"))
    attribution.handle_event(
        publish_event("z2m-3/dimmer/set", {"id": "ctx-sync"}, payload=timeout)
    )
    clock.now += 0.01
    attribution.handle_event(script_event("Publish Switch LED State", "ctx-led"))
    attribution.handle_event(
        publish_event("z2m-3/dimmer/set", {"id": "ctx-led"}, payload=intensity)
    )

    # Both candidates sit in the window; each command still finds its own.
    assert attribution.name_for("z2m-3/dimmer/set", wire(timeout)) == (
        "script: Silent Phantom Sync"
    )
    assert attribution.name_for("z2m-3/dimmer/set", wire(intensity)) == (
        "script: Publish Switch LED State"
    )


def test_identical_payloads_from_two_commanders_are_ambiguous():
    """Byte-identical writes cannot be told apart, and must not be guessed."""
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    same = '{"state": "ON"}'

    attribution.handle_event(automation_event("First Mover", "ctx-a"))
    attribution.handle_event(publish_event("z2m-1/lamp/set", {"id": "ctx-a"}, payload=same))
    clock.now += 0.05
    attribution.handle_event(automation_event("Second Mover", "ctx-b"))
    attribution.handle_event(publish_event("z2m-1/lamp/set", {"id": "ctx-b"}, payload=same))

    verdict = attribution.name_for("z2m-1/lamp/set", wire(same))
    assert verdict == AMBIGUOUS_COMMANDER
    assert verdict != "automation: Second Mover"  # never a confident wrong name
    assert attribution.counters["ambiguous"] == 1


def test_absent_payload_degrades_to_ambiguous_not_to_a_guess():
    """A publish whose bytes cannot be rendered taints the window (TRAP 1).

    It cannot be confirmed as the command in hand and cannot be ruled out
    either, so the honest answer is ambiguous rather than the name attached to
    an unrelated publish on the same topic.
    """
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event(automation_event("Opaque Publisher", "ctx-x"))
    result = attribution.handle_event(
        publish_event("z2m-1/lamp/set", {"id": "ctx-x"}, payload=_ABSENT)
    )
    assert result[2] is None  # no fingerprint derivable
    assert attribution.name_for("z2m-1/lamp/set", wire('{"state": "ON"}')) == (
        AMBIGUOUS_COMMANDER
    )


def test_unrelated_payload_on_the_same_topic_is_not_credited():
    """A stale entry for the topic must not name a command it cannot explain."""
    clock = FakeClock()
    attribution = HaAttribution(clock=clock)
    attribution.handle_event(automation_event("Earlier", "ctx-1"))
    attribution.handle_event(
        publish_event("z2m-1/lamp/set", {"id": "ctx-1"}, payload='{"state": "OFF"}')
    )
    clock.now += 1.0
    assert attribution.name_for("z2m-1/lamp/set", wire('{"brightness": 5}')) is None


def test_engine_prefers_ha_name_for_chain_commander(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    engine.registry.handle("z2m-test/bridge/info", b'{"version": "2.3.0"}')

    engine.ha_attr.handle_event(automation_event("PASCL Kitchen Lifecycle", "run-9"))
    engine.ha_attr.handle_event(
        publish_event("z2m-test/kitchen/set", {"id": "run-9"}, payload='{"state":"ON"}')
    )
    engine.on_message("z2m-test/kitchen/set", b'{"state":"ON"}')

    chains = engine.chains._open[("z2m-test", "kitchen")]
    assert chains[0].client == "automation: PASCL Kitchen Lifecycle"


def test_engine_backfills_the_right_chain_when_the_wire_arrives_first(client):
    """The majority path: the command lands before HA says who sent it.

    Two chains open on one target with different payloads; the two HA events
    then arrive. Target-only backfill gave both names to whichever chain was
    newest and unattributed.
    """
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    engine.registry.handle("z2m-test/bridge/info", b'{"version": "2.3.0"}')
    timeout = '{"loadLevelIndicatorTimeout": "Stay Off"}'
    intensity = '{"ledIntensityWhenOn": 99}'

    engine.on_message("z2m-test/dimmer/set", timeout.encode())
    engine.on_message("z2m-test/dimmer/set", intensity.encode())

    engine.ha_attr.handle_event(script_event("Silent Phantom Sync", "ctx-sync"))
    result = engine.ha_attr.handle_event(
        publish_event("z2m-test/dimmer/set", {"id": "ctx-sync"}, payload=timeout)
    )
    engine._on_ha_publish(*result)
    engine.ha_attr.handle_event(script_event("Publish Switch LED State", "ctx-led"))
    result = engine.ha_attr.handle_event(
        publish_event("z2m-test/dimmer/set", {"id": "ctx-led"}, payload=intensity)
    )
    engine._on_ha_publish(*result)

    opened = list(engine.chains._open[("z2m-test", "dimmer")])
    by_digest = {chain.payload_digest: chain.client for chain in opened}
    assert by_digest[command_digest(timeout.encode())] == "script: Silent Phantom Sync"
    assert by_digest[command_digest(intensity.encode())] == (
        "script: Publish Switch LED State"
    )


def test_ha_api_flow(client, monkeypatch):
    client.post("/api/setup", json=SETUP)

    view = client.get("/api/ha").json()
    assert view["configured"] is False
    assert view["status"]["state"] == "unconfigured"

    async def fake_test_ha(config, timeout=6.0):
        assert isinstance(config, HaConfig)
        return None

    async def fake_restart(self):
        return None

    monkeypatch.setattr(app_module, "test_ha", fake_test_ha)
    monkeypatch.setattr(Engine, "restart_ha", fake_restart)

    response = client.post(
        "/api/ha", json={"url": "http://ha.example:8123/", "token": "tok-secret"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "http://ha.example:8123"
    assert "token" not in body

    persisted = client.get("/api/ha").json()
    assert persisted["configured"] is True
    assert "token" not in persisted


def test_ha_api_rejects_bad_connection(client, monkeypatch):
    client.post("/api/setup", json=SETUP)

    async def failing_test_ha(config, timeout=6.0):
        return "auth rejected"

    monkeypatch.setattr(app_module, "test_ha", failing_test_ha)
    response = client.post("/api/ha", json={"url": "http://ha.example:8123", "token": "bad"})
    assert response.status_code == 400
    assert client.get("/api/ha").json()["configured"] is False
