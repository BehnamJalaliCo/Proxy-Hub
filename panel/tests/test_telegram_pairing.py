"""Owner pairing on a real PostgreSQL: single use, 10-minute expiry, wrong-code limit, and the
updates loop that binds the owner's private chat while storing nothing about anyone else.

Updates are the real Bot API ``Update`` JSON shape (core.telegram.org/bots/api#update) used as data.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import assert_absent_from_database, make_vault
from test_telegram_support import (
    SilentServer,
    assert_no_token,
    clean_telegram_tables,
    closed_port_bot,
    random_token,
    real_bot,
    telegram_reachable,
)

from proxyhub.reconcilers.telegram import TelegramUpdatesReconciler, handle_update
from proxyhub.telegram import pairing, store
from proxyhub.telegram.client import Update
from proxyhub.vault import SecretValue, Vault

pytestmark = pytest.mark.db

OWNER = 111222333
STRANGER = 444555666
STRANGER_NAME = "StrangerName" + "Zq7"


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_telegram_tables(eng)
    yield eng
    await clean_telegram_tables(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


def private_message(update_id: int, chat_id: int, text_value: str, first_name: str = "Owner") -> Update:
    return Update.from_result(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id % 1000,
                "from": {
                    "id": chat_id,
                    "is_bot": False,
                    "first_name": first_name,
                    "username": f"{first_name.lower()}_u",
                    "language_code": "fa",
                },
                "chat": {
                    "id": chat_id,
                    "first_name": first_name,
                    "username": f"{first_name.lower()}_u",
                    "type": "private",
                },
                "date": 1793734000,
                "text": text_value,
                "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
            },
        }
    )


async def new_code(engine: AsyncEngine, now: datetime) -> str:
    code = pairing.new_code()
    async with engine.begin() as conn:
        await store.create_pairing(conn, code, now)
    return code


async def test_code_is_single_use(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    code = await new_code(engine, now)
    async with engine.begin() as conn:
        assert await store.consume_pairing(conn, code, now + timedelta(minutes=1))
        assert not await store.consume_pairing(conn, code, now + timedelta(minutes=1))
        assert await store.open_pairing(conn, now) is None


async def test_code_expires_after_ten_minutes(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    code = await new_code(engine, now)
    async with engine.begin() as conn:
        assert (await store.open_pairing(conn, now + timedelta(minutes=9, seconds=59))) is not None
        assert await store.open_pairing(conn, now + timedelta(minutes=10)) is None
        assert not await store.consume_pairing(conn, code, now + timedelta(minutes=10))


async def test_new_code_cancels_the_old_one(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    old = await new_code(engine, now)
    new = await new_code(engine, now + timedelta(seconds=5))
    async with engine.begin() as conn:
        assert not await store.consume_pairing(conn, old, now + timedelta(seconds=6))
        assert await store.consume_pairing(conn, new, now + timedelta(seconds=6))


async def test_twenty_wrong_codes_cancel_the_pairing(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    code = await new_code(engine, now)
    async with engine.begin() as conn:
        results = [await store.count_wrong_code(conn, now) for _ in range(pairing.MAX_WRONG_CODES)]
        assert results == [False] * (pairing.MAX_WRONG_CODES - 1) + [True]
        assert not await store.consume_pairing(conn, code, now)
        assert not await store.count_wrong_code(conn, now)  # nothing open any more


async def test_start_code_from_the_owner_binds_the_private_chat(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    code = await new_code(engine, now)
    async with engine.begin() as conn:
        assert await handle_update(conn, private_message(1, OWNER, f"/start {code.lower()}"), now) == OWNER
        assert await store.owner_chat_id(conn) == OWNER
    async with engine.connect() as conn:
        logged = (
            await conn.execute(
                text(
                    "SELECT action, target, CAST(reasons AS text) FROM actions_log "
                    "WHERE action LIKE 'telegram.%'"
                )
            )
        ).all()
    assert [(a, t) for a, t, _ in logged] == [("telegram.owner_paired", "owner_chat")]
    assert str(OWNER) not in logged[0][2]


async def test_everyone_else_is_ignored_and_not_stored(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    code = await new_code(engine, now)
    group = Update.from_result(
        {
            "update_id": 5,
            "message": {
                "message_id": 1,
                "chat": {"id": -100777, "title": "g", "type": "supergroup"},
                "from": {"id": STRANGER, "is_bot": False, "first_name": STRANGER_NAME},
                "date": 1793734000,
                "text": f"/start {code}",
            },
        }
    )
    async with engine.begin() as conn:
        assert await handle_update(conn, group, now) is None  # right code, but not a private chat
        assert await handle_update(conn, private_message(6, STRANGER, "hello", STRANGER_NAME), now) is None
        assert await handle_update(conn, private_message(7, STRANGER, "/start", STRANGER_NAME), now) is None
        assert (
            await handle_update(conn, private_message(8, STRANGER, "/start WRONGCOD", STRANGER_NAME), now)
            is None
        )
        assert await store.owner_chat_id(conn) is None
        wrong = (await conn.execute(text("SELECT wrong_codes FROM telegram_pairings"))).scalar_one()
    assert wrong == 1  # only the counter; nothing about the sender
    for needle in (str(STRANGER), STRANGER_NAME, STRANGER_NAME.lower()):
        await assert_absent_from_database(engine, needle)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM tg_updates"))).scalar_one() == 0
    # The real owner can still pair afterwards.
    async with engine.begin() as conn:
        assert await handle_update(conn, private_message(9, OWNER, f"/start {code}"), now) == OWNER


# --- the loop ------------------------------------------------------------------------------------


async def configure_bot(engine: AsyncEngine, vault: Vault) -> str:
    token = random_token()
    async with engine.begin() as conn:
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=8123456789, username="proxyhub_test_bot"),
            token=SecretValue(token),
            now=datetime.now(UTC),
        )
    return token


async def updates_state(engine: AsyncEngine) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        return await store.get_setting(conn, store.KEY_UPDATES)


async def test_loop_idles_without_a_bot(engine: AsyncEngine, vault: Vault) -> None:
    loop = TelegramUpdatesReconciler(vault_factory=lambda: vault, bot_factory=closed_port_bot())
    assert await loop.try_run_once(engine)
    assert loop.interval_seconds == loop.IDLE_SECONDS
    assert await updates_state(engine) is None


async def test_loop_records_an_unreachable_telegram(engine: AsyncEngine, vault: Vault) -> None:
    token = await configure_bot(engine, vault)
    loop = TelegramUpdatesReconciler(vault_factory=lambda: vault, bot_factory=closed_port_bot())
    assert await loop.try_run_once(engine)
    state = await updates_state(engine)
    assert state is not None and state["error"] == "telegram_unavailable" and state["offset"] is None
    assert loop.interval_seconds == loop.ERROR_SECONDS
    assert_no_token(token, str(state))


async def test_stopping_the_worker_ends_a_long_poll_at_once(engine: AsyncEngine, vault: Vault) -> None:
    # Docker stops a container after 10 s; a 25 s long poll (40 s read timeout) must not hold it.
    await configure_bot(engine, vault)
    async with SilentServer() as hung:  # accepts getUpdates and never answers, like a long poll
        loop = TelegramUpdatesReconciler(vault_factory=lambda: vault, bot_factory=hung.bot())
        stop = asyncio.Event()
        task = asyncio.create_task(loop.loop(engine, stop))
        for _ in range(100):
            if hung.connections:
                break
            await asyncio.sleep(0.05)
        assert hung.connections == 1  # the poll is in flight
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        assert time.monotonic() - started < 2
    assert await updates_state(engine) is None  # nothing recorded: the offset did not move


async def test_loop_with_real_telegram_and_a_revoked_token(engine: AsyncEngine, vault: Vault) -> None:
    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    await configure_bot(engine, vault)
    loop = TelegramUpdatesReconciler(vault_factory=lambda: vault, bot_factory=real_bot, long_poll_s=1)
    assert await loop.try_run_once(engine)
    state = await updates_state(engine)
    assert state is not None and state["error"] == "bot_unauthorized"
    async with engine.connect() as conn:
        health = await store.get_setting(conn, store.KEY_HEALTH)
    assert health is not None and health["error"] == "bot_unauthorized"
    assert loop.interval_seconds == 60.0
