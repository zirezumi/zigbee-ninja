SETUP = {"username": "admin", "password": "correct-horse"}


def test_me_requires_authentication(client):
    assert client.get("/api/auth/me").status_code == 401


def test_setup_creates_admin_and_logs_in(client):
    response = client.post("/api/setup", json=SETUP)
    assert response.status_code == 201
    assert client.get("/api/auth/me").json() == {"username": "admin"}


def test_setup_is_single_shot(client):
    assert client.post("/api/setup", json=SETUP).status_code == 201
    retry = client.post("/api/setup", json={"username": "eve", "password": "long-enough"})
    assert retry.status_code == 409


def test_setup_rejects_weak_password(client):
    response = client.post("/api/setup", json={"username": "admin", "password": "short"})
    assert response.status_code == 400
    assert client.get("/api/health").json()["setup_complete"] is False


def test_setup_rejects_bad_username(client):
    response = client.post("/api/setup", json={"username": "a b!", "password": "long-enough"})
    assert response.status_code == 400


def test_login_logout_cycle(client):
    client.post("/api/setup", json=SETUP)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401

    bad = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-wrong"})
    assert bad.status_code == 401
    assert client.get("/api/auth/me").status_code == 401

    good = client.post("/api/auth/login", json=SETUP)
    assert good.status_code == 200
    assert client.get("/api/auth/me").json() == {"username": "admin"}


def test_session_cookie_is_httponly(client):
    response = client.post("/api/setup", json=SETUP)
    cookie_header = response.headers.get("set-cookie", "")
    assert "zn_session=" in cookie_header
    assert "HttpOnly" in cookie_header
