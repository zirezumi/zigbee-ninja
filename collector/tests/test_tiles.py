import asyncio
import json

from zigbee_ninja.store.db import Database
from zigbee_ninja.tiles import TileManager, probe_code, probe_version


class FakePublisher:
    def __init__(self, responder=None):
        self.published: list[tuple[str, str]] = []
        self._responder = responder

    async def __call__(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))
        if self._responder is not None:
            self._responder(topic, payload)


def test_probe_asset_is_bundled():
    code = probe_code()
    assert "module.exports" in code
    assert "zigbee-ninja/probe/heartbeat" in code
    assert probe_version() != "unknown"


def test_deploy_success_flow(tmp_path):
    db = Database(tmp_path)

    manager = TileManager(db, publisher=None)  # publisher set below

    def responder(topic: str, payload: str) -> None:
        assert topic == "z2m-test/bridge/request/extension/save"
        body = json.loads(payload)
        assert body["name"] == "zigbee-ninja-probe.js"
        assert "module.exports" in body["code"]
        manager.on_bridge_response(
            "z2m-test",
            "save",
            json.dumps({"status": "ok", "transaction": body["transaction"]}).encode(),
        )

    publisher = FakePublisher(responder)
    manager._publish = publisher

    tile = asyncio.run(manager.deploy_extension("z2m-test"))
    assert tile["status"] == "deployed"
    assert tile["version"] == probe_version()
    assert len(publisher.published) == 1


def test_deploy_error_response(tmp_path):
    db = Database(tmp_path)
    manager = TileManager(db, publisher=None)

    def responder(topic: str, payload: str) -> None:
        body = json.loads(payload)
        manager.on_bridge_response(
            "z2m-test",
            "save",
            json.dumps(
                {"status": "error", "error": "nope", "transaction": body["transaction"]}
            ).encode(),
        )

    manager._publish = FakePublisher(responder)
    tile = asyncio.run(manager.deploy_extension("z2m-test"))
    assert tile["status"] == "error"
    assert "nope" in tile["detail"]


def test_revoke_flow_and_revoke_all(tmp_path):
    db = Database(tmp_path)
    manager = TileManager(db, publisher=None)

    def ok_responder(topic: str, payload: str) -> None:
        action = topic.rsplit("/", 1)[-1]
        body = json.loads(payload)
        manager.on_bridge_response(
            "z2m-test",
            action,
            json.dumps({"status": "ok", "transaction": body["transaction"]}).encode(),
        )

    manager._publish = FakePublisher(ok_responder)
    asyncio.run(manager.deploy_extension("z2m-test"))
    results = asyncio.run(manager.revoke_all())
    assert results and results[0]["status"] == "revoked"

    tiles = manager.list(bases=["z2m-test"], probe_stats={})
    assert extension_tile(tiles, "z2m-test")["status"] == "revoked"


def extension_tile(tiles: list[dict], target: str) -> dict:
    return next(
        t for t in tiles if t["capability"] == "z2m_extension" and t["target"] == target
    )


def test_list_synthesizes_available_and_health(tmp_path):
    db = Database(tmp_path)
    clock = lambda: 2000.0  # noqa: E731
    manager = TileManager(db, publisher=FakePublisher(), clock=clock)

    tiles = manager.list(bases=["z2m-a", "z2m-b"], probe_stats={})
    assert {t["target"] for t in tiles} == {"z2m-a", "z2m-b"}
    assert {t["capability"] for t in tiles} == {
        "z2m_extension",
        "topology_pull",
        "mqtt_discovery",
    }
    assert all(t["status"] == "available" for t in tiles)

    manager._upsert("z2m_extension", "z2m-a", status="deployed", version=probe_version())
    fresh = manager.list(
        bases=["z2m-a"],
        probe_stats={"z2m-a": {"last_heartbeat_at": 1990.0, "version": probe_version()}},
    )
    tile_a = extension_tile(fresh, "z2m-a")
    assert tile_a["health"] == "ok"
    assert tile_a["drift"] is False

    stale = manager.list(bases=["z2m-a"], probe_stats={"z2m-a": {"last_heartbeat_at": 1000.0}})
    assert extension_tile(stale, "z2m-a")["health"] == "stale"


def test_heartbeat_promotes_status(tmp_path):
    db = Database(tmp_path)
    manager = TileManager(db, publisher=FakePublisher())
    manager._upsert("z2m_extension", "z2m-test", status="error", detail="timeout")
    manager.on_heartbeat("z2m-test", {"version": "0.3.0"})
    tiles = manager.list(bases=["z2m-test"], probe_stats={})
    assert extension_tile(tiles, "z2m-test")["status"] == "deployed"
    assert extension_tile(tiles, "z2m-test")["version"] == "0.3.0"
