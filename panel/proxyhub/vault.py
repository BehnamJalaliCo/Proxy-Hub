"""Secret vault: tokens (Hetzner, later Telegram/Cloudflare) encrypted at rest in table ``secrets``.

* AES-256-GCM (``cryptography``), a fresh random 96-bit nonce per write.
* The key is the panel master key: 64 hex characters in ``RuntimeConfig.master_key_file``
  (``/run/secrets/master_key``), never in the database, the repo or an environment variable.
* The secret's name and the key version are bound to the ciphertext as associated data, so a
  ciphertext copied into another row (or decrypted with another key) fails authentication.
* Values come back wrapped in :class:`SecretValue`, whose ``repr``/``str`` never show the value,
  so an accidental log line or f-string prints ``***`` (CLAUDE.md rule 3). Nothing in this module
  logs, and no API ever returns a vault value.

Master-key rotation (re-encrypting every row under ``key_version + 1``) is a later, separate step;
``key_version`` is stored now so that rotation needs no schema change.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.config import SecretFileError, get_config, read_secret_file
from proxyhub.db.models_fleet import VaultSecret

KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
CURRENT_KEY_VERSION: Final = 1
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9:_.\-]{0,199}$")
_HEX_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")

Db = AsyncConnection | AsyncSession


class VaultError(RuntimeError):
    """Base class; messages never contain a secret value or key material."""


class VaultKeyError(VaultError):
    """The master key file is missing, unsafe or malformed."""


class VaultDecryptError(VaultError):
    """A stored value did not authenticate (wrong key, tampered row, or row copied to another name)."""


class SecretValue:
    """A secret that refuses to show itself. Call :meth:`reveal` only right where it is used."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("a secret value must not be empty")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue('***')"

    def __str__(self) -> str:
        return "***"

    def __reduce__(self) -> tuple[object, ...]:  # never pickled into caches or queues
        raise TypeError("SecretValue cannot be pickled")


def _check_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise VaultError("invalid secret name")
    return name


def _aad(name: str, key_version: int) -> bytes:
    return f"proxyhub-vault|v{key_version}|{name}".encode()


class Vault:
    def __init__(self, key: bytes, *, key_version: int = CURRENT_KEY_VERSION) -> None:
        if len(key) != KEY_BYTES:
            raise VaultKeyError("master key must be 32 bytes")
        self._aead = AESGCM(key)
        self.key_version = key_version

    def __repr__(self) -> str:
        return f"Vault(key_version={self.key_version})"

    @classmethod
    def from_key_file(cls, path: Path) -> Vault:
        try:
            text = read_secret_file(path)
        except SecretFileError as exc:
            raise VaultKeyError(str(exc)) from None
        if not _HEX_KEY_RE.fullmatch(text):
            raise VaultKeyError(f"master key file must hold exactly 64 hex characters: {path}")
        return cls(bytes.fromhex(text))

    @classmethod
    def from_config(cls) -> Vault:
        return cls.from_key_file(get_config().master_key_file)

    # --- pure crypto ---------------------------------------------------------------------------

    def encrypt(self, name: str, value: str) -> tuple[bytes, bytes]:
        """Returns (nonce, ciphertext+tag)."""
        _check_name(name)
        if not value:
            raise VaultError("refusing to store an empty secret")
        nonce = os.urandom(NONCE_BYTES)
        return nonce, self._aead.encrypt(nonce, value.encode(), _aad(name, self.key_version))

    def decrypt(self, name: str, nonce: bytes, ciphertext: bytes, key_version: int) -> SecretValue:
        if key_version != self.key_version:
            raise VaultDecryptError(
                f"secret {name!r} uses key version {key_version}, vault has {self.key_version}"
            )
        try:
            plain = self._aead.decrypt(nonce, ciphertext, _aad(name, key_version))
        except InvalidTag:
            raise VaultDecryptError(f"secret {name!r} failed authentication") from None
        return SecretValue(plain.decode())

    # --- storage -------------------------------------------------------------------------------

    async def put(self, db: Db, name: str, value: str | SecretValue) -> None:
        """Insert or replace. Replacing sets ``rotated_at``."""
        plain = value.reveal() if isinstance(value, SecretValue) else value
        nonce, ciphertext = self.encrypt(name, plain)
        stmt = insert(VaultSecret).values(
            name=name, ciphertext=ciphertext, nonce=nonce, key_version=self.key_version
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[VaultSecret.name],
            set_={
                "ciphertext": stmt.excluded.ciphertext,
                "nonce": stmt.excluded.nonce,
                "key_version": stmt.excluded.key_version,
                "rotated_at": func.now(),
            },
        )
        await db.execute(stmt)

    async def get(self, db: Db, name: str) -> SecretValue | None:
        row = (
            await db.execute(
                select(VaultSecret.nonce, VaultSecret.ciphertext, VaultSecret.key_version).where(
                    VaultSecret.name == _check_name(name)
                )
            )
        ).first()
        if row is None:
            return None
        return self.decrypt(name, bytes(row.nonce), bytes(row.ciphertext), int(row.key_version))

    async def delete(self, db: Db, name: str) -> bool:
        return await delete_secret(db, name)


async def delete_secret(db: Db, name: str) -> bool:
    """Remove a secret (no key needed). Returns True if it existed."""
    result = await db.execute(
        delete(VaultSecret).where(VaultSecret.name == _check_name(name)).returning(VaultSecret.id)
    )
    return result.first() is not None


async def secret_exists(db: Db, name: str) -> bool:
    """True if a secret with this name is stored (no key needed, nothing decrypted)."""
    row = (await db.execute(select(VaultSecret.id).where(VaultSecret.name == _check_name(name)))).first()
    return row is not None


def hcloud_secret_name(account_id: object) -> str:
    """Vault name of a Hetzner account's token."""
    return f"hcloud:{account_id}"
