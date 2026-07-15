import json

from tests.test_pcap_cli import conversation

SETUP = {"username": "admin", "password": "correct-horse"}


def authed(client):
    client.post("/api/setup", json=SETUP)
    return client


def test_tap_info_requires_auth(client):
    assert client.get("/api/tap").status_code == 401


def test_tap_token_stable_and_present(client):
    authed(client)
    first = client.get("/api/tap").json()
    assert first["token"]
    assert first["stats"]["agents"] == 0
    # Token is generated once and stays stable.
    assert client.get("/api/tap").json()["token"] == first["token"]


def test_tap_ws_rejects_bad_token(client):
    authed(client)
    import pytest
    from fastapi.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/ws/tap", headers={"Authorization": "Bearer wrong"}
        ):
            pass


def test_airtime_requires_auth(client):
    assert client.get("/api/airtime").status_code == 401


def test_airtime_endpoint_aggregates_stored_windows(client):
    import time

    authed(client)
    conn = client.app.state.db.connect()
    now = int(time.time())
    conn.execute(
        "INSERT INTO airtime_10s VALUES (?, 'z2m-test', 'tx_unicast', 27200.0, 10)",
        (now - 20,),
    )
    conn.execute(
        "INSERT INTO airtime_10s VALUES (?, 'z2m-test', 'rx', 2784.0, 1)", (now - 20,)
    )
    conn.commit()

    data = client.get("/api/airtime?seconds=600").json()
    instance = data["instances"]["z2m-test"]
    assert instance["buckets"]["tx_unicast"] == {"airtime_us": 27200.0, "frames": 10}
    assert instance["us_per_s"] == round((27200.0 + 2784.0) / 600, 1)
    assert instance["budget_pct"] > 0
    assert instance["provenance"] == "reconstructed"


def test_latency_endpoint_returns_stored_windows(client):
    import time

    authed(client)
    assert client.get("/api/latency").status_code == 200
    conn = client.app.state.db.connect()
    now = int(time.time())
    conn.execute(
        "INSERT INTO latency_10s VALUES (?, 'z2m-test', 12, 55.0, 240.0, 260.0)",
        (now - 20,),
    )
    conn.commit()

    data = client.get("/api/latency?seconds=600").json()
    assert data["rows"] == [
        {
            "ts": now - 20,
            "instance": "z2m-test",
            "count": 12,
            "p50_ms": 55.0,
            "p95_ms": 240.0,
            "max_ms": 260.0,
        }
    ]


def test_tap_ws_ingests_pcap_stream(client):
    authed(client)
    engine = client.app.state.engine
    token = engine.tap_token()
    # Point the registry at the synthetic conversation's coordinator endpoint.
    engine.registry.handle(
        "z2m-test/bridge/info",
        json.dumps({"config": {"serial": {"port": "tcp://10.0.0.50:6638"}}}).encode(),
    )

    pcap, _ = conversation()
    with client.websocket_connect(
        "/api/ws/tap", headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        ws.send_text(json.dumps({"type": "hello", "agent": "test", "iface": "lo"}))
        ws.send_bytes(pcap)

    stats = engine.tap.stats()
    assert len(stats["flows"]) == 1
    flow = stats["flows"][0]
    assert flow["instance"] == "z2m-test"
    assert flow["protocol_version"] == 13
    assert flow["ezsp_frames"]["sendUnicast"] == 1
