"""Migration 0014_posts on the real PostgreSQL: down and up again, the seeded library, no drift."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from proxyhub.posts import content
from proxyhub.telegram.templates import utf16_len, visible_text

pytestmark = pytest.mark.db

POSTS_TABLES = {"content_items", "posts", "post_links"}


async def test_migration_0014_round_trip_seed_and_no_drift(migrated_db: str) -> None:
    from alembic import command

    from proxyhub.cli import _alembic_config, downgrade, migrate

    engine = create_async_engine(migrated_db)
    try:
        await asyncio.to_thread(downgrade, "0010_domains")
        async with engine.connect() as conn:
            tables = set(
                (await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")))
                .scalars()
                .all()
            )
        assert not tables & POSTS_TABLES and "outbox" in tables  # only the posts tables went
        await asyncio.to_thread(migrate, "head")
        async with engine.connect() as conn:
            seeds = (
                await conn.execute(
                    text("SELECT title, body, tags, status, origin, used_count FROM content_items")
                )
            ).all()
            columns = set(
                (
                    await conn.execute(
                        text(
                            "SELECT table_name || '.' || column_name FROM information_schema.columns "
                            "WHERE table_name IN ('posts', 'post_links')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(seeds) >= 30
        assert {(s.status, s.origin, s.used_count) for s in seeds} == {("approved", "seed", 0)}
        assert len({s.title for s in seeds}) == len(seeds)  # no item twice
        for s in seeds:  # every seed passes the owner's own checks unchanged (tags, length, HTML)
            assert content.check_input(s.title, s.body, list(s.tags)) == content.ContentIn(
                title=s.title, body=s.body, tags=list(s.tags)
            )
            assert utf16_len(visible_text(s.body)) <= content.MAX_BODY_VISIBLE
            assert "tg-emoji" not in s.body
        assert {"post_links.missing_since", "posts.shown_sha256"} <= columns
        await asyncio.to_thread(command.check, _alembic_config())  # the models and the migration agree
    finally:
        await engine.dispose()
