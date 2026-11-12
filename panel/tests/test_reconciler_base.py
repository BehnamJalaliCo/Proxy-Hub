"""The shared control loop (proxyhub/reconcilers/base.py) and the worker that runs the loops
(proxyhub/worker.py), on a real PostgreSQL: each loop records how its passes go, so a loop that
fails on every pass (or hangs) is visible beside the heartbeat, and a loop task that ends stops the
worker instead of leaving it running without that loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from proxyhub import worker
from proxyhub.reconcilers.base import LOOP_KEY_PREFIX, Reconciler

pytestmark = pytest.mark.db


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await _clean(eng)
    yield eng
    await _clean(eng)
    await eng.dispose()


async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM settings WHERE key LIKE 'worker.loop.%'"))


async def _record(engine: AsyncEngine, name: str) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        value = (
            await conn.execute(
                text("SELECT value FROM settings WHERE key = :k"), {"k": LOOP_KEY_PREFIX + name}
            )
        ).scalar_one_or_none()
    return value if isinstance(value, dict) else None


class _Counted(Reconciler):
    """Runs ``passes`` passes (each one raises when ``fails``), recording the waits between them."""

    interval_seconds = 15.0
    max_backoff_seconds = 300.0

    def __init__(self, name: str, *, fails: bool, passes: int) -> None:
        self.name = name
        self.fails = fails
        self.passes = passes
        self.waits: list[float] = []

    async def run_once(self, conn: AsyncConnection) -> None:
        await conn.execute(text("SELECT 1"))
        if self.fails:
            raise RuntimeError("every pass fails")

    async def pause(self, stop: asyncio.Event, seconds: float) -> None:
        self.waits.append(seconds)
        if len(self.waits) >= self.passes:
            stop.set()


async def test_a_loop_that_fails_every_pass_is_recorded_and_logs_its_real_wait(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    loop = _Counted("test_failing", fails=True, passes=4)
    with caplog.at_level(logging.ERROR, logger="proxyhub.reconcilers.base"):
        await loop.loop(engine, asyncio.Event())
    record = await _record(engine, "test_failing")
    assert record is not None and "ok_at" not in record
    assert record["failures"] == 4 and record["interval_s"] == 15.0 and "error_at" in record
    assert loop.waits == [30.0, 60.0, 120.0, 240.0]
    logged = [float(r.getMessage().rsplit(" ", 1)[1].rstrip("s")) for r in caplog.records]
    assert logged == loop.waits  # the log says the wait that really follows


async def test_a_loop_that_works_records_its_last_pass_and_a_failure_streak_ends(engine: AsyncEngine) -> None:
    loop = _Counted("test_working", fails=False, passes=2)
    await loop.loop(engine, asyncio.Event())
    record = await _record(engine, "test_working")
    assert record is not None and record["failures"] == 0 and "ok_at" in record
    assert loop.waits == [15.0, 15.0]
    # Failures, then one good pass: written at once (not only once a minute), the streak ends.
    await _Counted("test_working", fails=True, passes=2).loop(engine, asyncio.Event())
    assert (await _record(engine, "test_working") or {})["failures"] == 2
    await _Counted("test_working", fails=False, passes=1).loop(engine, asyncio.Event())
    after = await _record(engine, "test_working")
    assert after is not None and after["failures"] == 0 and "error_at" not in after


class _Ends(Reconciler):
    name = "test_ends"

    async def run_once(self, conn: AsyncConnection) -> None:
        return None

    async def loop(self, engine: AsyncEngine, stop: asyncio.Event) -> None:
        raise asyncio.CancelledError


class _Waits(Reconciler):
    name = "test_waits"

    async def run_once(self, conn: AsyncConnection) -> None:
        return None

    async def loop(self, engine: AsyncEngine, stop: asyncio.Event) -> None:
        await stop.wait()


async def test_a_loop_task_that_ends_stops_the_worker_with_an_error(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    async with engine.begin() as conn:  # a record of a loop the worker no longer runs
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES (:k, '{\"failures\": 0}'::jsonb)"),
            {"k": LOOP_KEY_PREFIX + "renamed_long_ago"},
        )
    with caplog.at_level(logging.CRITICAL, logger="proxyhub.worker"), pytest.raises(SystemExit) as exited:
        await asyncio.wait_for(worker.run([_Waits(), _Ends()]), timeout=30)
    assert exited.value.code == 1
    assert any("test_ends" in r.getMessage() for r in caplog.records)
    assert await _record(engine, "renamed_long_ago") is None
