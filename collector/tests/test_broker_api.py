import pytest
from fastapi.websockets import WebSocketDisconnect

import zigbee_ninja.api.app as app_module
from zigbee_ninja.ingest.engine import Engine

SETUP = {"username": "admin", "password": "correct-horse"}
BROKER = {"host": "broker.example", "port": 1883, "username": "ninja", "password": "hunter22"}


@pytest.fixture()
def authed(client):
    client.post("/api/setup", json=SETUP)
    return client


def test_broker_endpoints_require_auth(client):
    assert client.get("/api/broker").status_code == 401
    assert client.post("/api/broker", json=BROKER).status_code == 401
    assert client.get("/api/instances").status_code == 401


def test_broker_unconfigured_view(authed):
    body = authed.get("/api/broker").json()
    assert body["configured"] is False
    assert body["status"]["state"] == "unconfigured"


def test_broker_save_happy_path(authed, monkeypatch):
    async def fake_test_connection(config, timeout=5.0):
        return None

    async def fake_restart(self):
        return None

    monkeypatch.setattr(app_module, "test_connection", fake_test_connection)
    monkeypatch.setattr(Engine, "restart_ingest", fake_restart)

    response = authed.post("/api/broker", json=BROKER)
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["host"] == "broker.example"
    assert body["username"] == "ninja"
    assert "password" not in body  # never echoed (DESIGN.md §15)

    persisted = authed.get("/api/broker").json()
    assert persisted["configured"] is True
    assert "password" not in persisted


def test_broker_save_rejected_on_failed_connection(authed, monkeypatch):
    async def fake_test_connection(config, timeout=5.0):
        return "connection refused"

    monkeypatch.setattr(app_module, "test_connection", fake_test_connection)

    response = authed.post("/api/broker", json=BROKER)
    assert response.status_code == 400
    assert "connection refused" in response.json()["detail"]
    assert authed.get("/api/broker").json()["configured"] is False


def test_instances_empty_before_discovery(authed):
    assert authed.get("/api/instances").json() == {"instances": []}


def test_fleet_websocket_requires_auth(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws/fleet"):
            pass


def test_fleet_websocket_streams_snapshot(authed):
    with authed.websocket_connect("/api/ws/fleet") as ws:
        message = ws.receive_json()
    assert "instances" in message
    assert "rates" in message
    assert message["broker"]["state"] == "unconfigured"
