"""Shared helpers for the Reality tests (no tests here): real PostgreSQL, the real vault with a
master key file, nodes and statuses as the fleet and the agents leave them."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_agent_support import SecretFiles, reset_config_caches, use_secret_files, write_secret_files

from proxyhub.db.session import get_engine
from proxyhub.reality.store import XrayUserTotal, store_xray_report
from proxyhub.vault import Vault

PREFIX = "reality-test-"


@dataclass
class World:
    engine: AsyncEngine
    files: SecretFiles

    @property
    def vault(self) -> Vault:
        return Vault.from_key_file(self.files.master_key)

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []

    async def node(
        self,
        *,
        state: str = "ready",
        slot: int | None = None,
        ipv4: str | None = "203.0.113.10",
        location: str = "fsn1",
        ready_at: datetime | None = None,
    ) -> uuid.UUID:
        node_id, account_id = uuid.uuid4(), uuid.uuid4()
        await self.rows(
            "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
            "VALUES (:id, :name, 'reality-test-token-name', 'active')",
            id=account_id,
            name=f"{PREFIX}{account_id}",
        )
        await self.rows(
            "INSERT INTO nodes (id, account_id, name, slot, location, server_type, state, ipv4, ready_at, "
            "is_test) VALUES (:id, :acc, :name, :slot, :loc, 'cpx02', :state, CAST(:ip AS inet), :ready, "
            "false)",
            id=node_id,
            acc=account_id,
            name=f"{PREFIX}{node_id.hex[:8]}",
            slot=slot,
            loc=location,
            state=state,
            ip=ipv4,
            ready=ready_at or datetime.now(UTC),
        )
        return node_id

    async def report(
        self,
        node_id: uuid.UUID,
        *,
        now: datetime | None = None,
        enabled: bool = True,
        up: bool = True,
        target_ok: bool | None = True,
        apply_error: str | None = None,
        users: dict[str, int] | None = None,
    ) -> int:
        async with self.engine.begin() as conn:
            return await store_xray_report(
                conn,
                node_id,
                enabled=enabled,
                up=up,
                version="26.10.31",
                uptime_s=60.0,
                target_ok=target_ok,
                apply_error=apply_error,
                users=[XrayUserTotal(e, n) for e, n in (users or {}).items()],
                now=now or datetime.now(UTC),
            )

    async def zone(self, label: str, pool_state: str = "active") -> str:
        """A Cloudflare zone row as the net team stores it; returns its name (rtzone-<label>.example)."""
        name = f"rtzone-{label}.example"
        await self.rows(
            "INSERT INTO cf_zones (id, name, status, pool_state, seen_at) "
            "VALUES (:id, :name, 'active', :pool, now())",
            id=uuid.uuid4().hex,
            name=name,
            pool=pool_state,
        )
        return name


async def clean_reality(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for table in ("reality_usage", "reality_configs", "reality_keys", "reality_node_status"):
            await conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed table names
        await conn.execute(text("DELETE FROM secrets WHERE name LIKE 'reality:%'"))
        await conn.execute(text("DELETE FROM settings WHERE key IN ('reality', 'reality.state')"))
        await conn.execute(text("DELETE FROM actions_log WHERE actor = 'reality' OR target = 'reality'"))
        await conn.execute(text("DELETE FROM cf_zones WHERE name LIKE 'rtzone-%'"))
        await conn.execute(
            text(
                "DELETE FROM node_events WHERE node_id IN "
                "(SELECT id FROM nodes WHERE name LIKE 'reality-test-%')"
            )
        )
        await conn.execute(text("DELETE FROM nodes WHERE name LIKE 'reality-test-%'"))
        await conn.execute(text("DELETE FROM hetzner_accounts WHERE name LIKE 'reality-test-%'"))


@pytest.fixture(name="world")
async def world_fixture(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[World]:
    files = write_secret_files(tmp_path / "secrets")
    use_secret_files(monkeypatch, files)
    engine = create_async_engine(migrated_db)
    await clean_reality(engine)
    try:
        yield World(engine=engine, files=files)
    finally:
        await clean_reality(engine)
        await engine.dispose()
        await get_engine().dispose()
        reset_config_caches()
