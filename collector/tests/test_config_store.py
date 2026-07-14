from zigbee_ninja.store.config import ConfigStore
from zigbee_ninja.store.db import _MIGRATIONS, Database


def test_config_roundtrip_and_overwrite(tmp_path):
    config = ConfigStore(Database(tmp_path))
    assert config.get("broker") is None
    assert config.get("broker", {"host": None}) == {"host": None}

    config.set("broker", {"host": "mqtt.local", "port": 1883})
    assert config.get("broker") == {"host": "mqtt.local", "port": 1883}

    config.set("broker", {"host": "mqtt.local", "port": 8883})
    assert config.get("broker")["port"] == 8883

    config.set("retention_gb", 8)
    assert config.all() == {
        "broker": {"host": "mqtt.local", "port": 8883},
        "retention_gb": 8,
    }

    config.delete("broker")
    assert config.get("broker") is None


def test_migrations_are_idempotent(tmp_path):
    Database(tmp_path)
    db2 = Database(tmp_path)  # re-open same directory: migrations must not re-run
    row = db2.connect().execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == len(_MIGRATIONS)
