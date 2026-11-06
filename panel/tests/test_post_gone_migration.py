"""Migration 0024_post_gone on the real PostgreSQL: ``posts.gone_at`` down and up again, no drift."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from proxyhub.db.models_posts import Post

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)


async def _columns(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        got = await conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'posts'")
        )
        return set(got.scalars().all())


async def test_migration_0024_round_trip_and_no_drift(migrated_db: str) -> None:
    from alembic import command

    from proxyhub.cli import _alembic_config, downgrade, migrate

    engine = create_async_engine(migrated_db)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM post_links"))
            await conn.execute(text("DELETE FROM posts"))
            row = {
                "role": "test",
                "channel_tag": "@ipoxxo",
                "plan_day": date(2026, 11, 6),
                "plan_slot": 0,
                "planned_at": NOW,
                "created_at": NOW,
            }
            await conn.execute(insert(Post).values(**row, gone_at=NOW))
            await conn.execute(insert(Post).values(**{**row, "plan_slot": 1}))
        await asyncio.to_thread(downgrade, "0023_posting_compact")
        assert "gone_at" not in await _columns(engine)
        async with engine.connect() as conn:  # the posts themselves stay (rule 6)
            assert (await conn.execute(text("SELECT count(*) FROM posts"))).scalar_one() == 2
        await asyncio.to_thread(migrate, "head")
        assert "gone_at" in await _columns(engine)
        async with engine.connect() as conn:
            gone = (
                await conn.execute(text("SELECT count(*) FROM posts WHERE gone_at IS NOT NULL"))
            ).scalar_one()
        assert gone == 0  # a downgrade forgets it: such a post is edited (and found gone) once more
        await asyncio.to_thread(command.check, _alembic_config())  # the models and the migration agree
    finally:
        await asyncio.to_thread(migrate, "head")
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM posts"))
        await engine.dispose()
