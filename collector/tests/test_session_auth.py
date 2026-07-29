"""Session resolution: expiry enforcement, and staying off the write path.

resolve_session used to open with `DELETE FROM sessions WHERE expires_at < ?`.
That put a write on the path of every request and every WebSocket handshake, and
when the write started failing, authentication failed with it.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

from zigbee_ninja.api import auth
from zigbee_ninja.store.db import Database

PASSWORD = "correct-horse"


def _account(db: Database) -> tuple[int, str]:
    user_id = auth.create_user(db, "zach", PASSWORD)
    return user_id, auth.create_session(db, user_id)


def _backdate(db: Database, delta: timedelta) -> None:
    with db.write() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ?", ((datetime.now(UTC) + delta).isoformat(),)
        )


def test_valid_session_resolves(tmp_path):
    db = Database(tmp_path)
    user_id, token = _account(db)
    assert auth.resolve_session(db, token) == {"id": user_id, "username": "zach"}


def test_expired_session_is_refused_by_the_query_not_the_prune(tmp_path):
    """The trap in this fix: expiry must not depend on the prune succeeding.

    With expiry resting on the DELETE (as it did), anything that stops the prune
    from running turns every expired session into a valid one. So the row is left
    in place here on purpose, and it still must not authenticate.
    """
    db = Database(tmp_path)
    _, token = _account(db)
    _backdate(db, -timedelta(seconds=1))

    assert auth.resolve_session(db, token) is None
    still_there = db.connect().execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
    assert still_there == 1, "expiry is enforced by the predicate, with the row still present"


def test_unknown_token_resolves_to_nothing(tmp_path):
    db = Database(tmp_path)
    _account(db)
    assert auth.resolve_session(db, "not-a-real-token") is None


def test_resolve_session_works_while_another_connection_holds_the_write_lock(tmp_path):
    """The regression: resolving a session must not need to write.

    A second connection holds the write lock for the whole check. Under WAL a
    reader is fine alongside a writer, so this passes now; the old
    delete-then-select would have spent busy_timeout and then raised, which is
    exactly how the fleet socket died.
    """
    db = Database(tmp_path)
    user_id, token = _account(db)

    blocker = sqlite3.connect(db.path)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO settings (key, value) VALUES ('held', '1')")
    try:
        assert auth.resolve_session(db, token) == {"id": user_id, "username": "zach"}
    finally:
        blocker.rollback()
        blocker.close()


def test_prune_removes_only_expired_sessions(tmp_path):
    db = Database(tmp_path)
    user_id, live_token = _account(db)
    stale_token = auth.create_session(db, user_id)
    with db.write() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (
                (datetime.now(UTC) - timedelta(days=30)).isoformat(),
                auth._token_hash(stale_token),
            ),
        )

    assert auth.prune_expired_sessions(db) == 1
    assert auth.resolve_session(db, live_token) is not None
    assert auth.resolve_session(db, stale_token) is None


def test_fleet_socket_opens_while_a_writer_holds_the_lock(client):
    """End to end: the handshake that returned 500 for 29 hours.

    Holding the write lock across the handshake reproduces the contention that
    started the incident. The socket must still open and deliver a snapshot.
    """
    client.post("/api/setup", json={"username": "admin", "password": PASSWORD})
    db = client.app.state.db

    blocker = sqlite3.connect(db.path)
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO settings (key, value) VALUES ('held', '1')")
    try:
        with client.websocket_connect("/api/ws/fleet") as socket:
            snapshot = socket.receive_json()
        assert "broker" in snapshot
    finally:
        blocker.rollback()
        blocker.close()
