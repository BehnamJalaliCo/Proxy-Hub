"""Against a real PostgreSQL: migrations round-trip, constraints, and the advisory lock."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from proxyhub.reconcilers.base import Reconciler

pytestmark = pytest.mark.db


async def test_migrations_round_trip(migrated_db: str) -> None:
    from proxyhub.cli import downgrade, migrate

    await asyncio.to_thread(downgrade, "base")
    await asyncio.to_thread(migrate, "head")
    engine = create_async_engine(migrated_db)
    async with engine.connect() as conn:
        tables = set(
            (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))).scalars()
        )
    await engine.dispose()
    assert {
        "settings",
        "nodes",
        "hetzner_accounts",
        "outbox",
        "mtproxy_registrations",
        "sponsor_campaigns",
        "mtproxy_stats",
    } <= tables


async def test_outbox_rejects_delete_jobs(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db)
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO outbox (kind, payload) VALUES ('delete_post', '{}')"))
    await engine.dispose()


async def test_node_state_check_constraint(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db)
    async with engine.begin() as conn:
        acc = (
            await conn.execute(
                text(
                    "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                    "VALUES (gen_random_uuid(), 'test-acc', 'hcloud_test', 'verifying') RETURNING id"
                )
            )
        ).scalar_one()
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO nodes (id, account_id, name, location, server_type, state, is_test) "
                    "VALUES (gen_random_uuid(), :a, 'n', 'fsn1', 'cpx02', 'exploded', true)"
                ),
                {"a": acc},
            )
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM hetzner_accounts WHERE id = :a"), {"a": acc})
    await engine.dispose()


class _Slow(Reconciler):
    name = "test-slow"

    def __init__(self) -> None:
        self.runs = 0

    async def run_once(self, conn: AsyncConnection) -> None:
        self.runs += 1
        await asyncio.sleep(0.5)


async def test_advisory_lock_prevents_double_run(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db, pool_size=6)
    a, b = _Slow(), _Slow()
    ran = await asyncio.gather(a.try_run_once(engine), b.try_run_once(engine))
    await engine.dispose()
    assert sorted(ran) == [False, True]
    assert a.runs + b.runs == 1
