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
from .. import alerts as alerts_module
from .. import tiles as tiles_module
from ..attribution import queries as attribution_queries
from ..calibration.benchmark import CalibrationRejected
from ..capacity import airtime as airtime_model
from ..capacity import headroom as headroom_model
from ..ingest import topology as topology_module
from ..ingest.engine import Engine
from ..ingest.hacontrol import HaConfig, test_ha
from ..ingest.mqtt import BrokerConfig, test_connection
from ..store.config import ConfigStore
from ..store.db import Database
from ..store.events import RawEventLog
from ..store.secrets import SecretBox
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


class CalibrationPreviewRequest(BaseModel):
    instance: str
    target: str | None = None
    mode: str = "single"  # single | spread
    count: int = 6  # spread only: how many routers to auto-pick
    targets: list[str] | None = None  # spread only: explicit roster override


class CalibrationRunRequest(BaseModel):
    instance: str
    target: str | None = None  # required for single, ignored for spread
    authorization: str


class CalibrationBulkPreviewRequest(BaseModel):
    instances: list[str] | None = None
    targets: dict[str, str] | None = None


class CalibrationBulkRunRequest(BaseModel):
    authorization: str


class AlertRuleBody(BaseModel):
    name: str
    metric: str
    instance: str = "*"
    op: str = ">"
    threshold: float
    clear_threshold: float | None = None
    sustain_seconds: int = 60
    severity: str = "warning"
    enabled: bool = True


class SettingsBody(BaseModel):
    retention_rollup_days: int | None = None
    retention_chains_hours: int | None = None
    retention_topology_snapshots: int | None = None
    raw_event_quota_mb: int | None = None
    raw_event_horizon_hours: int | None = None
    client_labels: dict[str, str] | None = None


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
    engine = Engine(db, config, SecretBox(data_dir), RawEventLog(data_dir))

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
        if action.capability == "z2m_extension":
            try:
                return await engine.tiles.deploy_extension(action.target)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if action.capability in tiles_module.GRANT_CAPABILITIES:
            return engine.tiles.grant(action.capability, action.target)
        raise HTTPException(status_code=400, detail="Unknown tile capability")

    @app.post("/api/tiles/revoke")
    async def tiles_revoke(request: Request, action: TileAction) -> dict:
        require_user(request)
        if action.capability == "z2m_extension":
            try:
                return await engine.tiles.revoke_extension(action.target)
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if action.capability in tiles_module.GRANT_CAPABILITIES:
            result = engine.tiles.revoke_grant(action.capability, action.target)
            if action.capability == tiles_module.CAPABILITY_MQTT_DISCOVERY:
                try:
                    await engine.discovery.revoke_cleanup(action.target)
                except RuntimeError:
                    pass  # broker offline; the publish loop's sweep finishes it
            return result
        raise HTTPException(status_code=400, detail="Unknown tile capability")

    @app.post("/api/tiles/revoke_all")
    async def tiles_revoke_all(request: Request) -> dict:
        require_user(request)
        try:
            revoked = await engine.tiles.revoke_all()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            await engine.discovery.cleanup_revoked()
        except RuntimeError:
            pass  # broker offline; the publish loop's sweep finishes it
        return {"revoked": revoked}

    def _label_clients(rows: list[dict]) -> list[dict]:
        labels = engine.runtime_settings()["client_labels"]
        if labels:
            for row in rows:
                label = labels.get(row.get("client") or "")
                if label:
                    row["label"] = label
        return rows

    @app.get("/api/attribution/summary")
    def attribution_summary(request: Request, seconds: int = 3600) -> dict:
        require_user(request)
        seconds = max(60, min(seconds, MAX_QUERY_WINDOW_SECONDS))
        engine.flush_rollups()  # fold in anything pending so short windows are fresh
        summary = attribution_queries.summary(db, seconds)
        _label_clients(summary.get("top_clients") or [])
        return summary

    @app.get("/api/attribution/redundant")
    def attribution_redundant(request: Request, seconds: int = 3600) -> dict:
        require_user(request)
        seconds = max(60, min(seconds, MAX_QUERY_WINDOW_SECONDS))
        engine.flush_rollups()
        return {"redundant": _label_clients(attribution_queries.redundant(db, seconds))}

    @app.get("/api/settings")
    def settings_get(request: Request) -> dict:
        require_user(request)
        return engine.runtime_settings()

    @app.post("/api/settings")
    def settings_set(request: Request, body: SettingsBody) -> dict:
        require_user(request)
        try:
            return engine.apply_settings(body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    @app.get("/api/headroom")
    def headroom_view(request: Request, seconds: int = 21600) -> dict:
        """Utilization per denominator, steady/burst headroom, and the
        latency-vs-load scatter for continuous knee validation (§10)."""
        require_user(request)
        seconds = max(600, min(seconds, MAX_QUERY_WINDOW_SECONDS))
        engine.flush_rollups()
        return headroom_model.summarize(db, seconds, engine.registry.snapshot())

    @app.get("/api/topology")
    def topology_get(request: Request, instance: str | None = None, full: bool = False) -> dict:
        require_user(request)
        return {
            "instances": engine.topology.latest(instance, include_raw=full),
        }

    @app.get("/api/topology/graph")
    def topology_graph(request: Request, instance: str) -> dict:
        require_user(request)
        entry = engine.topology.latest(instance, include_raw=True).get(instance)
        if entry is None:
            raise HTTPException(
                status_code=404, detail="No topology snapshot for this instance"
            )
        return {
            "instance": instance,
            "pulled_at": entry["pulled_at"],
            **topology_module.graph(entry["raw"]),
        }

    @app.post("/api/topology/pull")
    async def topology_pull(request: Request, action: TileAction) -> dict:
        require_user(request)
        if action.capability != tiles_module.CAPABILITY_TOPOLOGY:
            raise HTTPException(status_code=400, detail="Unknown capability")
        try:
            return await engine.topology.pull(action.target)
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="Zigbee2MQTT did not answer the networkmap request in time",
            ) from exc
        except RuntimeError as exc:  # PullRejected, broker-unconfigured, ...
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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

    # -- calibration wizard (DESIGN.md §11): per-run authorized, never a grant --

    @app.get("/api/calibration")
    def calibration_view(request: Request) -> dict:
        require_user(request)
        return engine.calibration.view()

    @app.get("/api/calibration/candidates")
    def calibration_candidates(request: Request, instance: str) -> dict:
        require_user(request)
        try:
            return engine.calibration.candidates(instance)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/calibration/preview")
    def calibration_preview(request: Request, settings: CalibrationPreviewRequest) -> dict:
        """Dry run: the exact traffic, schedule, caps, and stop rules, plus the
        single-use authorization token — nothing transmits."""
        require_user(request)
        try:
            if settings.mode == "spread":
                return engine.calibration.preview_spread(
                    settings.instance, settings.count, settings.targets
                )
            if not settings.target:
                raise ValueError("target is required for a single-target preview")
            return engine.calibration.preview(settings.instance, settings.target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/calibration/run", status_code=202)
    async def calibration_run(request: Request, settings: CalibrationRunRequest) -> dict:
        require_user(request)
        try:
            return await engine.calibration.start(
                settings.instance, settings.target or "", settings.authorization
            )
        except CalibrationRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/calibration/bulk/preview")
    def calibration_bulk_preview(
        request: Request, settings: CalibrationBulkPreviewRequest
    ) -> dict:
        """Dry run for an enumerated batch: one run per instance (top-ranked
        eligible router, unless pinned via targets), one single-use token."""
        require_user(request)
        try:
            return engine.calibration.preview_bulk(settings.instances, settings.targets)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/calibration/bulk/run", status_code=202)
    async def calibration_bulk_run(
        request: Request, settings: CalibrationBulkRunRequest
    ) -> dict:
        require_user(request)
        try:
            return await engine.calibration.start_bulk(settings.authorization)
        except CalibrationRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/calibration/abort")
    def calibration_abort(request: Request) -> dict:
        require_user(request)
        try:
            return engine.calibration.abort()
        except CalibrationRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # -- alerting (DESIGN.md §14) ------------------------------------------------

    @app.get("/api/alerts")
    def alerts_view(request: Request) -> dict:
        require_user(request)
        return {
            "active": engine.alerts.active(),
            "rules": engine.alerts.rules(),
            "metrics": alerts_module.metric_catalog(),
        }

    @app.get("/api/alerts/history")
    def alerts_history(request: Request, seconds: int = 86400) -> dict:
        require_user(request)
        seconds = max(60, min(seconds, alerts_module.EVENT_RETENTION_SECONDS))
        return {"events": engine.alerts.history(seconds)}

    @app.post("/api/alerts/rules", status_code=201)
    def alerts_rule_create(request: Request, body: AlertRuleBody) -> dict:
        require_user(request)
        try:
            return engine.alerts.create_rule(body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/alerts/rules/{rule_id}")
    def alerts_rule_update(request: Request, rule_id: int, body: AlertRuleBody) -> dict:
        require_user(request)
        try:
            updated = engine.alerts.update_rule(rule_id, body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="No such alert rule")
        return updated

    @app.delete("/api/alerts/rules/{rule_id}")
    def alerts_rule_delete(request: Request, rule_id: int) -> dict:
        require_user(request)
        if not engine.alerts.delete_rule(rule_id):
            raise HTTPException(status_code=404, detail="No such alert rule")
        return {"ok": True}

    # -- burst inspector (DESIGN.md §12, §13 view 5) -------------------------------

    @app.get("/api/burst/timeline")
    def burst_timeline(
        request: Request,
        instance: str,
        seconds: int = 900,
        end: float | None = None,
        bucket_ms: int = 1000,
    ) -> dict:
        require_user(request)
        seconds = max(10, min(seconds, 48 * 3600))
        bucket_ms = max(10, min(bucket_ms, 3_600_000))
        end_ts = end if end is not None else time.time()
        view = engine.events.timeline(instance, end_ts - seconds, end_ts, bucket_ms)
        view["store"] = engine.events.stats()
        return view

    @app.get("/api/burst/events")
    def burst_events(
        request: Request,
        instance: str,
        start: float,
        end: float,
        limit: int = 2000,
    ) -> dict:
        require_user(request)
        limit = max(1, min(limit, 10_000))
        if end <= start or end - start > 48 * 3600:
            raise HTTPException(status_code=400, detail="Invalid window")
        return {"events": engine.events.events(instance, start, end, limit)}

    @app.get("/api/burst/chains")
    def burst_chains(
        request: Request, instance: str, start: float, end: float
    ) -> dict:
        """Command chains inside the window, for the micro-gantt overlay:
        each span runs opened_at → opened_at + first_echo_ms."""
        require_user(request)
        if end <= start or end - start > 48 * 3600:
            raise HTTPException(status_code=400, detail="Invalid window")
        engine.flush_rollups()
        rows = db.connect().execute(
            "SELECT target, verb, opened_at, client, echo_count, first_echo_ms, redundant "
            "FROM chains WHERE instance = ? AND opened_at >= ? AND opened_at < ? "
            "ORDER BY opened_at LIMIT 500",
            (instance, start, end),
        ).fetchall()
        return {"chains": [dict(row) for row in rows]}

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
            "fusion": engine.fusion.snapshot(),
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
                        "fusion": engine.fusion.snapshot(),
                        "alerts": engine.alerts.active_brief(),
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
