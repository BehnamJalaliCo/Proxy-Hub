"""Shared helpers for the sponsor tests (real PostgreSQL and Redis; decision D10).

:class:`TelemtUsersProvider` is this team's own small DB-backed provider (the brief's stand-in for
team mtproto's ``sponsor_api`` until the integrator wires it): it reads the REAL ``nodes``,
``telemt_node_configs`` and ``telemt_users`` tables, opens the secrets with the REAL sealing code,
and writes the tag into ``telemt_users.ad_tag`` exactly where the node's desired state reads it
(proxyhub/agent_api/desired.py). It is internal code, not an imitation of an external service.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from proxyhub.agent_api.desired import create_telemt_user, set_telemt_node_config
from proxyhub.agent_api.sealing import open_secret
from proxyhub.mtproxy import state as account_state
from proxyhub.mtproxy import store as account_store
from proxyhub.telegram import store as tg_store

SPONSOR_SQL = (
    "DELETE FROM mtproxy_stats",
    "UPDATE telemt_users SET registration_id = NULL WHERE registration_id IS NOT NULL",
    "DELETE FROM mtproxy_registrations",  # sponsor_flows cascade
    "DELETE FROM sponsor_campaigns",
    "DELETE FROM settings WHERE key LIKE 'sponsor.%' "
    "OR key IN ('fleet', 'telegram.owner', 'mtproxy.account')",
    "DELETE FROM secrets WHERE name LIKE 'mtproxy:%'",
    "DELETE FROM actions_log WHERE action LIKE 'sponsor.%' OR action LIKE 'mtproxy.%'",
    "DELETE FROM outbox",
    "DELETE FROM nodes WHERE name LIKE 'sponsor-test-%'",  # telemt configs and users cascade
    "DELETE FROM hetzner_accounts WHERE name LIKE 'sponsor-test-%'",
)
OWNER_CHAT = 5_550_001


async def clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for sql in SPONSOR_SQL:
            await conn.execute(text(sql))


@dataclass(frozen=True)
class Proxy:
    key: str
    host: str
    port: int
    secret_hex: str

    def __repr__(self) -> str:
        return f"Proxy(key={self.key!r}, host={self.host!r}, port={self.port})"


class TelemtUsersProvider:
    """Live proxies = enabled Telemt users without a tag on this team's test nodes in state
    ``ready`` (the shared test database may hold other teams' nodes, sealed with other keys)."""

    def __init__(self, seal_key: bytes, *, host_of: dict[uuid.UUID, str] | None = None) -> None:
        self.seal_key = seal_key
        self.host_of = host_of or {}  # node id -> slot DNS name (else the node's IPv4)
        self.applied: list[tuple[str, str | None]] = []

    async def proxies_to_register(self, db: AsyncConnection) -> list[Proxy]:
        rows = await db.execute(
            text(
                "SELECT u.node_id, u.name, u.secret_sealed, host(n.ipv4) AS ip, c.ports[1] AS port "
                "FROM telemt_users u JOIN nodes n ON n.id = u.node_id "
                "JOIN telemt_node_configs c ON c.node_id = u.node_id "
                "WHERE u.ad_tag IS NULL AND u.enabled AND n.state = 'ready' AND n.ipv4 IS NOT NULL "
                "AND n.name LIKE 'sponsor-test-%' "
                "ORDER BY n.created_at, u.name"
            )
        )
        found = []
        for r in rows:
            secret = open_secret(self.seal_key, r.node_id, r.name, bytes(r.secret_sealed)).hex()
            host = self.host_of.get(r.node_id, str(r.ip))
            found.append(
                Proxy(key=f"mtproto:{r.node_id}:{r.name}", host=host, port=int(r.port), secret_hex=secret)
            )
        return found

    async def apply_ad_tag(self, db: AsyncConnection, key: str, ad_tag: str | None) -> None:
        _, node_id, name = key.split(":")
        done = await db.execute(
            text(
                "UPDATE telemt_users SET ad_tag = :tag, updated_at = now() WHERE node_id = :n AND name = :u"
            ),
            {"tag": ad_tag, "n": uuid.UUID(node_id), "u": name},
        )
        if done.rowcount != 1:
            raise LookupError("no such Telemt user")
        self.applied.append((key, ad_tag))


async def create_proxy_node(
    engine: AsyncEngine,
    seal_key: bytes,
    *,
    ipv4: str,
    state: str = "ready",
    port: int = 8443,
    users: tuple[str, ...] = ("s1",),
) -> uuid.UUID:
    """A Hetzner account, a node with Telemt settings and users (fresh random secrets, sealed)."""
    account_id, node_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (:id, :name, 'sponsor-test-token-file-name', 'active')"
            ),
            {"id": account_id, "name": f"sponsor-test-{account_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO nodes (id, account_id, name, location, server_type, state, is_test, ipv4) "
                "VALUES (:id, :acc, :name, 'fsn1', 'cpx12', :state, true, CAST(:ip AS inet))"
            ),
            {"id": node_id, "acc": account_id, "name": f"sponsor-test-{node_id}", "state": state, "ip": ipv4},
        )
    async with AsyncSession(engine) as session:
        await set_telemt_node_config(session, node_id, ports=[port], tls_domain="www.example.com")
        for name in users:
            await create_telemt_user(session, seal_key, node_id, name)
        await session.commit()
    return node_id


async def set_node_state(
    engine: AsyncEngine, node_id: uuid.UUID, state: str, ipv4: str | None = None
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE nodes SET state = :s, ipv4 = COALESCE(CAST(:ip AS inet), ipv4) WHERE id = :id"),
            {"s": state, "ip": ipv4, "id": node_id},
        )


async def user_tags(engine: AsyncEngine, node_id: uuid.UUID) -> dict[str, str | None]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT name, ad_tag FROM telemt_users WHERE node_id = :n"), {"n": node_id}
        )
        return {str(r[0]): r[1] for r in rows}


async def kill_switch(engine: AsyncEngine, on: bool) -> None:
    from proxyhub.fleet.settings_store import save_fleet_settings
    from proxyhub.settings_schema import FleetSettings

    async with engine.begin() as conn:
        await save_fleet_settings(conn, FleetSettings(kill_switch=on))


async def pair_owner(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await tg_store.set_owner_chat(conn, OWNER_CHAT, datetime.now(UTC))


async def connect_account(engine: AsyncEngine, *, bot_id: int | None = 93_372_553, **row: Any) -> None:
    """The operator account as connected (status row only; tests that need the vault seed it)."""
    new = account_state.connected(
        account_state.AccountRow(phone_masked="+44•••••23"),
        session_id=secrets.token_hex(8),
        user_id=5_000_001,
        first_name="Operator",
        mtproxybot_id=bot_id,
        check_error=None,
        now=datetime.now(UTC),
    )
    new = account_state.AccountRow(**{**new.to_json(), **row})
    async with engine.begin() as conn:
        await account_store.save_row(conn, new)


async def actions(engine: AsyncEngine, prefix: str = "sponsor.") -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT actor, action, target, allowed, reasons FROM actions_log "
                "WHERE action LIKE :p ORDER BY id"
            ),
            {"p": prefix + "%"},
        )
        return [dict(r._mapping) for r in rows]


async def alerts(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT payload FROM outbox WHERE kind = 'alert' ORDER BY id"))
        return [str(r[0]["text"]) for r in rows]


async def regs(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT r.id, r.host, r.port, r.secret_name, r.ad_tag, r.sponsor_channel, r.state, "
                "r.state_reason, f.proxy_key, f.attempts, f.next_attempt_at, f.last_error, f.stuck_reason, "
                "f.last_bot_text, f.alerted_at FROM mtproxy_registrations r "
                "LEFT JOIN sponsor_flows f ON f.registration_id = r.id ORDER BY r.created_at, r.host"
            )
        )
        return [dict(r._mapping) for r in rows]
