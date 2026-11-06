"""Migration 0023_posting_compact on the real PostgreSQL: the stored posting settings take the D48
values (one Reality pair, six MTProto links) and keep every other key; down puts back only what is
still the D48 value; no row is made where the owner never saved one; no drift."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from proxyhub.posts import settings as posting

pytestmark = pytest.mark.db

BEFORE = "0022_mtproxybot_inventory"
# What an owner saved before D48: the old defaults for the two counts and own values elsewhere.
SAVED: dict[str, Any] = {
    **posting.DEFAULTS.as_json(),
    "max_mtproto": 12,
    "max_reality": 3,
    "posts_per_day": 7,
    "main_enabled": True,
    "status_texts": {"expired": "⌛️ تمام", "full": "🔴 پر", "burned": "🚫 رفت"},
}


async def _stored(engine: AsyncEngine) -> Any:
    async with engine.connect() as conn:
        got = await conn.execute(text("SELECT value FROM settings WHERE key = 'posting'"))
        return got.scalar_one_or_none()


async def _store(engine: AsyncEngine, value: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO settings (key, value) VALUES ('posting', CAST(:v AS jsonb)) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
            ),
            {"v": json.dumps(value)},
        )


async def test_migration_0023_moves_the_counts_once_and_back(migrated_db: str) -> None:
    from alembic import command

    from proxyhub.cli import _alembic_config, downgrade, migrate

    engine = create_async_engine(migrated_db)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM settings WHERE key LIKE 'posting%'"))
        await asyncio.to_thread(downgrade, BEFORE)
        # Never saved: nothing is stored, and the code's own defaults (now D48's) apply.
        await asyncio.to_thread(migrate, "head")
        assert await _stored(engine) is None
        assert (posting.DEFAULTS.max_mtproto, posting.DEFAULTS.max_reality) == (6, 1)

        # Saved before D48: the two counts change, every other key stays, and the row still loads.
        await asyncio.to_thread(downgrade, BEFORE)
        await _store(engine, SAVED)
        await asyncio.to_thread(migrate, "head")
        after = await _stored(engine)
        assert after == {**SAVED, "max_mtproto": 6, "max_reality": 1}
        cfg = posting.parse(after)
        assert (cfg.max_mtproto, cfg.max_reality, cfg.posts_per_day, cfg.main_enabled) == (6, 1, 7, True)
        assert cfg.texts().full == "🔴 پر"

        # Down: what still is the D48 value goes back; what the owner changed since stays his.
        await asyncio.to_thread(downgrade, BEFORE)
        assert await _stored(engine) == SAVED
        await asyncio.to_thread(migrate, "head")
        await _store(engine, {**SAVED, "max_mtproto": 8, "max_reality": 1})
        await asyncio.to_thread(downgrade, BEFORE)
        assert await _stored(engine) == {**SAVED, "max_mtproto": 8, "max_reality": 3}

        # A value that is not an object (corrupt; the code loads the defaults) is left as it is.
        await _store(engine, ["not", "settings"])
        await asyncio.to_thread(migrate, "head")
        assert await _stored(engine) == ["not", "settings"]
        await asyncio.to_thread(downgrade, BEFORE)
        assert await _stored(engine) == ["not", "settings"]
        await asyncio.to_thread(migrate, "head")
        await asyncio.to_thread(command.check, _alembic_config())  # the models and the migration agree
    finally:
        await asyncio.to_thread(migrate, "head")  # the next tests need the whole schema
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM settings WHERE key LIKE 'posting%'"))
        await engine.dispose()
