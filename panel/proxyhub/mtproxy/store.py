"""Where the operator account's data lives. Nothing here logs.

* ``settings`` row :data:`KEY`: the status (:class:`~proxyhub.mtproxy.state.AccountRow`), no secret.
* Vault :data:`API_SECRET`: ``{"api_id", "api_hash"}`` together; vault :data:`SESSION_SECRET`: the
  Telethon session string. Both AES-GCM with the panel master key (proxyhub/vault.py), written only
  once the login succeeded, removed on logout.
* Redis ``proxyhub:mtproxy:login:<login_id>``: the login in progress (api credentials, phone,
  temporary session string, phone_code_hash), sealed with the master key (AES-GCM, the key name as
  associated data) and kept for :data:`~proxyhub.mtproxy.state.LOGIN_TTL` only. Never sent to the
  browser. The login code and the two-step password are never stored anywhere.
* The PostgreSQL advisory lock :data:`LOCK_NAME`, shared by the API and the worker's check, so only
  one Telegram operation on this account runs at a time.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from proxyhub.db.models import Setting
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings
from proxyhub.mtproxy.state import LOGIN_TTL, AccountRow
from proxyhub.mtproxy.userclient import ApiCredentials
from proxyhub.reconcilers.base import lock_key
from proxyhub.vault import SecretValue, Vault, VaultDecryptError, delete_secret, secret_exists

Db = AsyncConnection | AsyncSession

KEY: Final = "mtproxy.account"
API_SECRET: Final = "mtproxy:api"  # noqa: S105 - vault NAMES, not values
SESSION_SECRET: Final = "mtproxy:session"  # noqa: S105
PENDING_PREFIX: Final = "proxyhub:mtproxy:login:"
LOCK_NAME: Final = "mtproxy_account"


# --- the status row ----------------------------------------------------------------------------------


async def load_row(db: Db, *, for_update: bool = False) -> AccountRow:
    stmt = select(Setting.value).where(Setting.key == KEY)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).first()
    return AccountRow() if row is None else AccountRow.from_json(row[0])


async def save_row(db: Db, row: AccountRow) -> None:
    stmt = insert(Setting).values(key=KEY, value=row.to_json())
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value, "updated_at": text("now()")}
        )
    )


async def kill_switch_on(db: Db) -> bool:
    """The fleet's kill switch, read the way the fleet code reads it; unreadable means ON."""
    try:
        return (await load_fleet_settings(db)).kill_switch
    except SettingsCorrupt:
        return True


# --- vault -------------------------------------------------------------------------------------------


def _creds_json(creds: ApiCredentials) -> str:
    return json.dumps({"api_id": creds.api_id, "api_hash": creds.api_hash.reveal()})


def _creds_from_json(value: str) -> ApiCredentials:
    data = json.loads(value)
    api_id, api_hash = data["api_id"], data["api_hash"]
    if not isinstance(api_id, int) or isinstance(api_id, bool) or not isinstance(api_hash, str):
        raise VaultDecryptError("stored api credentials have the wrong shape")
    return ApiCredentials(api_id=api_id, api_hash=SecretValue(api_hash))


async def save_account_secrets(db: Db, vault: Vault, creds: ApiCredentials, session: SecretValue) -> None:
    await vault.put(db, API_SECRET, _creds_json(creds))
    await vault.put(db, SESSION_SECRET, session)


async def load_creds(db: Db, vault: Vault) -> ApiCredentials | None:
    value = await vault.get(db, API_SECRET)
    if value is None:
        return None
    try:
        return _creds_from_json(value.reveal())
    except (ValueError, KeyError, TypeError):
        raise VaultDecryptError("stored api credentials are unreadable") from None


async def load_session(db: Db, vault: Vault) -> SecretValue | None:
    return await vault.get(db, SESSION_SECRET)


async def has_session(db: Db) -> bool:
    return await secret_exists(db, SESSION_SECRET)


async def drop_secrets(db: Db, *, session_only: bool = False) -> None:
    await delete_secret(db, SESSION_SECRET)
    if not session_only:
        await delete_secret(db, API_SECRET)


# --- the login in progress (Redis, sealed) -------------------------------------------------------------


@dataclass(frozen=True)
class PendingLogin:
    login_id: str
    creds: ApiCredentials
    phone: SecretValue
    session: SecretValue
    phone_code_hash: SecretValue

    def __repr__(self) -> str:
        return f"PendingLogin(login_id={self.login_id!r}, ***)"


def _pending_name(login_id: str) -> str:
    return f"redis:mtproxy-login:{login_id}"


def seal_pending(vault: Vault, pending: PendingLogin) -> str:
    plain = json.dumps(
        {
            "api_id": pending.creds.api_id,
            "api_hash": pending.creds.api_hash.reveal(),
            "phone": pending.phone.reveal(),
            "session": pending.session.reveal(),
            "phone_code_hash": pending.phone_code_hash.reveal(),
        }
    )
    nonce, ciphertext = vault.encrypt(_pending_name(pending.login_id), plain)
    return json.dumps(
        {
            "kv": vault.key_version,
            "n": base64.b64encode(nonce).decode(),
            "c": base64.b64encode(ciphertext).decode(),
        }
    )


def open_pending(vault: Vault, login_id: str, sealed: str | bytes) -> PendingLogin:
    """Raises VaultDecryptError if the blob does not authenticate (wrong key, other login)."""
    try:
        outer = json.loads(sealed)
        nonce, ciphertext = base64.b64decode(outer["n"]), base64.b64decode(outer["c"])
        key_version = int(outer["kv"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise VaultDecryptError("pending login is unreadable") from None
    data: dict[str, Any] = json.loads(
        vault.decrypt(_pending_name(login_id), nonce, ciphertext, key_version).reveal()
    )
    return PendingLogin(
        login_id=login_id,
        creds=ApiCredentials(api_id=int(data["api_id"]), api_hash=SecretValue(str(data["api_hash"]))),
        phone=SecretValue(str(data["phone"])),
        session=SecretValue(str(data["session"])),
        phone_code_hash=SecretValue(str(data["phone_code_hash"])),
    )


async def put_pending(redis: Redis, vault: Vault, pending: PendingLogin) -> None:
    await redis.set(
        PENDING_PREFIX + pending.login_id, seal_pending(vault, pending), ex=int(LOGIN_TTL.total_seconds())
    )


async def get_pending(redis: Redis, vault: Vault, login_id: str) -> PendingLogin | None:
    sealed = await redis.get(PENDING_PREFIX + login_id)
    if sealed is None:
        return None
    try:
        return open_pending(vault, login_id, sealed)
    except (VaultDecryptError, ValueError, KeyError, TypeError):
        return None  # unreadable is the same as gone: the owner starts again


async def drop_pending(redis: Redis | None, login_id: str | None) -> None:
    if redis is not None and login_id:
        await redis.delete(PENDING_PREFIX + login_id)


# --- one Telegram operation at a time ------------------------------------------------------------------


class LockBusy(RuntimeError):
    """Another operation on the operator account holds the lock."""


@asynccontextmanager
async def account_lock(engine: AsyncEngine, *, wait_s: float) -> AsyncIterator[None]:
    """The session-level advisory lock :data:`LOCK_NAME` (the worker's check takes the same one),
    held on its own connection with no transaction open, so Telegram I/O may run inside."""
    key = lock_key(LOCK_NAME)
    async with engine.connect() as conn:
        deadline = time.monotonic() + wait_s
        while True:
            got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})).scalar_one()
            await conn.commit()
            if got:
                break
            if time.monotonic() >= deadline:
                raise LockBusy
            await asyncio.sleep(0.2)
        try:
            yield
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await conn.commit()
            except BaseException:
                await conn.invalidate()  # closing the connection releases its session locks
                raise
