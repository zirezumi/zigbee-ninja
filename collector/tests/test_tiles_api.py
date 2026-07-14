from zigbee_ninja.tiles import TileManager

SETUP = {"username": "admin", "password": "correct-horse"}


def authed(client):
    client.post("/api/setup", json=SETUP)
    return client


def test_tiles_endpoints_require_auth(client):
    assert client.get("/api/tiles").status_code == 401
    assert (
        client.post(
            "/api/tiles/deploy", json={"capability": "z2m_extension", "target": "x"}
        ).status_code
        == 401
    )
    assert client.post("/api/tiles/revoke_all").status_code == 401


def test_tiles_list_synthesizes_from_registry(client):
    authed(client)
    engine = client.app.state.engine
    engine.registry.handle("z2m-test/bridge/info", b'{"version": "2.3.0"}')

    tiles = client.get("/api/tiles").json()["tiles"]
    assert len(tiles) == 1
    assert tiles[0]["target"] == "z2m-test"
    assert tiles[0]["status"] == "available"
    assert tiles[0]["bundled_version"]


def test_deploy_endpoint_flow(client, monkeypatch):
    authed(client)

    async def fake_deploy(self, base):
        return {"capability": "z2m_extension", "target": base, "status": "deployed"}

    monkeypatch.setattr(TileManager, "deploy_extension", fake_deploy)
    response = client.post(
        "/api/tiles/deploy", json={"capability": "z2m_extension", "target": "z2m-test"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deployed"


def test_deploy_unknown_capability_rejected(client):
    authed(client)
    response = client.post(
        "/api/tiles/deploy", json={"capability": "warp_drive", "target": "z2m-test"}
    )
    assert response.status_code == 400


def test_deploy_without_broker_returns_conflict(client):
    authed(client)
    response = client.post(
        "/api/tiles/deploy", json={"capability": "z2m_extension", "target": "z2m-test"}
    )
    # No broker configured → engine.publish raises RuntimeError → 409
    assert response.status_code == 409
