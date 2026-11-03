"""Worker's API watchdog against a REAL uvicorn server running the panel app (then stopped).

The message itself only goes out with a real bot (tests/test_telegram_real.py); here the failure
counting, the 3-in-a-row threshold and "nobody to tell / not delivered" are checked for real.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import make_vault
from test_telegram_support import (
    LiveApi,
    SilentServer,
    all_logs,
    assert_no_token,
    clean_telegram_tables,
    closed_port_bot,
    random_token,
)

from proxyhub.reconcilers.telegram import ApiWatchdogReconciler
from proxyhub.telegram import store
from proxyhub.vault import SecretValue, Vault

pytestmark = pytest.mark.db


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_telegram_tables(eng)
    yield eng
    await clean_telegram_tables(eng)
    await eng.dispose()


@pytest.fixture
async def api() -> AsyncIterator[LiveApi]:
    live = LiveApi()
    await live.start()
    yield live
    await live.stop()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


async def test_three_failures_in_a_row_then_tell_the_owner(
    engine: AsyncEngine, api: LiveApi, vault: Vault
) -> None:
    dog = ApiWatchdogReconciler(url=api.url, vault_factory=lambda: vault, bot_factory=closed_port_bot())
    assert await dog.try_run_once(engine)
    assert (dog.failures, dog.alerted) == (0, False)
    await api.stop()
    with all_logs(logging.INFO) as logs:
        for expected in (1, 2):
            await dog.try_run_once(engine)
            assert dog.failures == expected
        assert "nobody to tell" not in logs.getvalue()  # no message before the third failure
        await dog.try_run_once(engine)
    assert dog.failures == 3 and dog.alerted is False
    assert "api watchdog: no bot or no paired owner chat; nobody to tell" in logs.getvalue()

    token = random_token()
    async with engine.begin() as conn:
        now = datetime.now(UTC)
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=1234567, username="proxyhub_test_bot"),
            token=SecretValue(token),
            now=now,
        )
        await store.set_owner_chat(conn, 111222333, now)
    with all_logs(logging.INFO) as logs:
        await dog.try_run_once(engine)  # 4th failure: tries again because nothing was delivered
    assert dog.failures == 4 and dog.alerted is False
    assert (
        "api watchdog: message not delivered (sendMessage: network error (ConnectError))" in logs.getvalue()
    )
    assert_no_token(token, logs.getvalue())

    await api.start()
    await dog.try_run_once(engine)
    assert (dog.failures, dog.alerted) == (0, False)


async def test_no_transaction_stays_open_while_telegram_is_called(
    engine: AsyncEngine, api: LiveApi, vault: Vault
) -> None:
    async with engine.begin() as conn:
        now = datetime.now(UTC)
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=1234567, username="proxyhub_test_bot"),
            token=SecretValue(random_token()),
            now=now,
        )
        await store.set_owner_chat(conn, 111222333, now)
    await api.stop()
    async with SilentServer() as hung:  # Telegram that never answers: the send hangs
        dog = ApiWatchdogReconciler(
            url=api.url, failures_to_alert=1, vault_factory=lambda: vault, bot_factory=hung.bot()
        )
        task = asyncio.create_task(dog.try_run_once(engine))
        try:
            for _ in range(200):
                if hung.connections:
                    break
                await asyncio.sleep(0.05)
            assert hung.connections == 1  # the alert is being sent right now
            async with engine.connect() as conn:
                idle_in_tx = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND state LIKE 'idle in transaction%' AND pid <> pg_backend_pid()"
                        )
                    )
                ).scalar_one()
            assert idle_in_tx == 0
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
