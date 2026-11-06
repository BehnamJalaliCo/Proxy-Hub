"""Migrations 0025_mtproto_extras (D49) and 0026_post_link_operator (D50) on the real PostgreSQL:
their checks, down and up again, no drift."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from test_mtproto_support import clean, make_node

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)


async def _refused(conn: AsyncConnection, constraint: str, sql: str, **params: object) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with conn.begin_nested():
            await conn.execute(text(sql), params)


async def _proxy(conn: AsyncConnection, node: object, user: str, ordinal: int) -> None:
    await conn.execute(
        text("INSERT INTO mtproto_proxies (node_id, user_name, ordinal, created_at) VALUES (:n, :u, :o, :t)"),
        {"n": node, "u": user, "o": ordinal, "t": NOW},
    )


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        got = await conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table}
        )
        return set(got.scalars().all())


async def test_migrations_0025_0026_round_trip_checks_and_no_drift(migrated_db: str) -> None:
    from alembic import command

    from proxyhub.cli import _alembic_config, downgrade, migrate

    engine = create_async_engine(migrated_db)
    await clean(engine)
    try:
        node = await make_node(engine, slot=1)
        async with engine.begin() as conn:
            await _proxy(conn, node, "mt_a", 0)
            await _proxy(conn, node, "mt_a_x1", 1)
            insert = (
                "INSERT INTO mtproto_proxies (node_id, user_name, ordinal, created_at) "
                "VALUES (:n, :u, :o, :t)"
            )
            await _refused(conn, "uq_mtproto_proxies_node_ordinal", insert, n=node, u="mt_a_x9", o=1, t=NOW)
            await _refused(conn, "ck_mtproto_proxies_ordinal", insert, n=node, u="mt_a_x6", o=6, t=NOW)
            post = (
                await conn.execute(
                    text(
                        "INSERT INTO posts (role, channel_tag, plan_day, plan_slot, planned_at, created_at) "
                        "VALUES ('test', '@ipoxxo', :d, 0, :t, :t) RETURNING id"
                    ),
                    {"d": date(2026, 11, 6), "t": NOW},
                )
            ).scalar_one()
            link = (
                "INSERT INTO post_links (post_id, position, kind, key, label, status, operator) "
                "VALUES (:p, :pos, 'mtproto', :k, 'پروکسی ۱', 'active', :op)"
            )
            await conn.execute(text(link), {"p": post, "pos": 0, "k": "mtproto:a:mt_a", "op": "mci"})
            await conn.execute(text(link), {"p": post, "pos": 1, "k": "mtproto:a:mt_a_x1", "op": None})
            await _refused(conn, "ck_post_links_operator", link, p=post, pos=2, k="mtproto:b:mt_b", op="tci")
        await asyncio.to_thread(downgrade, "0024_post_gone")
        assert "ordinal" not in await _columns(engine, "mtproto_proxies")
        assert "operator" not in await _columns(engine, "post_links")
        async with engine.connect() as conn:  # the primary stays; the extra row is forgotten
            kept = await conn.execute(
                text("SELECT user_name FROM mtproto_proxies WHERE node_id = :n"), {"n": node}
            )
            assert kept.scalars().all() == ["mt_a"]
            assert (await conn.execute(text("SELECT count(*) FROM post_links"))).scalar_one() == 2
        await asyncio.to_thread(migrate, "head")
        async with engine.connect() as conn:
            back = await conn.execute(
                text("SELECT user_name, ordinal FROM mtproto_proxies WHERE node_id = :n"), {"n": node}
            )
            assert [tuple(r) for r in back] == [("mt_a", 0)]
            ops = await conn.execute(text("SELECT operator FROM post_links ORDER BY position"))
            assert ops.scalars().all() == [None, None]  # a downgrade forgets the names
        await asyncio.to_thread(command.check, _alembic_config())  # the models and the migrations agree
    finally:
        await asyncio.to_thread(migrate, "head")
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM posts"))
        await clean(engine)
        await engine.dispose()
