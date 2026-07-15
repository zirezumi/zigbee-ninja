"""Secrets-at-rest: key handling, round trips, startup upgrade (DESIGN.md §15)."""

import asyncio
import os

from zigbee_ninja.ingest.engine import Engine
from zigbee_ninja.store.config import ConfigStore
from zigbee_ninja.store.db import Database
from zigbee_ninja.store.secrets import KEY_FILE_NAME, SecretBox, is_encrypted


def test_round_trip_and_marker(tmp_path):
    box = SecretBox(tmp_path)
    token = box.encrypt("hunter2")
    assert is_encrypted(token)
    assert token != "hunter2"
    assert box.decrypt(token) == "hunter2"


def test_key_file_created_once_with_owner_only_mode(tmp_path):
    SecretBox(tmp_path)
    path = tmp_path / KEY_FILE_NAME
    key = path.read_bytes()
    assert (path.stat().st_mode & 0o777) == 0o600
    # A second box reuses the same key: earlier ciphertext stays readable.
    box = SecretBox(tmp_path)
    assert path.read_bytes() == key
    assert box.decrypt(box.encrypt("x")) == "x"


def test_plaintext_passes_through_decrypt(tmp_path):
    box = SecretBox(tmp_path)
    assert box.decrypt("legacy-plaintext") == "legacy-plaintext"
    assert box.decrypt(None) is None


def test_foreign_ciphertext_resolves_to_none(tmp_path):
    token = SecretBox(tmp_path / "a").encrypt("secret")
    other = SecretBox(tmp_path / "b")
    assert other.decrypt(token) is None


def test_engine_upgrades_plaintext_secrets_in_place(tmp_path):
    db = Database(tmp_path)
    config = ConfigStore(db)
    config.set("broker", {"host": "broker.local", "port": 1883,
                          "username": "zn", "password": "plain-pw"})
    config.set("ha", {"url": "http://ha.local:8123", "token": "plain-token"})

    engine = Engine(db, config, SecretBox(tmp_path))

    stored_broker = config.get("broker")
    stored_ha = config.get("ha")
    assert is_encrypted(stored_broker["password"])
    assert is_encrypted(stored_ha["token"])
    assert engine.broker_config().password == "plain-pw"
    assert engine.ha_config().token == "plain-token"

    # Idempotent: a second engine must not double-encrypt.
    Engine(db, config, SecretBox(tmp_path))
    assert config.get("broker")["password"] == stored_broker["password"]


def test_apply_paths_store_ciphertext_and_read_back(tmp_path):
    db = Database(tmp_path)
    config = ConfigStore(db)
    engine = Engine(db, config, SecretBox(tmp_path))

    async def scenario():
        await engine.apply_broker_config(
            {"host": "broker.local", "port": 1883, "username": "zn", "password": "pw2"}
        )
        await engine.stop()

    asyncio.run(scenario())
    assert is_encrypted(config.get("broker")["password"])
    assert engine.broker_config().password == "pw2"
    # The API view never carries the secret in any form.
    assert "password" not in engine.broker_config().public_dict()


def test_key_file_mode_repaired_if_loosened(tmp_path):
    SecretBox(tmp_path)
    path = tmp_path / KEY_FILE_NAME
    os.chmod(path, 0o644)
    SecretBox(tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600
