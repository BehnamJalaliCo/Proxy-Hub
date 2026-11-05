"""Migration 0021_operator_poster on the real PostgreSQL: the operator's outbox kinds, down and up."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from proxyhub.db.models import Outbox

pytestmark = pytest.mark.db


async def _refused(conn: AsyncConnection, kind: str) -> None:
    with pytest.raises(IntegrityError, match="ck_outbox_kind"):
        async with conn.begin_nested():
            await conn.execute(insert(Outbox).values(kind=kind, payload={}))


async def test_migration_0021_round_trip_and_no_drift(migrated_db: str) -> None:
    from alembic import command

    from proxyhub.cli import _alembic_config, downgrade, migrate

    engine = create_async_engine(migrated_db)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM outbox"))
            for kind, done in (
                ("operator_send_post", None),
                ("operator_edit_post", None),
                ("operator_send_post", datetime.now(UTC)),
            ):
                await conn.execute(insert(Outbox).values(kind=kind, payload={}, done_at=done))
        await asyncio.to_thread(downgrade, "0020_merge")
        async with engine.connect() as conn:
            got = (
                await conn.execute(
                    text("SELECT kind, done_at IS NOT NULL, last_error FROM outbox ORDER BY id")
                )
            ).all()
        # The older code cannot send an operator job: a waiting one is finished as failed, and every
        # one takes the bot's kind of the same meaning (never a delete, nothing removed).
        assert [(k, d) for k, d, _ in got] == [("send_post", True), ("edit_post", True), ("send_post", True)]
        assert got[0][2] == got[1][2] == "failed: operator job (downgrade)" and got[2][2] is None
        async with engine.begin() as conn:
            await _refused(conn, "operator_send_post")
        await asyncio.to_thread(migrate, "head")
        async with engine.begin() as conn:
            for kind in ("operator_send_post", "operator_edit_post", "send_post", "edit_post", "alert"):
                await conn.execute(insert(Outbox).values(kind=kind, payload={}))
            await _refused(conn, "delete_post")
            await _refused(conn, "operator_delete_post")
            await conn.execute(text("DELETE FROM outbox"))
        await asyncio.to_thread(command.check, _alembic_config())  # the models and the migration agree
    finally:
        await engine.dispose()
