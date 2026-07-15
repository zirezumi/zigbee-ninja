"""Permission tiles: grant/deploy/revoke lifecycle + footprint (DESIGN.md §6).

M3 implements the first tile — the per-instance Z2M extension probe, deployed
and removed entirely over MQTT (`bridge/request/extension/save|remove`) with
transaction-correlated responses. Every deployed artifact is version-stamped;
health is computed from probe heartbeats at read time.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from importlib import resources

from .store.db import Database

CAPABILITY_Z2M_EXTENSION = "z2m_extension"
# Topology pulls are ACTIVE mesh operations (Mgmt_Lqi/Mgmt_Rtg sweeps): the
# tile is a pure grant — nothing is installed anywhere — and the puller
# enforces its own rate limit on top (DESIGN.md §6).
CAPABILITY_TOPOLOGY = "topology_pull"
GRANT_CAPABILITIES = (CAPABILITY_TOPOLOGY,)
PROBE_FILE_NAME = "zigbee-ninja-probe.js"
RESPONSE_TIMEOUT_SECONDS = 10.0
HEALTH_STALE_SECONDS = 60.0

_VERSION_RE = re.compile(r'PROBE_VERSION\s*=\s*"([^"]+)"')


def probe_code() -> str:
    return (
        resources.files("zigbee_ninja")
        .joinpath("probe_assets")
        .joinpath(PROBE_FILE_NAME)
        .read_text(encoding="utf-8")
    )


def probe_version() -> str:
    match = _VERSION_RE.search(probe_code())
    return match.group(1) if match else "unknown"


class TileManager:
    def __init__(
        self,
        db: Database,
        publisher: Callable[[str, str], Awaitable[None]],
        clock: Callable[[], float] = time.time,
    ):
        self._db = db
        self._publish = publisher
        self._clock = clock
        self._pending: dict[str, asyncio.Future] = {}

    # -- persistence -----------------------------------------------------------

    def _upsert(self, capability: str, target: str, **fields) -> None:
        conn = self._db.connect()
        row = conn.execute(
            "SELECT status FROM tiles WHERE capability = ? AND target = ?",
            (capability, target),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO tiles (capability, target, status) VALUES (?, ?, 'available')",
                (capability, target),
            )
        if fields:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            conn.execute(
                f"UPDATE tiles SET {assignments} WHERE capability = ? AND target = ?",
                (*fields.values(), capability, target),
            )
        conn.commit()

    def _row(self, capability: str, target: str) -> dict | None:
        row = (
            self._db.connect()
            .execute(
                "SELECT * FROM tiles WHERE capability = ? AND target = ?",
                (capability, target),
            )
            .fetchone()
        )
        return dict(row) if row else None

    # -- read side --------------------------------------------------------------

    def list(self, bases: list[str], probe_stats: dict[str, dict]) -> list[dict]:
        conn = self._db.connect()
        stored = {
            (row["capability"], row["target"]): dict(row)
            for row in conn.execute("SELECT * FROM tiles")
        }
        for base in bases:
            for capability in (CAPABILITY_Z2M_EXTENSION, *GRANT_CAPABILITIES):
                key = (capability, base)
                if key not in stored:
                    stored[key] = {
                        "capability": capability,
                        "target": base,
                        "status": "available",
                        "granted_at": None,
                        "deployed_at": None,
                        "revoked_at": None,
                        "version": None,
                        "last_health_at": None,
                        "detail": None,
                    }

        now = self._clock()
        bundled = probe_version()
        tiles = []
        for tile in stored.values():
            tile = dict(tile)
            stats = probe_stats.get(tile["target"], {})
            heartbeat_at = stats.get("last_heartbeat_at")
            tile["health"] = None
            tile["drift"] = False
            if tile["status"] == "deployed":
                if heartbeat_at is None or now - heartbeat_at > HEALTH_STALE_SECONDS:
                    tile["health"] = "stale"
                else:
                    tile["health"] = "ok"
                    reported = stats.get("version")
                    tile["drift"] = bool(reported) and reported != bundled
            tile["probe"] = {
                "version": stats.get("version"),
                "hooks": stats.get("hooks", []),
                "counters": stats.get("counters", {}),
                "seq_gaps": stats.get("seq_gaps", 0),
                "last_heartbeat_at": heartbeat_at,
            }
            tile["bundled_version"] = bundled
            tiles.append(tile)
        return sorted(tiles, key=lambda t: (t["capability"], t["target"]))

    # -- bridge response plumbing ------------------------------------------------

    def on_bridge_response(self, base: str, action: str, payload: bytes) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return
        transaction = data.get("transaction")
        future = self._pending.pop(transaction, None) if transaction else None
        if future is None:
            future = self._pending.pop(f"{base}:{action}", None)
        if future is not None and not future.done():
            future.set_result(data)

    async def _request(self, base: str, action: str, payload: dict) -> dict:
        transaction = secrets.token_hex(8)
        payload = {**payload, "transaction": transaction}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[transaction] = future
        self._pending[f"{base}:{action}"] = future
        try:
            await self._publish(
                f"{base}/bridge/request/extension/{action}", json.dumps(payload)
            )
            return await asyncio.wait_for(future, RESPONSE_TIMEOUT_SECONDS)
        finally:
            self._pending.pop(transaction, None)
            self._pending.pop(f"{base}:{action}", None)

    # -- pure grants (no remote artifact) ------------------------------------------

    def grant(self, capability: str, base: str) -> dict:
        self._upsert(capability, base, status="granted", granted_at=self._clock(), detail=None)
        return self._row(capability, base) or {}

    def revoke_grant(self, capability: str, base: str) -> dict:
        self._upsert(capability, base, status="revoked", revoked_at=self._clock(), detail=None)
        return self._row(capability, base) or {}

    def is_granted(self, capability: str, base: str) -> bool:
        row = self._row(capability, base)
        return bool(row) and row["status"] == "granted"

    # -- lifecycle actions ---------------------------------------------------------

    async def deploy_extension(self, base: str) -> dict:
        now = self._clock()
        self._upsert(
            CAPABILITY_Z2M_EXTENSION, base, status="deploying", granted_at=now, detail=None
        )
        try:
            response = await self._request(
                base, "save", {"name": PROBE_FILE_NAME, "code": probe_code()}
            )
        except TimeoutError:
            self._upsert(
                CAPABILITY_Z2M_EXTENSION,
                base,
                status="error",
                detail="No response from Zigbee2MQTT within 10s",
            )
            return self._row(CAPABILITY_Z2M_EXTENSION, base) or {}

        if response.get("status") == "ok":
            self._upsert(
                CAPABILITY_Z2M_EXTENSION,
                base,
                status="deployed",
                deployed_at=self._clock(),
                version=probe_version(),
                detail=None,
            )
        else:
            self._upsert(
                CAPABILITY_Z2M_EXTENSION,
                base,
                status="error",
                detail=str(response.get("error") or "extension save rejected"),
            )
        return self._row(CAPABILITY_Z2M_EXTENSION, base) or {}

    async def revoke_extension(self, base: str) -> dict:
        try:
            response = await self._request(base, "remove", {"name": PROBE_FILE_NAME})
            accepted = response.get("status") == "ok" or "exist" in str(
                response.get("error", "")
            )
        except TimeoutError:
            accepted = False
        if accepted:
            self._upsert(
                CAPABILITY_Z2M_EXTENSION,
                base,
                status="revoked",
                revoked_at=self._clock(),
                detail=None,
            )
        else:
            self._upsert(
                CAPABILITY_Z2M_EXTENSION,
                base,
                status="error",
                detail="Revoke not confirmed by Zigbee2MQTT",
            )
        return self._row(CAPABILITY_Z2M_EXTENSION, base) or {}

    async def revoke_all(self) -> list[dict]:
        conn = self._db.connect()
        targets = [
            row["target"]
            for row in conn.execute(
                "SELECT target FROM tiles WHERE capability = ? AND status IN "
                "('deployed', 'deploying', 'error')",
                (CAPABILITY_Z2M_EXTENSION,),
            )
        ]
        results = []
        for target in targets:
            results.append(await self.revoke_extension(target))
        # Emergency stop also drops pure grants (active-operation permissions).
        granted = [
            (row["capability"], row["target"])
            for row in conn.execute(
                "SELECT capability, target FROM tiles WHERE status = 'granted'"
            )
        ]
        for capability, target in granted:
            results.append(self.revoke_grant(capability, target))
        return results

    def on_heartbeat(self, base: str, heartbeat: dict) -> None:
        row = self._row(CAPABILITY_Z2M_EXTENSION, base)
        if row is None or row["status"] not in ("deployed", "error", "deploying"):
            return
        fields = {"last_health_at": self._clock()}
        if heartbeat.get("version"):
            fields["version"] = heartbeat["version"]
        # A live heartbeat proves the probe is running regardless of what the
        # deploy handshake concluded (e.g. response lost, collector restarted).
        if row["status"] != "deployed":
            fields["status"] = "deployed"
            fields["deployed_at"] = self._clock()
        self._upsert(CAPABILITY_Z2M_EXTENSION, base, **fields)
