"""FastAPI application factory: health, auth, broker config, fleet, GUI serving."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..attribution import queries as attribution_queries
from ..ingest.engine import Engine
from ..ingest.mqtt import BrokerConfig, test_connection
from ..store.config import ConfigStore
from ..store.db import Database
from . import auth

MAX_QUERY_WINDOW_SECONDS = 14 * 24 * 3600

SESSION_COOKIE = "zn_session"


class Credentials(BaseModel):
    username: str
    password: str


class BrokerSettings(BaseModel):
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None


class TileAction(BaseModel):
    capability: str
    target: str


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

    db = Database(data_dir)
    config = ConfigStore(db)
    engine = Engine(db, config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await engine.start()
        yield
        await engine.stop()

    app = FastAPI(
        title="zigbee-ninja", version=__version__, docs_url=None, openapi_url=None,
        lifespan=lifespan,
    )
    app.state.db = db
    app.state.config = config
    app.state.engine = engine

    def require_user(request: Request) -> dict:
        token = request.cookies.get(SESSION_COOKIE)
        user = auth.resolve_session(db, token) if token else None
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user

    # -- health & auth ---------------------------------------------------------

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

    # -- broker & fleet --------------------------------------------------------

    def _broker_view() -> dict:
        stored = engine.broker_config()
        view: dict = {"configured": stored is not None, "status": engine.ingest_status()}
        if stored is not None:
            view.update(stored.public_dict())
        return view

    @app.get("/api/broker")
    def broker_get(request: Request) -> dict:
        require_user(request)
        return _broker_view()

    @app.post("/api/broker")
    async def broker_set(request: Request, settings: BrokerSettings) -> dict:
        require_user(request)
        candidate = BrokerConfig(
            host=settings.host,
            port=settings.port,
            username=settings.username or None,
            password=settings.password or None,
        )
        error = await test_connection(candidate)
        if error is not None:
            raise HTTPException(status_code=400, detail=f"Connection test failed: {error}")
        await engine.apply_broker_config(settings.model_dump())
        return _broker_view()

    @app.get("/api/instances")
    def instances(request: Request) -> dict:
        require_user(request)
        return {"instances": engine.registry.snapshot()}

    @app.get("/api/tiles")
    def tiles_list(request: Request) -> dict:
        require_user(request)
        bases = [instance["base_topic"] for instance in engine.registry.snapshot()]
        return {"tiles": engine.tiles.list(bases, engine.probes.stats())}

    @app.post("/api/tiles/deploy")
    async def tiles_deploy(request: Request, action: TileAction) -> dict:
        require_user(request)
        if action.capability != "z2m_extension":
            raise HTTPException(status_code=400, detail="Unknown tile capability")
        try:
            return await engine.tiles.deploy_extension(action.target)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tiles/revoke")
    async def tiles_revoke(request: Request, action: TileAction) -> dict:
        require_user(request)
        if action.capability != "z2m_extension":
            raise HTTPException(status_code=400, detail="Unknown tile capability")
        try:
            return await engine.tiles.revoke_extension(action.target)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/tiles/revoke_all")
    async def tiles_revoke_all(request: Request) -> dict:
        require_user(request)
        try:
            return {"revoked": await engine.tiles.revoke_all()}
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/attribution/summary")
    def attribution_summary(request: Request, seconds: int = 3600) -> dict:
        require_user(request)
        seconds = max(60, min(seconds, MAX_QUERY_WINDOW_SECONDS))
        engine.flush_rollups()  # fold in anything pending so short windows are fresh
        return attribution_queries.summary(db, seconds)

    @app.get("/api/attribution/redundant")
    def attribution_redundant(request: Request, seconds: int = 3600) -> dict:
        require_user(request)
        seconds = max(60, min(seconds, MAX_QUERY_WINDOW_SECONDS))
        engine.flush_rollups()
        return {"redundant": attribution_queries.redundant(db, seconds)}

    @app.websocket("/api/ws/fleet")
    async def ws_fleet(websocket: WebSocket) -> None:
        token = websocket.cookies.get(SESSION_COOKIE)
        user = auth.resolve_session(db, token) if token else None
        if user is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(
                    {
                        "ts": time.time(),
                        "broker": engine.ingest_status(),
                        "instances": engine.registry.snapshot(),
                        "rates": engine.rates.snapshot(),
                        "latency": engine.probes.latency.snapshot(),
                        "probes": engine.probes.stats(),
                    }
                )
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            pass

    # -- GUI -------------------------------------------------------------------

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
