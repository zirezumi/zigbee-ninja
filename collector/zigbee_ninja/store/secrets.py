"""Secrets-at-rest encryption (DESIGN.md §15).

The broker password and HA token are encrypted with Fernet under a key
created on first boot inside the data volume (mode 0600). Stored ciphertext
carries an ``enc:`` marker so plaintext rows written before this landed are
recognized and upgraded in place at startup — the upgrade is idempotent.

Threat model, stated honestly: the key lives beside the database, so a
compromise of the data volume is a compromise of the secrets. What this
protects against is narrower — casual inspection of the SQLite file, backups
or exports of the database alone, and secrets leaking through settings dumps.
A passphrase-locked mode (key derived from operator input, not stored) is a
later hardening step.

A ciphertext that no longer decrypts (key file replaced or corrupted)
resolves to ``None`` rather than raising: the dependent link then fails to
connect and surfaces in its status, and re-entering the secret in the GUI
repairs it.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_FILE_NAME = "secret.key"
_PREFIX = "enc:"


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


class SecretBox:
    def __init__(self, data_dir: Path | str):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / KEY_FILE_NAME
        if path.exists():
            key = path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, key)
            finally:
                os.close(descriptor)
        os.chmod(path, 0o600)
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        return _PREFIX + self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str | None) -> str | None:
        """Ciphertext → plaintext; plaintext passes through untouched (rows
        that predate encryption keep working until the startup upgrade)."""
        if not is_encrypted(value):
            return value
        assert isinstance(value, str)
        try:
            return self._fernet.decrypt(value[len(_PREFIX) :].encode()).decode()
        except InvalidToken:
            return None
