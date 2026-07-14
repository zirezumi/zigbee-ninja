import zigbee_ninja.api.app as app_module
from zigbee_ninja.ingest.engine import Engine
from zigbee_ninja.ingest.hacontrol import HaAttribution, HaConfig

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


def publish_event(topic: str, context: dict) -> dict:
    return {
        "event_type": "call_service",
        "data": {
            "domain": "mqtt",
            "service": "publish",
            "service_data": {"topic": topic, "payload": "{}"},
        },
        "context": context,
    }


def test_automation_context_names_a_publish():
    attribution = HaAttribution(clock=FakeClock())
    attribution.handle_event(automation_event("PASCL Office Lifecycle", "ctx-run-1"))
    result = attribution.handle_event(
        publish_event("z2m-1/office/set", {"id": "ctx-run-1", "parent_id": "ctx-trigger"})
    )
    assert result == ("z2m-1/office/set", "automation: PASCL Office Lifecycle")
    assert attribution.name_for("z2m-1/office/set") == "automation: PASCL Office Lifecycle"


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
    clock.now += 10.0
    assert attribution.name_for("z2m-1/lamp/set") is None


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


def test_engine_prefers_ha_name_for_chain_commander(client):
    client.post("/api/setup", json=SETUP)
    engine = client.app.state.engine
    engine.registry.handle("z2m-test/bridge/info", b'{"version": "2.3.0"}')

    engine.ha_attr.handle_event(automation_event("PASCL Kitchen Lifecycle", "run-9"))
    engine.ha_attr.handle_event(
        publish_event("z2m-test/kitchen/set", {"id": "run-9"})
    )
    engine.on_message("z2m-test/kitchen/set", b'{"state":"ON"}')

    chains = engine.chains._open[("z2m-test", "kitchen")]
    assert chains[0].client == "automation: PASCL Kitchen Lifecycle"


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
