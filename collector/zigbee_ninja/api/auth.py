"""Admin account + session auth. Single-admin in V1 (DESIGN.md §13, §15)."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

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
    return datetime.now(timezone.utc)


def user_count(db: Database) -> int:
    row = db.connect().execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return row["n"]


def create_user(db: Database, username: str, password: str) -> int:
    if not _USERNAME_RE.match(username):
        raise ValueError("Username must be 3-32 characters: letters, digits, . _ -")
    if len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD_LEN} characters")
    conn = db.connect()
    cursor = conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (username, _hasher.hash(password)),
    )
    conn.commit()
    return cursor.lastrowid


def authenticate(db: Database, username: str, password: str) -> dict | None:
    row = (
        db.connect()
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
    conn = db.connect()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (_token_hash(token), user_id, (_now() + SESSION_TTL).isoformat()),
    )
    conn.commit()
    return token


def resolve_session(db: Database, token: str) -> dict | None:
    conn = db.connect()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now().isoformat(),))
    conn.commit()
    row = conn.execute(
        "SELECT u.id, u.username FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ?",
        (_token_hash(token),),
    ).fetchone()
    return {"id": row["id"], "username": row["username"]} if row else None


def delete_session(db: Database, token: str) -> None:
    conn = db.connect()
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()
