"""Admin account + session auth. Single-admin in V1 (DESIGN.md §13, §15)."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from ..store.db import Database

SESSION_TTL = timedelta(days=14)
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
_MIN_PASSWORD_LEN = 8

_hasher = PasswordHasher()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def user_count(db: Database) -> int:
    # fresh_read, not connect: on a stale snapshot this reports a configured
    # install as needing first-run setup.
    row = db.fresh_read().execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return row["n"]


def create_user(db: Database, username: str, password: str) -> int:
    if not _USERNAME_RE.match(username):
        raise ValueError("Username must be 3-32 characters: letters, digits, . _ -")
    if len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD_LEN} characters")
    with db.write() as conn:
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, _hasher.hash(password)),
        )
    return cursor.lastrowid


def authenticate(db: Database, username: str, password: str) -> dict | None:
    row = (
        db.fresh_read()
        .execute("SELECT id, username, password_hash FROM users WHERE username = ?", (username,))
        .fetchone()
    )
    if row is None:
        return None
    try:
        _hasher.verify(row["password_hash"], password)
    except VerifyMismatchError:
        return None
    return {"id": row["id"], "username": row["username"]}


def create_session(db: Database, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), user_id, (_now() + SESSION_TTL).isoformat()),
        )
    return token


def resolve_session(db: Database, token: str) -> dict | None:
    """Resolve a session cookie. READ ONLY, deliberately.

    This used to open with `DELETE FROM sessions WHERE expires_at < ?`, which put
    a write on the path every request and every WebSocket handshake takes. When
    that write started failing, authentication failed with it: /api/ws/fleet and
    every async endpoint returned 500 for 29 hours (see Database.write).

    Expiry is enforced by the predicate below, NOT by the prune. Keep it that
    way: with expiry resting on the DELETE, dropping or weakening the prune
    silently turns every expired session into a valid one.
    """
    row = (
        db.fresh_read()
        .execute(
            "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at >= ?",
            (_token_hash(token), _now().isoformat()),
        )
        .fetchone()
    )
    return {"id": row["id"], "username": row["username"]} if row else None


def prune_expired_sessions(db: Database) -> int:
    """Housekeeping only: expired rows do not authenticate either way."""
    with db.write() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now().isoformat(),))
    return cursor.rowcount


def delete_session(db: Database, token: str) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
