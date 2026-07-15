"""FastAPI application factory: health, auth, broker config, fleet, GUI serving."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..attribution import queries as attribution_queries
from ..capacity import airtime as airtime_model
from ..ingest.engine import Engine
from ..ingest.hacontrol import HaConfig, test_ha
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


class HaSettings(BaseModel):
    url: str
    token: str


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

    @app.get("/api/airtime")
    def airtime_series(request: Request, seconds: int = 3600) -> dict:
        require_user(request)
        seconds = max(60, min(seconds, MAX_QUERY_WINDOW_SECONDS))
        engine.flush_rollups()
        since = int(time.time()) - seconds
        rows = db.connect().execute(
            "SELECT instance, bucket, SUM(airtime_us) AS airtime_us, SUM(frames) AS frames "
            "FROM airtime_10s WHERE ts >= ? GROUP BY instance, bucket",
            (since,),
        ).fetchall()
        instances: dict = {}
        for row in rows:
            view = instances.setdefault(row["instance"], {"buckets": {}})
            view["buckets"][row["bucket"]] = {
                "airtime_us": row["airtime_us"],
                "frames": row["frames"],
            }
        for view in instances.values():
            us_per_s = sum(b["airtime_us"] for b in view["buckets"].values()) / seconds
            view["us_per_s"] = round(us_per_s, 1)
            view["airtime_pct"] = round(us_per_s / 1_000_000.0 * 100.0, 3)
            view["budget_pct"] = round(
                us_per_s / airtime_model.CHANNEL_BUDGET_US_PER_S * 100.0, 3
            )
            view["provenance"] = airtime_model.PROVENANCE
        return {"window_seconds": seconds, "instances": instances}

    @app.get("/api/latency")
    def latency_series(request: Request, seconds: int = 3600) -> dict:
        """Wire-latency 10 s rollups — the latency-vs-load axis for continuous
        knee validation (DESIGN.md §10)."""
        require_user(request)
        seconds = max(60, min(seconds, MAX_QUERY_WINDOW_SECONDS))
        engine.flush_rollups()
        since = int(time.time()) - seconds
        rows = db.connect().execute(
            "SELECT ts, instance, count, p50_ms, p95_ms, max_ms FROM latency_10s "
            "WHERE ts >= ? ORDER BY ts",
            (since,),
        ).fetchall()
        return {
            "window_seconds": seconds,
            "rows": [dict(row) for row in rows],
        }

    @app.get("/api/ha")
    def ha_get(request: Request) -> dict:
        require_user(request)
        stored = engine.ha_config()
        view: dict = {"configured": stored is not None, "status": engine.ha_status()}
        if stored is not None:
            view.update(stored.public_dict())  # token never echoed
        return view

    @app.post("/api/ha")
    async def ha_set(request: Request, settings: HaSettings) -> dict:
        require_user(request)
        candidate = HaConfig(url=settings.url.rstrip("/"), token=settings.token)
        error = await test_ha(candidate)
        if error is not None:
            raise HTTPException(status_code=400, detail=f"HA connection failed: {error}")
        await engine.apply_ha_config(settings.model_dump())
        return {"configured": True, "url": candidate.url, "status": engine.ha_status()}

    @app.get("/api/tap")
    def tap_info(request: Request) -> dict:
        require_user(request)
        return {
            "token": engine.tap_token(),
            "stats": engine.tap.stats(),
        }

    @app.websocket("/api/ws/tap")
    async def ws_tap(websocket: WebSocket) -> None:
        # Agent auth: Bearer token in the Authorization header (outbound-only,
        # per-collector token). Not a browser session — this is a capture agent.
        auth_header = websocket.headers.get("authorization", "")
        token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
        if not token or token != engine.tap_token():
            await websocket.close(code=4401)
            return
        await websocket.accept()
        agent_id = f"{websocket.client.host}:{websocket.client.port}"
        registered = False
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is not None:  # JSON hello frame
                    try:
                        meta = json.loads(text)
                    except ValueError:
                        meta = {}
                    if meta.get("type") == "hello":
                        engine.tap.register_agent(agent_id, meta)
                        registered = True
                    continue
                data = message.get("bytes")
                if data:
                    if not registered:
                        engine.tap.register_agent(agent_id, {})
                        registered = True
                    engine.tap.feed(agent_id, data)
        except WebSocketDisconnect:
            pass
        finally:
            engine.tap.drop_agent(agent_id)

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
                        "tap": engine.tap.stats(),
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
