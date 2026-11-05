"""The operator poster's daily reads on the real PostgreSQL: the owner's emoji pack and the account's
rights in our channels (decision D38). Telegram is never reached (tests/test_operator_poster_support.py)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from telethon import errors, functions
from test_operator_poster_support import (
    EMOJI_SET,
    MAIN_NAME,
    NOW,
    TEST_NAME,
    checks_answers,
    clean_operator,
    participant,
    setup_operator,
    store_pack,
    telegram_answers,
)
from test_posts_support import MAIN_CHAT, TEST_CHAT, channel, clean_posts_tables, vault_at

from proxyhub.mtproxy import store as mtp_store
from proxyhub.posts import operator, operator_checks
from proxyhub.reconcilers.base import lock_key
from proxyhub.telegram import store
from proxyhub.vault import Vault

pytestmark = pytest.mark.db

ENTRIES = [(5_001, "📡"), (5_002, "⌛️"), (5_003, "🔗"), (5_004, "📡")]


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_posts_tables(eng)
    await clean_operator(eng)
    yield eng
    await clean_posts_tables(eng)
    await clean_operator(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return vault_at(tmp_path)


async def check(engine: AsyncEngine, vault: Vault, *, force: bool = False, at: Any = NOW) -> str:
    return await operator_checks.run_checks(engine, vault, now=at, force=force, lock_wait_s=0.5)


async def view(engine: AsyncEngine) -> dict[str, Any]:
    async with engine.connect() as conn:
        return await operator.view(conn, NOW)


async def test_nothing_is_read_unless_the_owner_wants_the_operator(engine: AsyncEngine, vault: Vault) -> None:
    await setup_operator(engine, vault, poster="bot", emoji_pack=None)
    assert await check(engine, vault) == "idle"
    await setup_operator(engine, vault, poster="bot", emoji_pack=None, kill_switch=True)
    assert await check(engine, vault, force=True) == "kill_switch"  # "check now" still obeys it
    async with engine.begin() as conn:
        await mtp_store.drop_secrets(conn)
    await setup_operator(engine, vault, connected=False)
    assert await check(engine, vault) == "not_connected"
    assert await view(engine) == {**await view(engine), "pack": None, "rights_checked_at": None}


async def test_the_pack_and_the_rights_are_read_once_a_day(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault, main=True)
    async with engine.begin() as conn:
        await store.save_channel(conn, "main", channel(MAIN_CHAT, f"@{MAIN_NAME}"))
    with telegram_answers(monkeypatch, checks_answers(ENTRIES)) as requests:
        assert await check(engine, vault) == "ok"
    kinds = [type(r).__name__ for r in requests]
    assert kinds == [
        "GetStickerSetRequest",
        "ResolveUsernameRequest",
        "GetParticipantRequest",
        "ResolveUsernameRequest",
        "GetParticipantRequest",
    ]
    assert requests[0].stickerset.short_name == EMOJI_SET
    shown = await view(engine)
    pack = shown["pack"]
    assert (pack["short_name"], pack["title"], pack["count"], pack["error"]) == (EMOJI_SET, "متحرک", 4, None)
    assert pack["emoji"] == [
        {"emoji": "📡", "id": "5001"},
        {"emoji": "⌛️", "id": "5002"},
        {"emoji": "🔗", "id": "5003"},
    ]  # one per emoji; document ids as strings (64-bit ids do not fit a JavaScript number)
    for role in ("test", "main"):
        ch = shown["channels"][role]
        assert ch["rights"] == {"status": "admin", "can_post": True, "can_edit": True, "can_delete": False,
                                "error": None, "telegram_error": None}  # fmt: skip
        assert ch["warnings"] == [] and ch["public"] is True and ch["ready"] is None
    async with engine.connect() as conn:
        assert await operator.emoji_map(conn) == {"📡": 5_001, "⌛": 5_002, "🔗": 5_003}
    # A day has not passed: nothing is read. Then a new link: only the pack is read again.
    with telegram_answers(monkeypatch, checks_answers(ENTRIES)) as requests:
        assert await check(engine, vault, at=NOW + timedelta(hours=23)) == "not_due"
        async with engine.begin() as conn:
            await operator.save_config(
                conn, operator.OperatorConfig(poster="operator", emoji_pack="Another_Pack")
            )
        async with engine.connect() as conn:
            assert await operator.emoji_map(conn) == {}  # the old pack is never used for the new link
        assert await check(engine, vault, at=NOW + timedelta(hours=23)) == "ok"
    assert [type(r).__name__ for r in requests] == ["GetStickerSetRequest"]
    assert requests[0].stickerset.short_name == "Another_Pack"
    with telegram_answers(monkeypatch, checks_answers(ENTRIES)) as requests:
        assert await check(engine, vault, at=NOW + timedelta(days=1, minutes=1)) == "ok"
    assert [type(r).__name__ for r in requests].count("GetParticipantRequest") == 2


@pytest.mark.parametrize(
    ("rights", "warnings"),
    [
        (participant(delete=True), ["delete_right"]),
        (participant(post=False), ["no_post_right"]),
        (participant(edit=False, delete=True), ["no_edit_right", "delete_right"]),
    ],
)
async def test_wrong_rights_are_warnings_and_a_missing_post_right_sends_posts_to_the_bot(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch, rights: Any, warnings: list[str]
) -> None:
    await setup_operator(engine, vault)
    with telegram_answers(monkeypatch, checks_answers(ENTRIES, rights=rights)):
        assert await check(engine, vault) == "ok"
    test = (await view(engine))["channels"]["test"]
    assert test["warnings"] == warnings
    assert test["ready"] == ("no_post_right" if "no_post_right" in warnings else None)


async def test_not_a_member_a_private_channel_and_a_refused_pack(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault, main=True)
    async with engine.begin() as conn:  # the main channel here has no public name
        await store.save_channel(conn, "main", channel(MAIN_CHAT))
    not_member = errors.UserNotParticipantError(None)
    invalid = errors.StickersetInvalidError(None)
    with telegram_answers(
        monkeypatch, checks_answers(ENTRIES, pack_error=invalid, rights_error=not_member)
    ) as requests:
        assert await check(engine, vault) == "partial"
    assert [r.username for r in requests if isinstance(r, functions.contacts.ResolveUsernameRequest)] == [
        TEST_NAME
    ]
    shown = await view(engine)
    assert shown["channels"]["test"]["rights"]["status"] == "not_member"
    assert shown["channels"]["main"]["rights"]["status"] == "no_username"
    assert (
        shown["channels"]["main"]["public"] is False and shown["channels"]["main"]["ready"] == "no_username"
    )
    assert (shown["pack"]["error"], shown["pack"]["telegram_error"]) == (
        "telegram_refused",
        "STICKERSET_INVALID",
    )
    assert shown["pack"]["emoji"] == []


async def test_a_sticker_pack_is_not_an_emoji_pack_and_a_failed_read_keeps_the_last_emoji(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault)
    with telegram_answers(monkeypatch, checks_answers(ENTRIES, emojis=False)):
        assert await check(engine, vault) == "partial"
    assert (await view(engine))["pack"]["error"] == "not_emoji_pack"
    await store_pack(engine, ENTRIES)
    with telegram_answers(
        monkeypatch, checks_answers(ENTRIES, pack_error=errors.StickersetInvalidError(None))
    ):
        assert await check(engine, vault, force=True) == "partial"
    pack = (await view(engine))["pack"]
    assert pack["error"] == "telegram_refused" and pack["count"] == 4  # the emoji read last time stay
    async with engine.connect() as conn:
        assert await operator.emoji_map(conn) != {}


async def test_a_real_connection_failure_and_a_busy_account(engine: AsyncEngine, vault: Vault) -> None:
    await setup_operator(engine, vault)
    assert await check(engine, vault) == "failed:telegram_unavailable"  # a real refusal, closed port
    shown = await view(engine)
    assert shown["pack"]["error"] == "telegram_unavailable"
    assert shown["channels"]["test"]["rights"]["status"] == "error"
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key(mtp_store.LOCK_NAME)})
        await holder.commit()
        assert await check(engine, vault, force=True) == "busy"
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(mtp_store.LOCK_NAME)})
        await holder.commit()
    # After a failure the next try comes after an hour, not a day.
    assert await check(engine, vault, at=NOW + timedelta(minutes=30)) == "not_due"
    assert await check(engine, vault, at=NOW + timedelta(minutes=61)) == "failed:telegram_unavailable"
    assert TEST_CHAT
