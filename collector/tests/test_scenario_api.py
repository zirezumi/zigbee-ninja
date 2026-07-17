"""Rebalance view backend: lane context, advisor scoring, saved scenarios
(V2_PROPOSAL.md §V2-11)."""

from zigbee_ninja.recommend import rebalance

SETUP = {"username": "admin", "password": "correct-horse"}


def authed(client):
    client.post("/api/setup", json=SETUP)
    return client


def test_scenario_context_requires_auth(client):
    assert client.get("/api/scenario/context").status_code == 401


def test_scenario_context_shape_on_empty_fleet(client):
    authed(client)
    body = client.get("/api/scenario/context").json()
    assert body["instances"] == {}
    assert body["basis"]["note"]


def test_scenario_score_rejects_bad_moves(client):
    authed(client)
    response = client.post("/api/scenario/score", json={"moves": []})
    assert response.status_code == 400


def test_saved_scenarios_lifecycle(client):
    authed(client)
    assert client.get("/api/scenario/saved").json() == {"scenarios": []}

    move = {
        "kind": "device",
        "subject": "lamp_1",
        "from_instance": "z2m-a",
        "to_instance": "z2m-b",
    }
    saved = client.post(
        "/api/scenario/saved", json={"name": "study move", "moves": [move]}
    )
    assert saved.status_code == 200
    assert saved.json()["name"] == "study move"

    listed = client.get("/api/scenario/saved").json()["scenarios"]
    assert len(listed) == 1
    assert listed[0]["name"] == "study move"
    assert listed[0]["moves"][0]["subject"] == "lamp_1"
    assert listed[0]["saved_at"] > 0

    # Same name overwrites in place.
    client.post(
        "/api/scenario/saved",
        json={"name": "study move", "moves": [move, {**move, "subject": "lamp_2"}]},
    )
    listed = client.get("/api/scenario/saved").json()["scenarios"]
    assert len(listed) == 1
    assert len(listed[0]["moves"]) == 2

    assert client.delete("/api/scenario/saved/study move").status_code == 200
    assert client.get("/api/scenario/saved").json() == {"scenarios": []}
    assert client.delete("/api/scenario/saved/study move").status_code == 404


def test_saved_scenarios_validation(client):
    authed(client)
    move = {
        "kind": "device",
        "subject": "x",
        "from_instance": "a",
        "to_instance": "b",
    }
    assert (
        client.post("/api/scenario/saved", json={"name": "", "moves": [move]}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/scenario/saved", json={"name": "x" * 65, "moves": [move]}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/scenario/saved", json={"name": "empty", "moves": []}).status_code
        == 400
    )


def report_entry(before_eps, after_eps, sustained, touched=True):
    limits = {"sustained_eps": sustained} if sustained else None
    return {
        "burst": {
            "before_peak_1s": {"eps_1s": before_eps} if before_eps else None,
            "after_peak_1s": {"eps_1s": after_eps} if after_eps else None,
            "verdict": rebalance.scenario._judge(after_eps, limits),
        },
        "limits": limits,
        "touched": touched,
    }


def test_score_report_accepts_a_clearing_scenario():
    report = {
        "instances": {
            "z2m-a": report_entry(12.0, 6.0, 8.0),
            "z2m-b": report_entry(2.0, 6.0, 8.0),
        }
    }
    score = rebalance.score_report(report)
    assert score["accepted"] is True
    assert score["pressured_before"] == ["z2m-a"]
    assert score["instances"]["z2m-a"]["before_verdict"] == "above_sustained"
    assert score["instances"]["z2m-a"]["after_verdict"] == "ok"
    assert any("clear" in note for note in score["notes"])


def test_score_report_rejects_relocated_pressure():
    report = {
        "instances": {
            "z2m-a": report_entry(12.0, 1.0, 8.0),
            "z2m-b": report_entry(2.0, 12.0, 8.0),
        }
    }
    score = rebalance.score_report(report)
    assert score["accepted"] is False
    assert any("relocate" in note for note in score["notes"])


def test_score_report_names_safety_only_scenarios():
    report = {
        "instances": {
            "z2m-a": report_entry(4.0, 3.0, 8.0),
            "z2m-b": report_entry(2.0, 3.0, 8.0),
        }
    }
    score = rebalance.score_report(report)
    assert score["accepted"] is True
    assert score["pressured_before"] == []
    assert any("judged for safety" in note for note in score["notes"])
