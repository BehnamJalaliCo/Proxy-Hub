"""Shared helpers for the sponsor tests (real PostgreSQL and Redis; decision D10).

:class:`TelemtUsersProvider` is this team's own small DB-backed provider (the brief's stand-in for
team mtproto's ``sponsor_api``, which is not on this branch): it reads the REAL ``nodes``,
``telemt_node_configs`` and ``telemt_users`` tables, opens the secrets with the REAL sealing code,
and writes the tag into ``telemt_users.ad_tag`` exactly where the node's desired state reads it
(proxyhub/agent_api/desired.py). It is internal code, not an imitation of an external service.

It lists exactly what team mtproto's ``proxies_to_register`` lists (wg3/mtproto
``sponsor_api._registered_names``): a proxy counts as registered, and is NOT listed, when a Telemt
user of that name has a tag or a linked registration, or when a ``mtproxy_registrations`` row in
state ``pending`` or ``registered`` names its user or key in ``secret_name``. User names are per
node (``mt_<node hex16>``), as team mtproto makes them.
"""

from __future__ import annotations

import dataclasses
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from proxyhub.agent_api.desired import create_telemt_user, set_telemt_node_config, set_user_sponsor_tag
from proxyhub.agent_api.sealing import open_secret
from proxyhub.mtproxy import state as account_state
from proxyhub.mtproxy import store as account_store
from proxyhub.sponsor import store
from proxyhub.telegram import store as tg_store

SPONSOR_SQL = (
    "DELETE FROM mtproxy_stats",
    "UPDATE telemt_users SET registration_id = NULL WHERE registration_id IS NOT NULL",
    "DELETE FROM sponsor_flows",
    "DELETE FROM mtproxy_registrations",
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
    """Live proxies without a registration (team mtproto's rule, module docstring): enabled Telemt
    users on this team's test nodes in state ``ready`` (the shared test database may hold other
    teams' nodes, sealed with other keys)."""

    def __init__(self, seal_key: bytes, *, host_of: dict[uuid.UUID, str] | None = None) -> None:
        self.seal_key = seal_key
        self.host_of = host_of or {}  # node id -> slot DNS name (else the node's IPv4)
        self.applied: list[tuple[str, str | None]] = []
        self.fail_apply = False  # apply_ad_tag raises, as for a proxy the provider does not know

    async def proxies_to_register(self, db: AsyncConnection) -> list[Proxy]:
        rows = await db.execute(
            text(
                "SELECT u.node_id, u.name, u.secret_sealed, host(n.ipv4) AS ip, c.ports[1] AS port "
                "FROM telemt_users u JOIN nodes n ON n.id = u.node_id "
                "JOIN telemt_node_configs c ON c.node_id = u.node_id "
                "WHERE u.enabled AND n.state = 'ready' AND n.ipv4 IS NOT NULL "
                "AND n.name LIKE 'sponsor-test-%' "
                # team mtproto's _registered_names: a tag or a linked registration on that user name
                "AND u.name NOT IN (SELECT t.name FROM telemt_users t "
                "WHERE t.ad_tag IS NOT NULL OR t.registration_id IS NOT NULL) "
                # ... or a pending/registered row naming the user or the key
                "AND NOT EXISTS (SELECT 1 FROM mtproxy_registrations r "
                "WHERE r.state IN ('pending', 'registered') "
                "AND r.secret_name IN (u.name, 'mtproto:' || u.node_id::text || ':' || u.name)) "
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
        if self.fail_apply:
            raise LookupError("no such MTProto proxy")
        # As team mtproto does it: the tag through the desired-state code on an ORM session bound to
        # the caller's connection (inside a SAVEPOINT that session would roll the tag back when it
        # closes), then the link to the registration the tag came from, in plain SQL.
        session = AsyncSession(bind=db)
        try:  # DesiredStateError for a user that is not there
            await set_user_sponsor_tag(session, uuid.UUID(node_id), name, ad_tag)
            await session.flush()
        finally:
            await session.close()
        await db.execute(
            text(
                "UPDATE telemt_users SET registration_id = (SELECT r.id FROM mtproxy_registrations r "
                "WHERE r.secret_name IN (CAST(:u AS varchar), CAST(:key AS varchar)) "
                "AND r.state = 'registered' AND r.ad_tag = CAST(:tag AS varchar) LIMIT 1) "
                "WHERE node_id = :n AND name = CAST(:u AS varchar)"
            ),
            {"tag": ad_tag, "n": uuid.UUID(node_id), "u": name, "key": key},
        )
        self.applied.append((key, ad_tag))


def user_name(node_id: uuid.UUID) -> str:
    """Team mtproto's Telemt user name for a node's proxy (panel/proxyhub/mtproto/urls.py)."""
    return f"mt_{node_id.hex[:16]}"


async def create_proxy_node(
    engine: AsyncEngine,
    seal_key: bytes,
    *,
    ipv4: str,
    state: str = "ready",
    port: int = 8443,
    users: tuple[str, ...] | None = None,
) -> uuid.UUID:
    """A Hetzner account, a node with Telemt settings and users (fresh random secrets, sealed).
    Without ``users``: one user named per node, ``mt_<node hex16>``, as team mtproto names them."""
    account_id, node_id = uuid.uuid4(), uuid.uuid4()
    if users is None:
        users = (user_name(node_id),)
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
    """Every proxy the module works on (flow rows with their registration), oldest first."""
    async with engine.connect() as conn:
        return [dataclasses.asdict(r) for r in await store.load_regs(conn)]


async def registration_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    """The ``mtproxy_registrations`` table itself (what team mtproto's provider reads)."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, host, port, secret_name, ad_tag, state, state_reason FROM mtproxy_registrations "
                "ORDER BY created_at, host"
            )
        )
        return [dict(r._mapping) for r in rows]
