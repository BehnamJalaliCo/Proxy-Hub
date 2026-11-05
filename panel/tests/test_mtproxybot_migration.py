"""Migration 0022_mtproxybot_inventory on the real PostgreSQL: the table, its checks, down and up."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from proxyhub.db.models_sponsor import MtproxybotEntry

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


def row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "host": "203.0.113.7",
        "port": None,
        "label_shape": "<host> <hex>",
        "label_fp": "0123456789abcdef",
        "ordinal": 0,
        "page": 1,
        "position": 0,
        "pressable": True,
        "first_seen_at": NOW,
        "seen_at": NOW,
    }
    return {**base, **kw}


async def _refused(conn: AsyncConnection, constraint: str, **kw: Any) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with conn.begin_nested():
            await conn.execute(insert(MtproxybotEntry).values(**row(**kw)))


async def test_migration_0022_round_trip_checks_and_no_drift(migrated_db: str) -> None:
    from alembic import command

    from proxyhub.cli import _alembic_config, downgrade, migrate

    engine = create_async_engine(migrated_db)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM mtproxybot_entries"))
            await conn.execute(insert(MtproxybotEntry).values(**row()))
            await conn.execute(
                insert(MtproxybotEntry).values(**row(ordinal=1, port=8443, removal_state="removed"))
            )
            await _refused(conn, "uq_mtproxybot_entries_label")  # the same button twice
            await _refused(conn, "ck_mtproxybot_entries_port", label_fp="f" * 16, port=70000)
            await _refused(
                conn, "ck_mtproxybot_entries_removal_state", label_fp="e" * 16, removal_state="deleted"
            )
            await conn.execute(
                text(
                    "INSERT INTO settings (key, value) VALUES ('sponsor.mtproxybot', '{}') "
                    "ON CONFLICT (key) DO NOTHING"
                )
            )
        await asyncio.to_thread(downgrade, "0021_operator_poster")
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT to_regclass('mtproxybot_entries')"))).scalar_one() is None
            left = await conn.execute(text("SELECT count(*) FROM settings WHERE key = 'sponsor.mtproxybot'"))
            assert left.scalar_one() == 0
        await asyncio.to_thread(migrate, "head")
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM mtproxybot_entries"))).scalar_one() == 0
        await asyncio.to_thread(command.check, _alembic_config())  # the models and the migration agree
    finally:
        await engine.dispose()
