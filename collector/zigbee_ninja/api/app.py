"""FastAPI application factory: health, first-run setup, session auth, GUI serving."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..store.config import ConfigStore
from ..store.db import Database
from . import auth

SESSION_COOKIE = "zn_session"


class Credentials(BaseModel):
    username: str
    password: str


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=int(auth.SESSION_TTL.total_seconds()),
        path="/",
    )


def create_app(data_dir: Path | str | None = None, static_dir: Path | str | None = None) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("ZN_DATA_DIR", "./data"))
    static_dir = static_dir or os.environ.get("ZN_STATIC_DIR")

    app = FastAPI(title="zigbee-ninja", version=__version__, docs_url=None, openapi_url=None)
    db = Database(data_dir)
    app.state.db = db
    app.state.config = ConfigStore(db)

    def require_user(request: Request) -> dict:
        token = request.cookies.get(SESSION_COOKIE)
        user = auth.resolve_session(db, token) if token else None
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "setup_complete": auth.user_count(db) > 0,
        }

    @app.post("/api/setup", status_code=201)
    def setup(credentials: Credentials, response: Response) -> dict:
        if auth.user_count(db) > 0:
            raise HTTPException(status_code=409, detail="Setup already completed")
        try:
            user_id = auth.create_user(db, credentials.username, credentials.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _set_session_cookie(response, auth.create_session(db, user_id))
        return {"username": credentials.username}

    @app.post("/api/auth/login")
    def login(credentials: Credentials, response: Response) -> dict:
        user = auth.authenticate(db, credentials.username, credentials.password)
        if user is None:
            raise HTTPException(status_code=401, detail="Invalid username or password")
        _set_session_cookie(response, auth.create_session(db, user["id"]))
        return {"username": user["username"]}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            auth.delete_session(db, token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(request: Request) -> dict:
        user = require_user(request)
        return {"username": user["username"]}

    if static_dir and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    else:

        @app.get("/")
        def index() -> dict:
            return {
                "service": "zigbee-ninja",
                "version": __version__,
                "note": "frontend bundle not present; API only",
            }

    return app
