"""Shared helpers for the posts team's tests (decision D10: nothing external is imitated).

The publisher's link providers are INTERNAL code (CROSS-TEAM CONTRACT item 3), so these tests use
small providers of their own that read the real ``nodes`` and ``cf_zones`` tables of the real
PostgreSQL, the way team mtproto's and team reality's providers will: the proxy host is
``slot<N>.<active domain>`` from :func:`proxyhub.cloudflare.rotation.current_active_domain`, or
the node's IPv4 when there is no active domain. Their secrets and VLESS ids are derived from the
node id at run time (no literal in the repo) and are only test values.

Telegram is never called here: a post counts as sent once the real outbox code records it
(:func:`proxyhub.telegram.outbox.record_success`, exactly what runs after Telegram accepted it).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.cloudflare.rotation import current_active_domain
from proxyhub.db.models import Node
from proxyhub.links.contract import ProxyLink
from proxyhub.telegram import outbox, store
from proxyhub.telegram.templates import fa_digits
from proxyhub.vault import SecretValue, Vault

OWNER = 111222333
TEST_CHAT = -1009876543210
MAIN_CHAT = -1001111111111
BOT_ID = 8123456789
REALITY_LIFETIME = timedelta(hours=24)

CLEAN_SQL = (
    "DELETE FROM post_links",
    "DELETE FROM posts",
    "DELETE FROM channel_posts",
    "DELETE FROM outbox",
    "DELETE FROM telegram_pacing",
    "DELETE FROM settings WHERE key LIKE 'telegram.%' OR key LIKE 'posting%' OR key = 'fleet'",
    "DELETE FROM secrets WHERE name LIKE 'telegram:%'",
    "DELETE FROM actions_log",
    "DELETE FROM content_items WHERE origin = 'owner'",
    "UPDATE content_items SET status = 'approved', used_count = 0, last_used_at = NULL WHERE origin = 'seed'",
    "DELETE FROM cf_records",
    "DELETE FROM domain_checks",
    "DELETE FROM cf_zones",
    "DELETE FROM telemt_node_configs WHERE node_id IN (SELECT id FROM nodes WHERE name LIKE 'posts-test-%')",
    "DELETE FROM nodes WHERE name LIKE 'posts-test-%'",
    "DELETE FROM hetzner_accounts WHERE name LIKE 'posts-test-%'",
)


async def clean_posts_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for sql in CLEAN_SQL:
            await conn.execute(text(sql))


def channel(chat_id: int, username: str | None = None) -> dict[str, Any]:
    return {"chat_id": chat_id, "username": username or str(chat_id), "title": "t", "can_post": True,
            "can_edit": True, "can_delete": False, "checked_at": "2026-11-04T20:00:00Z"}  # fmt: skip


async def setup_telegram(
    engine: AsyncEngine, vault: Vault, *, test: bool = True, main: bool = False, owner: bool = True
) -> None:
    """A bot (random token-shaped value; never sent anywhere), the owner chat and the channels."""
    import secrets

    token = f"{secrets.randbelow(9 * 10**9) + 10**9}:{secrets.token_urlsafe(26)[:35]}"
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=BOT_ID, username="proxyhub_test_bot"),
            token=SecretValue(token),
            now=now,
        )
        if owner:
            await store.set_owner_chat(conn, OWNER, now)
        if test:
            await store.save_channel(conn, "test", channel(TEST_CHAT))
        if main:
            await store.save_channel(conn, "main", channel(MAIN_CHAT, "@ipoxxo"))


async def fleet_kill_switch(engine: AsyncEngine, on: bool) -> None:
    from proxyhub.fleet.settings_store import load_fleet_settings, save_fleet_settings

    async with engine.begin() as conn:
        current = await load_fleet_settings(conn)
        await save_fleet_settings(conn, current.model_copy(update={"kill_switch": on}))


# --- nodes, zones --------------------------------------------------------------------------------


async def make_node(
    engine: AsyncEngine,
    *,
    slot: int,
    state: str = "ready",
    ipv4: str = "203.0.113.10",
    ready_at: datetime | None = None,
) -> uuid.UUID:
    node_id = uuid.uuid4()
    account_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (:id, :name, 'posts-test-token-name', 'active')"
            ),
            {"id": account_id, "name": f"posts-test-{account_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO nodes (id, account_id, name, slot, location, server_type, state, ipv4, "
                "ready_at, is_test) VALUES (:id, :acc, :name, :slot, 'fsn1', 'cpx02', :state, "
                "CAST(:ip AS inet), :ready_at, true)"
            ),
            {
                "id": node_id,
                "acc": account_id,
                "name": f"posts-test-{node_id.hex[:8]}",
                "slot": slot,
                "state": state,
                "ip": ipv4,
                "ready_at": ready_at or datetime.now(UTC),
            },
        )
    return node_id


async def set_node(engine: AsyncEngine, node_id: uuid.UUID, **values: Any) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in values)
    async with engine.begin() as conn:
        await conn.execute(text(f"UPDATE nodes SET {sets} WHERE id = :id"), {"id": node_id, **values})  # noqa: S608


async def set_active_zone(engine: AsyncEngine, name: str | None) -> None:
    """Make ``name`` the one active domain (the old active one becomes filtered), or none at all."""
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE cf_zones SET pool_state = 'filtered' WHERE pool_state = 'active'"))
        if name is not None:
            await conn.execute(
                text(
                    "INSERT INTO cf_zones (id, name, status, pool_state, dns_edit, seen_at) "
                    "VALUES (:id, :name, 'active', 'active', true, now())"
                ),
                {"id": hashlib.md5(name.encode(), usedforsecurity=False).hexdigest(), "name": name},
            )


# --- the test providers (internal code; read the real tables) -----------------------------------


def node_secret(node_id: uuid.UUID) -> str:
    return hashlib.sha256(b"posts-test-secret:" + node_id.bytes).hexdigest()[:32]


def node_vless_id(node_id: uuid.UUID) -> str:
    return str(uuid.UUID(bytes=hashlib.sha256(b"posts-test-vless:" + node_id.bytes).digest()[:16]))


async def _nodes(db: AsyncConnection) -> list[Any]:
    return list(
        (
            await db.execute(
                select(Node.id, Node.slot, Node.state, Node.ipv4, Node.ready_at)
                .where(Node.name.like("posts-test-%"), Node.slot.is_not(None))
                .order_by(Node.slot)
            )
        ).all()
    )


def _status(state: str) -> str | None:
    return {"ready": "active", "burned": "burned"}.get(state)


async def mtproto_provider(db: AsyncConnection) -> list[ProxyLink]:
    domain = await current_active_domain(db)
    out: list[ProxyLink] = []
    for r in await _nodes(db):
        status = _status(r.state)
        if status is None:
            continue
        host = f"slot{r.slot}.{domain}" if domain else str(r.ipv4).split("/")[0]
        out.append(
            ProxyLink(
                kind="mtproto",
                key=f"mtproto:{r.id}:slot{r.slot}",
                slot=r.slot,
                label=f"پروکسی {fa_digits(r.slot)}",
                url=f"https://t.me/proxy?server={host}&port=8443&secret={node_secret(r.id)}",
                expires_at=None,
                status=status,  # type: ignore[arg-type]
            )
        )
    return out


async def reality_provider(db: AsyncConnection) -> list[ProxyLink]:
    domain = await current_active_domain(db)
    now = datetime.now(UTC)
    out: list[ProxyLink] = []
    for r in await _nodes(db):
        status = _status(r.state)
        if status is None:
            continue
        expires = r.ready_at + REALITY_LIFETIME
        if status == "active" and expires <= now:
            status = "expired"
        host = f"slot{r.slot}.{domain}" if domain else str(r.ipv4).split("/")[0]
        url = (
            f"vless://{node_vless_id(r.id)}@{host}:443?encryption=none&security=reality"
            f"&sni=www.example.com&fp=chrome&pbk=testkey&type=tcp#ipoxxo-{r.slot}"
        )
        out.append(
            ProxyLink(
                kind="reality",
                key=f"reality:{r.id}",
                slot=r.slot,
                label=f"ریلیتی {fa_digits(r.slot)}",
                url=url,
                expires_at=expires,
                status=status,  # type: ignore[arg-type]
            )
        )
    return out


PROVIDERS = [mtproto_provider, reality_provider]


async def failing_provider(db: AsyncConnection) -> list[ProxyLink]:
    """A provider whose SQL fails: the real database error inside the provider's savepoint."""
    await db.execute(text("SELECT * FROM no_such_table_for_posts_tests"))
    return []


# --- the outbox, as it runs after Telegram accepted a message ------------------------------------


async def deliver(engine: AsyncEngine, job_id: int, *, message_id: int, at: datetime | None = None) -> None:
    """Record ``job_id`` as sent with the real outbox code (what process_one does on success)."""
    at = at or datetime.now(UTC)
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT kind, payload, attempts FROM outbox WHERE id = :i"), {"i": job_id}
            )
        ).one()
        job = outbox.Job(
            id=job_id, kind=row.kind, payload=dict(row.payload), attempts=row.attempts, last_error=None
        )
        if job.kind == "send_post":
            chat = TEST_CHAT if job.payload["role"] == "test" else MAIN_CHAT
            target = outbox.Target(chat_id=chat, role=job.payload["role"])
        else:
            cp = (
                await conn.execute(
                    text("SELECT chat_id, message_id FROM channel_posts WHERE id = :i"),
                    {"i": job.payload["post_id"]},
                )
            ).one()
            target = outbox.Target(
                chat_id=cp.chat_id, post_id=job.payload["post_id"], message_id=cp.message_id
            )
        await outbox.record_success(conn, job, target, message_id if job.kind == "send_post" else None, at)


async def fail_job(engine: AsyncEngine, job_id: int, reason: str = "bad_request: test") -> None:
    async with engine.begin() as conn:
        await outbox.finish_failed(conn, job_id, datetime.now(UTC), reason)


async def jobs(engine: AsyncEngine, kind: str | None = None) -> list[Any]:
    async with engine.connect() as conn:
        sql = "SELECT id, kind, payload, done_at, last_error, not_before FROM outbox"
        if kind:
            sql += " WHERE kind = :k"
        return list((await conn.execute(text(sql + " ORDER BY id"), {"k": kind})).all())


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


def vault_at(tmp_path: Path) -> Vault:
    from test_fleet_support import make_vault

    return make_vault(tmp_path)
