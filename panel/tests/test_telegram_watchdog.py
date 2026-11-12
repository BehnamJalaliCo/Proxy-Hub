"""Worker's API watchdog against a REAL uvicorn server running the panel app (then stopped), and the
daily read of the stored channels.

The message itself only goes out with a real bot (tests/test_telegram_real.py); here the failure
counting, the 3-in-a-row threshold and "nobody to tell / not delivered" are checked for real. The
channel read's answers come from a real bot only (tests/test_telegram_real.py); here what a read
changes, the same-chat guard and a failed read (a refused local port) are checked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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

from proxyhub.fleet.actions import iso
from proxyhub.reconcilers.telegram import (
    ApiWatchdogReconciler,
    TelegramChannelsReconciler,
    apply_channel_reads,
    channels_due,
)
from proxyhub.telegram import store
from proxyhub.telegram.client import ChatInfo, MemberRights
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


# --- the stored channels, read again once a day -------------------------------------------------------

MAIN_CHAT = -1001111111111
TEST_CHAT = -1009876543210
SET_AT = "2026-11-03T20:00:00+00:00"


def stored_channel(chat_id: int, username: str) -> dict[str, object]:
    return {"chat_id": chat_id, "username": username, "title": "t", "can_post": True, "can_edit": True,
            "can_delete": False, "checked_at": SET_AT}  # fmt: skip


async def bot_with_channels(engine: AsyncEngine, vault: Vault) -> None:
    async with engine.begin() as conn:
        now = datetime.now(UTC)
        await store.save_bot(
            conn, vault, info=store.BotInfo(id=1234567, username="proxyhub_test_bot"),
            token=SecretValue(random_token()), now=now,
        )  # fmt: skip
        await store.set_owner_chat(conn, 111222333, now)
        await store.save_channel(conn, "main", stored_channel(MAIN_CHAT, "@ipoxxo"))
        await store.save_channel(conn, "test", stored_channel(TEST_CHAT, "@ipoxxo_test"))


async def test_a_fresh_read_updates_the_same_channel_only_and_tells_the_owner_once(
    engine: AsyncEngine, vault: Vault
) -> None:
    await bot_with_channels(engine, vault)
    now = datetime.now(UTC)
    member = MemberRights(status="administrator", can_post=True, can_edit=False, can_remove=False)
    renamed = store.channel_record(
        ChatInfo(id=MAIN_CHAT, type="channel", title="t2", username="newname"), member, now
    )
    # The owner set another test channel while the read was in flight: that record is left alone.
    other = store.channel_record(ChatInfo(id=-1005, type="channel", title="x", username="x"), member, now)
    try:
        for _ in range(2):  # the same read twice: one change, one log line, the owner told once
            async with engine.begin() as conn:
                changed = await apply_channel_reads(conn, {"main": renamed, "test": other}, {}, now)
            assert changed in (["main"], [])
        async with engine.connect() as conn:
            channels = await store.channels(conn)
            check = await store.get_setting(conn, store.KEY_CHANNELS_CHECK)
            alerts = (
                await conn.execute(text("SELECT payload FROM outbox WHERE kind = 'alert' ORDER BY id"))
            ).all()
            logged = (
                await conn.execute(
                    text(
                        "SELECT target, reasons FROM actions_log WHERE action = 'telegram.channel_refreshed'"
                    )
                )
            ).all()
        assert channels["main"] == renamed and channels["main"]["chat_id"] == MAIN_CHAT
        assert channels["test"] == stored_channel(TEST_CHAT, "@ipoxxo_test")
        assert check == {"at": iso(now), "errors": {}}
        assert [r.target for r in logged] == ["main:@newname"]
        assert logged[0].reasons["reasons"] == ["username", "title", "can_edit"]
        texts = [r.payload["text"] for r in alerts]
        assert len(texts) == 2 and "@newname" in texts[0] and "ویرایش پیام" in texts[1]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM actions_log WHERE action = 'telegram.channel_refreshed'"))


async def test_a_failed_read_keeps_the_record_and_is_tried_again_an_hour_later(
    engine: AsyncEngine, vault: Vault
) -> None:
    loop = TelegramChannelsReconciler(vault_factory=lambda: vault, bot_factory=closed_port_bot())
    assert await loop.try_run_once(engine) and loop.last_outcome == "no_bot"
    await bot_with_channels(engine, vault)
    async with engine.connect() as conn:
        before = await store.channels(conn)
    assert await loop.try_run_once(engine) and loop.last_outcome == "failed"  # Telegram unreachable
    async with engine.connect() as conn:
        assert await store.channels(conn) == before  # nothing dropped, nothing rewritten
        check = await store.get_setting(conn, store.KEY_CHANNELS_CHECK)
    assert check is not None and check["errors"] == {"main": "NetworkError", "test": "NetworkError"}
    assert await loop.try_run_once(engine) and loop.last_outcome == "not_due"
    last = datetime.fromisoformat(check["at"])
    assert not channels_due(check, last + timedelta(minutes=59)) and channels_due(
        check, last + timedelta(hours=1)
    )
