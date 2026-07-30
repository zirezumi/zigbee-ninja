"""Fleet socket lifecycle.

The disconnect cases drive the handler directly with a stub socket rather than
through TestClient: a server-side close mid-test leaves the test client's own
teardown waiting for a peer that has already gone, which hangs the suite. The
branch under test is in the handler, so that is where the test belongs.
"""

import asyncio

import pytest

from zigbee_ninja.api import auth

SETUP = {"username": "admin", "password": "correct-horse"}
TRANSPORT_GONE = "unable to perform operation on <TCPTransport closed=True>; the handler is closed"


class _StubSocket:
    """Enough WebSocket for the fleet handler, with a send that can fail."""

    def __init__(self, token: str, send_error: Exception | None = None):
        self.cookies = {"zn_session": token}
        self.send_error = send_error
        self.accepted = False
        self.closed_code: int | None = None
        self.sent: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def send_json(self, data: dict) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)


def _handler(client):
    for route in client.app.routes:
        if getattr(route, "path", None) == "/api/ws/fleet":
            return route.endpoint
    raise AssertionError("/api/ws/fleet is not routed")


def _session_token(client) -> str:
    db = client.app.state.db
    user = auth.authenticate(db, SETUP["username"], SETUP["password"])
    return auth.create_session(db, user["id"])


def test_fleet_socket_delivers_snapshots(client):
    client.post("/api/setup", json=SETUP)
    with client.websocket_connect("/api/ws/fleet") as socket:
        snapshot = socket.receive_json()
    assert "broker" in snapshot and "instances" in snapshot


def test_a_vanished_client_closes_the_socket_quietly(client):
    """A client that goes away without a close frame is not an error.

    uvloop surfaces it as a RuntimeError from the send rather than as a
    WebSocketDisconnect, and it used to reach the log as an unhandled ASGI
    traceback. The handler should simply stop.
    """
    client.post("/api/setup", json=SETUP)
    socket = _StubSocket(_session_token(client), send_error=RuntimeError(TRANSPORT_GONE))

    asyncio.run(_handler(client)(socket))

    assert socket.accepted, "precondition: auth passed and the socket was accepted"
    assert socket.sent == [], "the send failed, so nothing was delivered"


def test_a_broken_snapshot_still_surfaces(client, monkeypatch):
    """The narrow scope of that catch, pinned.

    Only the send is forgiven. A RuntimeError from assembling the snapshot is a
    bug in the collector and must not be laundered into a tidy disconnect.
    """
    client.post("/api/setup", json=SETUP)
    socket = _StubSocket(_session_token(client))

    def broken():
        raise RuntimeError("snapshot assembly is broken")

    monkeypatch.setattr(client.app.state.engine.registry, "snapshot", broken)

    with pytest.raises(RuntimeError, match="snapshot assembly is broken"):
        asyncio.run(_handler(client)(socket))


def test_an_unauthenticated_socket_is_refused(client):
    client.post("/api/setup", json=SETUP)
    socket = _StubSocket("not-a-real-token")

    asyncio.run(_handler(client)(socket))

    assert socket.closed_code == 4401
    assert not socket.accepted
