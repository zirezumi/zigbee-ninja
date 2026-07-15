import asyncio
import json
import time

from zigbee_ninja.calibration import benchmark

SETUP = {"username": "admin", "password": "correct-horse"}

DEVICES_PAYLOAD = json.dumps(
    [
        {
            "ieee_address": "0xa1",
            "friendly_name": "plug-a",
            "type": "Router",
            "power_source": "Mains (single phase)",
            "network_address": 10,
            "definition": {
                "vendor": "ExampleCo",
                "model": "PLUG-1",
                "exposes": [
                    {"type": "switch", "features": [{"property": "state", "access": 7}]}
                ],
            },
            "endpoints": {"1": {"bindings": []}},
        }
    ]
).encode()


def authed(client):
    client.post("/api/setup", json=SETUP)
    return client


def feed_registry(engine):
    engine.registry.handle("z2m-test/bridge/info", b'{"version": "2.10.1"}')
    engine.registry.handle("z2m-test/bridge/devices", DEVICES_PAYLOAD)


def test_calibration_endpoints_require_auth(client):
    assert client.get("/api/calibration").status_code == 401
    assert client.get("/api/calibration/candidates?instance=x").status_code == 401
    assert (
        client.post(
            "/api/calibration/preview", json={"instance": "x", "target": "y"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/calibration/run",
            json={"instance": "x", "target": "y", "authorization": "z"},
        ).status_code
        == 401
    )
    assert client.post("/api/calibration/abort").status_code == 401


def test_validation_and_authorization_gates(client):
    authed(client)
    engine = client.app.state.engine

    assert (
        client.get("/api/calibration/candidates", params={"instance": "nope"}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/calibration/preview", json={"instance": "nope", "target": "x"}
        ).status_code
        == 400
    )

    feed_registry(engine)
    view = client.get("/api/calibration").json()
    assert view["active"] is None
    assert view["history"] == []

    candidates = client.get(
        "/api/calibration/candidates", params={"instance": "z2m-test"}
    ).json()
    assert candidates["candidates"][0]["friendly_name"] == "plug-a"

    preview = client.post(
        "/api/calibration/preview", json={"instance": "z2m-test", "target": "plug-a"}
    ).json()
    assert preview["authorization"]
    assert preview["topic"] == "z2m-test/plug-a/get"

    # A run demands the exact token from a fresh preview.
    response = client.post(
        "/api/calibration/run",
        json={"instance": "z2m-test", "target": "plug-a", "authorization": "bogus"},
    )
    assert response.status_code == 409
    assert "fresh preview" in response.json()["detail"]

    # Abort with nothing active is a clean conflict.
    assert client.post("/api/calibration/abort").status_code == 409

    # Mode gates: a single-target preview needs a target; a spread preview
    # needs enough eligible routers (this fixture has one).
    response = client.post("/api/calibration/preview", json={"instance": "z2m-test"})
    assert response.status_code == 400
    assert "target is required" in response.json()["detail"]
    response = client.post(
        "/api/calibration/preview", json={"instance": "z2m-test", "mode": "spread"}
    )
    assert response.status_code == 400
    assert "spread ramp needs" in response.json()["detail"]


def test_run_lifecycle_self_attribution_and_cooldown(client, monkeypatch):
    """Drives a miniature but real ramp through the full engine wiring: the
    stub broker echoes the benchmark's own /get commands and the target's
    state replies back into on_message, which must classify both as `self`
    and open no attribution chains (P4)."""
    authed(client)
    engine = client.app.state.engine
    feed_registry(engine)
    monkeypatch.setattr(benchmark, "RAMP_RATES_EPS", (5.0,))
    monkeypatch.setattr(benchmark, "STEP_SECONDS", 0.4)

    class StubIngest:
        status = {"state": "connected", "error": None, "connected_since": None}

        async def publish(self, topic, payload, retain=False):
            loop = asyncio.get_running_loop()
            loop.call_soon(engine.on_message, topic, payload.encode())
            loop.call_soon(engine.on_message, "z2m-test/plug-a", b'{"state": "ON"}')

    engine._ingest = StubIngest()

    preview = client.post(
        "/api/calibration/preview", json={"instance": "z2m-test", "target": "plug-a"}
    ).json()
    assert preview["steps"] == [{"rate_eps": 5.0, "duration_s": 0.4, "reads": 2}]

    response = client.post(
        "/api/calibration/run",
        json={
            "instance": "z2m-test",
            "target": "plug-a",
            "authorization": preview["authorization"],
        },
    )
    assert response.status_code == 202
    assert response.json()["active"]["target"] == "plug-a"

    view = None
    deadline = time.time() + 10
    while time.time() < deadline:
        view = client.get("/api/calibration").json()
        if view["active"] is None:
            break
        time.sleep(0.05)
    assert view is not None and view["active"] is None, "run did not finish in time"

    record = view["history"][0]
    assert record["status"] == "completed"
    assert record["knee"]["censored"] is True
    assert record["steps"][0]["timeouts"] == 0
    assert record["steps"][0]["completed"] >= 1
    assert view["cooldown_until"] is not None

    # P4: no chains were opened for the benchmark's own reads, and the
    # instance saw `self` traffic, never `commanded`.
    assert not engine.chains._open
    classes = engine.class_rates.snapshot().get("z2m-test", {})
    assert "self" in classes
    assert "commanded" not in classes

    # Cooldown blocks an immediate second run.
    preview = client.post(
        "/api/calibration/preview", json={"instance": "z2m-test", "target": "plug-a"}
    ).json()
    response = client.post(
        "/api/calibration/run",
        json={
            "instance": "z2m-test",
            "target": "plug-a",
            "authorization": preview["authorization"],
        },
    )
    assert response.status_code == 409
    assert "Cooling down" in response.json()["detail"]
