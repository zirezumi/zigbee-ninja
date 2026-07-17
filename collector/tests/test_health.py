from zigbee_ninja import __version__


def test_health_reports_version_and_setup_state(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["setup_complete"] is False
    assert "recent_stalls" in body["loop_lag"]
    assert set(body["loop_activity"]) == {"totals", "recent_slow"}


def test_health_setup_complete_after_setup(client):
    client.post("/api/setup", json={"username": "admin", "password": "correct-horse"})
    assert client.get("/api/health").json()["setup_complete"] is True


def test_root_serves_api_notice_without_frontend_bundle(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["service"] == "zigbee-ninja"
