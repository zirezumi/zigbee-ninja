"""Alert API: auth, seeded rules, CRUD round trip, validation (DESIGN.md §14)."""

SETUP = {"username": "admin", "password": "correct-horse"}


def authed(client):
    client.post("/api/setup", json=SETUP)
    return client


def test_alert_endpoints_require_auth(client):
    assert client.get("/api/alerts").status_code == 401
    assert client.get("/api/alerts/history").status_code == 401
    assert (
        client.post(
            "/api/alerts/rules",
            json={"name": "x", "metric": "wire_p95_ms", "threshold": 1},
        ).status_code
        == 401
    )


def test_alerts_view_shows_seeds_catalog_and_no_active(client):
    authed(client)
    data = client.get("/api/alerts").json()

    builtins = {rule["builtin"]: rule for rule in data["rules"] if rule["builtin"]}
    assert builtins["broker_down"]["enabled"] is True
    assert builtins["layout_mismatch"]["severity"] == "critical"
    # Capacity rules ship seeded but disabled — the user opts in (§14).
    assert builtins["knee_utilization"]["enabled"] is False
    assert builtins["wire_p95"]["enabled"] is False

    metrics = {entry["metric"] for entry in data["metrics"]}
    assert {"wire_p95_ms", "budget_pct", "tap_agents"} <= metrics
    assert data["active"] == []
    assert client.get("/api/alerts/history").json() == {"events": []}


def test_rule_crud_roundtrip(client):
    authed(client)
    body = {
        "name": "latency watch",
        "metric": "wire_p95_ms",
        "instance": "z2m-9",
        "op": ">",
        "threshold": 250.0,
        "clear_threshold": 150.0,
        "sustain_seconds": 30,
        "severity": "info",
        "enabled": True,
    }
    response = client.post("/api/alerts/rules", json=body)
    assert response.status_code == 201
    created = response.json()
    assert created["threshold"] == 250.0
    assert created["builtin"] is None

    rule_id = created["id"]
    updated = client.put(
        f"/api/alerts/rules/{rule_id}", json={**body, "threshold": 400.0, "enabled": False}
    ).json()
    assert updated["threshold"] == 400.0
    assert updated["enabled"] is False

    assert client.delete(f"/api/alerts/rules/{rule_id}").json() == {"ok": True}
    assert client.delete(f"/api/alerts/rules/{rule_id}").status_code == 404
    assert client.put(f"/api/alerts/rules/{rule_id}", json=body).status_code == 404


def test_rule_validation_errors(client):
    authed(client)

    def post(overrides):
        body = {"name": "x", "metric": "wire_p95_ms", "threshold": 100.0}
        body.update(overrides)
        return client.post("/api/alerts/rules", json=body).status_code

    assert post({"metric": "warp_core_breach"}) == 400
    assert post({"op": "="}) == 400
    assert post({"severity": "apocalyptic"}) == 400
    assert post({"clear_threshold": 200.0}) == 400  # wrong side for op '>'
    assert post({"metric": "tap_agents", "op": "<", "instance": "z2m-1"}) == 400
    assert post({"sustain_seconds": 999999}) == 400
