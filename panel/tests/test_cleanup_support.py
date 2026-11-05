"""Shared helpers for the cleaning buttons' tests (proxyhub/cleanup). Real PostgreSQL only (D10).

The purge counts EVERY ended row in the database, so the fixture starts from tables emptied of
what the purge looks at (other test modules set up their own rows per test). The rows are written
with SQL exactly as the panel's own code leaves them, with every CHECK constraint satisfied.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.mtproto.urls import proxy_key, user_name_for
from proxyhub.telegram import store
from proxyhub.telegram.schedule import TEHRAN
from proxyhub.vault import SecretValue, Vault

PREFIX = "cleanup-test-"
TEST_CHAT = -1009876500001
MAIN_CHAT = -1001234500002
BOT_ID = 8123456789
BOT_USERNAME = "proxyhub_cleanup_bot"

# Tables the purge reads or writes, emptied before and after each test (CASCADE follows the foreign
# keys: nodes and every table that hangs on them go with hetzner_accounts).
WIPE_SQL = (
    "TRUNCATE hetzner_accounts, outbox, posts, post_links, channel_posts, mtproxy_registrations, "
    "sponsor_flows, mtproxy_stats, reality_keys, reality_configs, reality_usage, probe_cycles, "
    "analytics_alerts, analytics_post_views, telegram_pacing, agent_ca_certificates, cf_zones, "
    "cost_ledger CASCADE",
    "DELETE FROM settings WHERE key LIKE 'telegram.%' OR key LIKE 'posting%'",
    "DELETE FROM secrets WHERE name LIKE 'telegram:%'",
    "DELETE FROM actions_log",
)


async def wipe(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for sql in WIPE_SQL:
            await conn.execute(text(sql))


def hex64() -> str:
    return secrets.token_hex(32)


def sealed() -> bytes:
    """45 bytes shaped like a sealed Telemt secret (version + nonce + secret + tag); random."""
    return secrets.token_bytes(45)


def random_token() -> str:
    """Token-SHAPED, random, never a real bot token (not sent anywhere unless a test says so)."""
    return f"{1_000_000_000 + secrets.randbelow(10**9)}:{secrets.token_urlsafe(32)}"


@dataclass
class World:
    engine: AsyncEngine
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    account: uuid.UUID | None = None
    cycle: int | None = None
    ca: str | None = None

    @property
    def old(self) -> datetime:
        return self.now - timedelta(days=3)

    @property
    def very_old(self) -> datetime:
        return self.now - timedelta(days=10)

    @property
    def recent(self) -> datetime:
        return self.now - timedelta(minutes=10)

    async def run(self, sql: str, **params: Any) -> Any:
        async with self.engine.begin() as conn:
            return await conn.execute(text(sql), params)

    async def one(self, sql: str, **params: Any) -> Any:
        async with self.engine.begin() as conn:  # also for INSERT ... RETURNING
            return (await conn.execute(text(sql), params)).scalar_one()

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.engine.connect() as conn:
            return list((await conn.execute(text(sql), params)).all())

    async def counts(self, tables: list[str] | tuple[str, ...]) -> dict[str, int]:
        async with self.engine.connect() as conn:
            out = {}
            for table in tables:
                out[table] = int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608
            return out

    # --- fleet ---------------------------------------------------------------------------------

    async def ensure_account(self) -> uuid.UUID:
        if self.account is None:
            self.account = uuid.uuid4()
            await self.run(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (:id, :name, 'cleanup-test-token-file', 'active')",
                id=self.account,
                name=f"{PREFIX}{self.account.hex[:8]}",
            )
        return self.account

    async def node(
        self, *, state: str = "ready", deleted_at: datetime | None = None, slot: int | None = None
    ) -> uuid.UUID:
        node_id = uuid.uuid4()
        await self.run(
            "INSERT INTO nodes (id, account_id, name, slot, location, server_type, state, ipv4, "
            "deleted_at, updated_at, is_test) VALUES (:id, :acc, :name, :slot, 'hel1', 'cpx02', :state, "
            "CAST(:ip AS inet), :deleted_at, :updated, false)",
            id=node_id,
            acc=await self.ensure_account(),
            name=f"{PREFIX}{node_id.hex[:8]}",
            slot=slot,
            state=state,
            ip=f"203.0.113.{secrets.randbelow(250) + 1}",
            deleted_at=deleted_at,
            updated=deleted_at or self.now,
        )
        return node_id

    async def node_rows(self, node_id: uuid.UUID, *, reach_at: datetime | None = None) -> None:
        """Every kind of row that hangs on a node, as the panel writes them."""
        if self.cycle is None:
            self.cycle = await self.one(
                "INSERT INTO probe_cycles (started_at, finished_at) VALUES (:t, :t) RETURNING id", t=self.old
            )
        if self.ca is None:
            self.ca = hex64()
            await self.run(
                "INSERT INTO agent_ca_certificates (spki_sha256, cert_pem, not_after) VALUES (:s, 'pem', :t)",
                s=self.ca,
                t=self.now + timedelta(days=300),
            )
        user = user_name_for(node_id)
        for kind in ("created", "ready"):
            await self.run(
                "INSERT INTO node_events (node_id, at, kind, detail) VALUES (:n, :t, :k, '{}')",
                n=node_id,
                t=self.old,
                k=kind,
            )
        await self.run(
            "INSERT INTO agent_nodes (node_id, agent_version, registered_at) VALUES (:n, '0.4.0', :t)",
            n=node_id,
            t=self.old,
        )
        await self.run(
            "INSERT INTO agent_certificates (node_id, fingerprint_sha256, serial_hex, ca_spki_sha256, "
            "cert_pem, not_before, not_after, issued_via) VALUES (:n, :f, :s, :ca, 'pem', :a, :b, "
            "'register')",
            n=node_id,
            f=hex64(),
            s=secrets.token_hex(16),
            ca=self.ca,
            a=self.old,
            b=self.now + timedelta(days=30),
        )
        await self.run(
            "INSERT INTO agent_registration_tokens (id, node_id, token_sha256, created_at, expires_at) "
            "VALUES (:i, :n, :h, :a, :b)",
            i=uuid.uuid4(),
            n=node_id,
            h=hex64(),
            a=self.old,
            b=self.old + timedelta(hours=1),
        )
        for _ in range(2):
            await self.run(
                "INSERT INTO agent_node_reports (node_id, at, apply_ok, telemt_up, users_reported) "
                "VALUES (:n, :t, true, true, 1)",
                n=node_id,
                t=self.old,
            )
        await self.run(
            "INSERT INTO telemt_node_configs (node_id, ports, tls_domain, mode_classic, mode_secure, "
            "mode_tls) "
            "VALUES (:n, ARRAY[8443], 'github.com', false, false, true)",
            n=node_id,
        )
        await self.run(
            "INSERT INTO telemt_users (id, node_id, name, secret_sealed, enabled) VALUES (:i, :n, :u, :s, "
            "true)",
            i=uuid.uuid4(),
            n=node_id,
            u=user,
            s=sealed(),
        )
        await self.run(
            "INSERT INTO mtproto_proxies (node_id, user_name, created_at, live_at) VALUES (:n, :u, :t, :t)",
            n=node_id,
            u=user,
            t=self.old,
        )
        measurement = await self.one(
            "INSERT INTO probe_measurements (cycle_id, node_id, at, source, kind, role, target, port, "
            "status) "
            "VALUES (:c, :n, :t, 'globalping', 'https', 'node', '203.0.113.9', 443, 'ok') RETURNING id",
            c=self.cycle,
            n=node_id,
            t=self.old,
        )
        for key in ("asn:197207", "asn:57218"):
            await self.run(
                "INSERT INTO probe_network_results (measurement_id, network_key, network, ok, fail, nodata, "
                "detail) VALUES (:m, :k, 'net', 1, 0, 0, '{}')",
                m=measurement,
                k=key,
            )
        await self.run(
            "INSERT INTO probe_rounds (cycle_id, node_id, started_at, finished_at, trigger, target_ip, port, "
            "observations, verdict, verdict_reasons) VALUES (:c, :n, :t, :t, 'schedule', "
            "CAST('203.0.113.9' AS inet), 443, '{}', 'ok', '[]')",
            c=self.cycle,
            n=node_id,
            t=self.old,
        )
        await self.run(
            "INSERT INTO probe_node_state (node_id, verdict, verdict_reasons) VALUES (:n, 'ok', '[]')",
            n=node_id,
        )
        await self.run(
            "INSERT INTO probe_location_reach (node_id, network_key, network, location, state, at) "
            "VALUES (:n, 'asn:197207', 'MCI', 'hel1', 'open', :t)",
            n=node_id,
            t=reach_at or self.very_old,
        )
        await self.run(
            "INSERT INTO node_operations (node_id, requested_by, requested_at, state_since, create_attempts, "
            "delete_attempts) VALUES (:n, 'owner', :t, :t, 1, 1)",
            n=node_id,
            t=self.old,
        )
        await self.run(
            "INSERT INTO reality_node_status (node_id, enabled, up, users_reported, reported_at) "
            "VALUES (:n, true, true, 2, :t)",
            n=node_id,
            t=self.old,
        )
        await self.run(
            "INSERT INTO ip_history (ip, prefix24, node_id, account_id, location, first_seen_at) "
            "VALUES (CAST(:ip AS inet), CAST(:p AS cidr), :n, :a, 'hel1', :t)",
            ip=f"198.51.100.{secrets.randbelow(250) + 1}",
            p="198.51.100.0/24",
            n=node_id,
            a=self.account,
            t=self.old,
        )

    # --- reality -------------------------------------------------------------------------------

    async def reality_key(self, *, slot: int | None = None, node_id: uuid.UUID | None = None) -> int:
        scope = f"slot:{slot}" if slot is not None else f"node:{node_id}"
        return int(
            await self.one(
                "INSERT INTO reality_keys (slot, node_id, public_key, short_id, target, server_name, "
                "secret_name, ws_path) VALUES (:slot, :node, :pk, 'ab12', 'github.com:443', 'github.com', "
                ":sn, :ws) RETURNING id",
                slot=slot,
                node=node_id,
                pk=secrets.token_urlsafe(32)[:43].ljust(43, "A"),
                sn=f"reality:key:{scope}",
                ws="/" + secrets.token_hex(8),
            )
        )

    async def reality_config(
        self, node_id: uuid.UUID, key_id: int, *, status: str = "active", ended_at: datetime | None = None
    ) -> int:
        created = (ended_at or self.now) - timedelta(hours=10)
        return int(
            await self.one(
                "INSERT INTO reality_configs (node_id, key_id, uuid, host, created_at, expires_at, "
                "data_cap_bytes, used_bytes, status, ended_at) VALUES (:n, :k, :u, 'slot1.example.test', "
                ":c, :e, 1000, 10, :s, :end) RETURNING id",
                n=node_id,
                k=key_id,
                u=uuid.uuid4(),
                c=created,
                e=created + timedelta(hours=48),
                s=status,
                end=ended_at,
            )
        )

    async def reality_usage(self, config_id: int, node_id: uuid.UUID) -> None:
        await self.run(
            "INSERT INTO reality_usage (config_id, node_id, total_bytes, last_reported) VALUES (:c, :n, 10, "
            "10)",
            c=config_id,
            n=node_id,
        )

    # --- sponsor -------------------------------------------------------------------------------

    async def registration(self, *, user: str, state: str, updated_at: datetime, stats: int = 0) -> uuid.UUID:
        reg_id = uuid.uuid4()
        await self.run(
            "INSERT INTO mtproxy_registrations (id, host, port, secret_name, ad_tag, state, registered_at, "
            "created_at, updated_at) VALUES (:i, :h, 8443, :u, :tag, :s, :t, :t, :t)",
            i=reg_id,
            h=f"{reg_id.hex[:8]}.example.test",
            u=user,
            tag=secrets.token_hex(16),
            s=state,
            t=updated_at,
        )
        await self.run(
            "INSERT INTO sponsor_flows (id, registration_id, host, port, user_name, created_at, updated_at) "
            "VALUES (:i, :i, 'x.example.test', 8443, :u, :t, :t)",
            i=reg_id,
            u=user,
            t=updated_at,
        )
        for _ in range(stats):
            await self.run(
                "INSERT INTO mtproxy_stats (registration_id, at, source, current_connections, extra) "
                "VALUES (:r, :t, 'node', 3, '{}')",
                r=reg_id,
                t=updated_at,
            )
        return reg_id

    # --- telegram, posts, outbox ---------------------------------------------------------------

    async def telegram(
        self, vault: Vault, *, test: bool = True, main: bool = True, same: bool = False
    ) -> str:
        """A bot (a random token-shaped value) and the channels, in our own database."""
        token = random_token()
        async with self.engine.begin() as conn:
            await store.save_bot(
                conn,
                vault,
                info=store.BotInfo(id=BOT_ID, username=BOT_USERNAME),
                token=SecretValue(token),
                now=self.now,
            )
            channels: dict[str, Any] = {}
            if main:
                channels["main"] = channel(MAIN_CHAT, "@ipoxxo_cleanup_main")
            if test:
                channels["test"] = channel(MAIN_CHAT if same else TEST_CHAT, "@ipoxxo_cleanup_test")
            await store.put_setting(conn, store.KEY_CHANNELS, channels)
        return token

    async def job(
        self,
        kind: str = "alert",
        *,
        done_at: datetime | None = None,
        failed: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> int:
        return int(
            await self.one(
                "INSERT INTO outbox (kind, payload, attempts, last_error, created_at, done_at) "
                "VALUES (:k, CAST(:p AS jsonb), 0, :e, :c, :d) RETURNING id",
                k=kind,
                p=_json(payload or {"text": "x"}),
                e="failed: bad_request: test" if failed else None,
                c=(done_at or self.now) - timedelta(minutes=1),
                d=done_at,
            )
        )

    async def channel_post(
        self, *, role: str, chat_id: int, message_id: int, outbox_id: int | None, created_at: datetime
    ) -> int:
        return int(
            await self.one(
                "INSERT INTO channel_posts (role, chat_id, message_id, kind, text_sha256, outbox_id, "
                "created_at) "
                "VALUES (:r, :c, :m, 'post', :h, :o, :t) RETURNING id",
                r=role,
                c=chat_id,
                m=message_id,
                h=hashlib.sha256(str(message_id).encode()).hexdigest(),
                o=outbox_id,
                t=created_at,
            )
        )

    async def post(
        self,
        *,
        role: str,
        created_at: datetime,
        slot: int,
        outbox_id: int | None,
        last_edit_outbox_id: int | None = None,
        links: Sequence[tuple[str, str]] = (),
        plan_day: date | None = None,
    ) -> int:
        post_id = int(
            await self.one(
                "INSERT INTO posts (role, outbox_id, channel_tag, plan_day, plan_slot, planned_at, "
                "created_at, last_edit_outbox_id) "
                "VALUES (:r, :o, '@ipoxxo', :d, :s, :t, :t, :e) RETURNING id",
                r=role,
                o=outbox_id,
                d=plan_day or created_at.astimezone(TEHRAN).date(),
                s=slot,
                t=created_at,
                e=last_edit_outbox_id,
            )
        )
        for position, (key, status) in enumerate(links):
            await self.run(
                "INSERT INTO post_links (post_id, position, kind, key, label, status) "
                "VALUES (:p, :pos, :k, :key, 'L', :s)",
                p=post_id,
                pos=position,
                k=key.split(":", 1)[0],
                key=key,
                s=status,
            )
        return post_id


def channel(chat_id: int, username: str) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "username": username,
        "title": "Proxy-Hub cleanup test",
        "can_post": True,
        "can_edit": True,
        "can_delete": False,
        "checked_at": "2026-11-05T20:00:00Z",
    }


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value)


def mtproto_key(node_id: uuid.UUID) -> str:
    return proxy_key(node_id, user_name_for(node_id))
