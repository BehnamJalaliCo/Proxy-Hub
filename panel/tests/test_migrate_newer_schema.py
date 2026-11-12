"""``proxyhub migrate`` on a database a newer commit already migrated (the deployer's rollback).

api and worker wait for ``migrate`` to succeed (deploy/docker-compose.yml), so it must leave such a
schema as it is instead of failing on the revision it does not know. Downgrade still refuses.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = pytest.mark.db

NEWER = "9999_from_a_newer_commit"


async def _version(engine: AsyncEngine) -> str:
    async with engine.connect() as conn:
        return str((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())


async def _set_version(engine: AsyncEngine, revision: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": revision})


async def test_migrate_leaves_a_newer_schema_alone(
    migrated_db: str, caplog: pytest.LogCaptureFixture
) -> None:
    from alembic.util.exc import CommandError

    from proxyhub.cli import downgrade, migrate

    engine = create_async_engine(migrated_db)
    head = await _version(engine)
    try:
        await _set_version(engine, NEWER)
        with caplog.at_level(logging.WARNING, logger="proxyhub.migrations"):
            await asyncio.to_thread(migrate)
        assert await _version(engine) == NEWER
        warned = [r.getMessage() for r in caplog.records if r.name == "proxyhub.migrations"]
        assert any("schema is newer than this code" in m and NEWER in m for m in warned)
        with pytest.raises(CommandError, match=NEWER):
            await asyncio.to_thread(downgrade, head)
        assert await _version(engine) == NEWER
        await _set_version(engine, head)
        await asyncio.to_thread(migrate)  # a known revision upgrades as before (nothing to do at head)
        assert await _version(engine) == head
    finally:
        await _set_version(engine, head)  # the test database is shared by the whole session
        await asyncio.to_thread(migrate, "head")  # test_suite_migration_guard: head in a finally
        await engine.dispose()
