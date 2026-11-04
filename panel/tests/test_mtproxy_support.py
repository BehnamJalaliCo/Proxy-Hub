"""Shared helpers for the operator-account tests (no imitation of Telegram anywhere, decision D10).

* Secret-shaped values (api_hash, session strings) are generated at run time, never literals.
* A session string for "a login in progress" or "a stored session" is a REAL Telethon
  ``StringSession`` holding a random auth key and pointing at a local port: connecting to it is a
  real refusal (nobody listens) or a real silence (``SilentServer``). Nothing answers like Telegram.
* The phone is from Ofcom's drama range (+44 7700 900xxx), never assigned to anyone.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from telethon.crypto import AuthKey
from telethon.sessions import StringSession
from test_fleet_support import assert_absent_from_database

from proxyhub.mtproxy import state, store
from proxyhub.mtproxy.state import AccountRow
from proxyhub.mtproxy.userclient import ApiCredentials
from proxyhub.mtproxy.validation import mask_phone
from proxyhub.vault import SecretValue, Vault

PHONE = "+447700900123"
MASKED = mask_phone(PHONE)
USER_ID = 5_000_001

MTPROXY_SQL = (
    "DELETE FROM settings WHERE key IN ('mtproxy.account', 'fleet', 'telegram.owner')",
    "DELETE FROM secrets WHERE name LIKE 'mtproxy:%'",
    "DELETE FROM actions_log WHERE action LIKE 'mtproxy.%'",
    "DELETE FROM outbox",
)


def api_hash() -> str:
    return secrets.token_hex(16)


def creds() -> ApiCredentials:
    return ApiCredentials(api_id=100_000 + secrets.randbelow(900_000), api_hash=SecretValue(api_hash()))


def session_string(port: int) -> str:
    """A real StringSession with a random auth key, pointing at 127.0.0.1:<port>."""
    session = StringSession()
    session.set_dc(2, "127.0.0.1", port)
    session.auth_key = AuthKey(os.urandom(256))
    return str(session.save())


async def clean(engine: AsyncEngine, redis: Redis | None = None) -> None:
    async with engine.begin() as conn:
        for sql in MTPROXY_SQL:
            await conn.execute(text(sql))
    if redis is not None:
        keys = [k async for k in redis.scan_iter(match=store.PENDING_PREFIX + "*")]
        if keys:
            await redis.delete(*keys)


async def seed_pending(
    engine: AsyncEngine,
    redis: Redis,
    vault: Vault,
    *,
    port: int,
    now: datetime | None = None,
    step: str = "code",
) -> store.PendingLogin:
    now = now or datetime.now(UTC)
    login_id = secrets.token_hex(8)
    pending = store.PendingLogin(
        login_id=login_id,
        creds=creds(),
        phone=SecretValue(PHONE),
        session=SecretValue(session_string(port)),
        phone_code_hash=SecretValue(secrets.token_hex(9)),
    )
    await store.put_pending(redis, vault, pending)
    row = state.code_sent(login_id=login_id, phone_masked=MASKED, via="app", length=5, now=now)
    if step == "password":
        row = state.password_needed(row, now)
    async with engine.begin() as conn:
        await store.save_row(conn, row)
    return pending


async def seed_connected(
    engine: AsyncEngine, vault: Vault, *, port: int, now: datetime | None = None, **row_fields: Any
) -> tuple[ApiCredentials, SecretValue, AccountRow]:
    now = now or datetime.now(UTC)
    api, session = creds(), SecretValue(session_string(port))
    row = state.connected(
        AccountRow(phone_masked=MASKED),
        session_id=secrets.token_hex(8),
        user_id=USER_ID,
        first_name="Operator",
        mtproxybot_id=None,
        check_error=None,
        now=now,
    )
    row = AccountRow(**{**row.to_json(), **row_fields})
    async with engine.begin() as conn:
        await store.save_account_secrets(conn, vault, api, session)
        await store.save_row(conn, row)
    return api, session, row


async def load_row(engine: AsyncEngine) -> AccountRow:
    async with engine.connect() as conn:
        return await store.load_row(conn)


async def secret_names(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT name FROM secrets WHERE name LIKE 'mtproxy:%' ORDER BY name"))
        return [str(r[0]) for r in rows]


async def actions(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT actor, action, target, allowed, reasons, result FROM actions_log "
                "WHERE action LIKE 'mtproxy.%' ORDER BY id"
            )
        )
        return [dict(r._mapping) for r in rows]


async def idle_in_transaction(engine: AsyncEngine) -> int:
    """Other connections of this database sitting in an open transaction right now."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
            )
        )
        return int(found.scalar_one())


async def redis_values(redis: Redis) -> list[bytes]:
    out: list[bytes] = []
    async for key in redis.scan_iter(match="*"):
        if await redis.type(key) == b"string":
            value = await redis.get(key)
            if value is not None:
                out.append(value)
    return out


def assert_not_in(needles: list[str], *texts: str | bytes) -> None:
    for needle in needles:
        for value in texts:
            body = value.decode(errors="replace") if isinstance(value, bytes) else value
            assert needle not in body, "a secret leaked"
            assert needle.encode().hex() not in body, "a hex-encoded secret leaked"


async def assert_nowhere(engine: AsyncEngine, redis: Redis | None, needles: list[str], *texts: str) -> None:
    """Rule 3: not in these texts (responses, logs), not in any database row, not in Redis."""
    assert_not_in(needles, *texts)
    for needle in needles:
        await assert_absent_from_database(engine, needle)
    if redis is not None:
        assert_not_in(needles, *(await redis_values(redis)))
