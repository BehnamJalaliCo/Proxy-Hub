"""REAL bot tests (marker ``telegram``): run on the panel server with a real test bot.

Environment (skipped without it, tests/conftest.py):
  PROXYHUB_TELEGRAM_TEST_TOKEN_FILE  a 0600 file holding the TEST bot's token (never the value itself)
  PROXYHUB_TELEGRAM_TEST_OWNER_CHAT  the owner's numeric private chat id (the owner sent /start to the bot)
  PROXYHUB_TELEGRAM_TEST_CHANNEL     the private test channel: numeric id (-100...) or @name; the bot is
                                     an admin there with "post" and "edit" (and without "delete")
Nothing here removes a message (rule 6): posts are sent and edited only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF, assert_absent_from_database, make_vault
from test_telegram_captured import FIRING_WORKER_STALE, RESOLVED_WORKER_STALE
from test_telegram_support import LiveApi, all_logs, assert_no_token, clean_telegram_tables

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_telegram import get_bot_factory, get_vault, render_test_post
from proxyhub.config import read_secret_file
from proxyhub.reconcilers.telegram import ApiWatchdogReconciler
from proxyhub.telegram import outbox, store
from proxyhub.telegram.alerts import ALERT_DEDUPER, ALERT_TARGETS
from proxyhub.telegram.client import BadRequest, BotClient
from proxyhub.telegram.templates import render_proxy_post
from proxyhub.vault import SecretValue, Vault

pytestmark = pytest.mark.telegram


def _token() -> str:
    path = os.environ.get("PROXYHUB_TELEGRAM_TEST_TOKEN_FILE", "")
    return read_secret_file(Path(path))


def _owner_chat() -> int:
    value = os.environ.get("PROXYHUB_TELEGRAM_TEST_OWNER_CHAT")
    if not value:
        pytest.skip("PROXYHUB_TELEGRAM_TEST_OWNER_CHAT not set")
    return int(value)


def _channel() -> str | int:
    value = os.environ.get("PROXYHUB_TELEGRAM_TEST_CHANNEL")
    if not value:
        pytest.skip("PROXYHUB_TELEGRAM_TEST_CHANNEL not set")
    return int(value) if value.lstrip("-").isdigit() else value


async def test_get_me() -> None:
    token = _token()
    with all_logs(logging.DEBUG) as logs:
        async with BotClient(token) as bot:
            me = await bot.get_me()
    assert me.id == int(token.split(":")[0]) and me.username
    assert_no_token(token, logs.getvalue())


async def test_send_and_edit_in_the_test_channel() -> None:
    token, channel = _token(), _channel()
    with all_logs(logging.DEBUG) as logs:
        async with BotClient(token) as bot:
            me = await bot.get_me()
            chat = await bot.get_chat(channel)
            assert chat.type == "channel"
            rights = await bot.get_chat_member(chat.id, me.id)
            assert rights.status == "administrator" and rights.can_post and rights.can_edit
            first = render_test_post(None)
            sent = await bot.send_message(chat.id, first)
            assert sent.chat_id == chat.id
            edited_text = first + "\n\n⌛️ زمان این کانفیگ تمام شد"
            edited = await bot.edit_message_text(chat.id, sent.message_id, edited_text)
            assert edited.message_id == sent.message_id
            with pytest.raises(BadRequest) as same:
                await bot.edit_message_text(chat.id, sent.message_id, edited_text)
            assert same.value.not_modified
    if rights.can_remove:
        logging.getLogger(__name__).warning(
            "the test bot may remove posts in the test channel; it should not"
        )
    assert_no_token(token, logs.getvalue())


async def test_a_long_post_with_proxy_words_is_accepted_by_telegram() -> None:
    token, channel = _token(), _channel()
    secret = "0123456789abcdef0123456789abcdef"
    post = render_proxy_post(
        "<b>آزمون قالب</b>\n" + "متن بلند " * 800,
        [(f"10.0.0.{i}", 443, secret) for i in range(1, 9)],
        "@ipoxxo",
    )
    async with BotClient(token) as bot:
        sent = await bot.send_message(channel, post)
    assert sent.message_id > 0


async def test_send_to_the_owner_chat() -> None:
    token, owner = _token(), _owner_chat()
    async with BotClient(token) as bot:
        sent = await bot.send_message(owner, "🧪 <b>آزمون Proxy-Hub</b>\nپیام به چت خصوصی مالک.")
    assert sent.chat_id == owner


# --- the whole path: API -> outbox -> Telegram, and the Alertmanager route -----------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_telegram_tables(eng)
    ALERT_DEDUPER.clear()
    ALERT_TARGETS.clear()
    yield eng
    await clean_telegram_tables(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    app.dependency_overrides[get_bot_factory] = lambda: BotClient
    try:
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        for dep in (require_admin, get_vault, get_bot_factory):
            app.dependency_overrides.pop(dep, None)


async def run_outbox(engine: AsyncEngine, vault: Vault) -> str:
    for _ in range(20):
        outcome = await outbox.process_one(engine, vault, BotClient)
        if outcome != "idle":
            return outcome
        await asyncio.sleep(1)  # pacing
    return "idle"


@pytest.mark.db
@pytest.mark.redis
async def test_full_flow(client: AsyncClient, engine: AsyncEngine, vault: Vault) -> None:
    token, channel, owner = _token(), _channel(), _owner_chat()
    with all_logs(logging.DEBUG) as logs:
        r = await client.put("/api/telegram/bot", json={"token": token})
        assert r.status_code == 200, r.text
        assert r.json()["bot"]["id"] == int(token.split(":")[0])
        r = await client.put("/api/telegram/channel", json={"role": "test", "username": str(channel)})
        assert r.status_code == 200, r.text
        test_channel = r.json()["channels"]["test"]
        assert test_channel["can_post"] and test_channel["can_edit"]

        # Test post through the outbox, recorded with its message_id.
        job_id = (await client.post("/api/telegram/test-post")).json()["outbox_id"]
        assert await run_outbox(engine, vault) == "sent"
        async with engine.connect() as conn:
            post_id, message_id = (
                await conn.execute(
                    text("SELECT id, message_id FROM channel_posts WHERE outbox_id = :j"), {"j": job_id}
                )
            ).one()
        assert message_id > 0
        # Edit it (never removed): a status line, as for a finished config (docs/04).
        async with engine.begin() as conn:
            await store.enqueue(
                conn,
                "edit_post",
                {"post_id": post_id, "text": render_test_post(None) + "\n\n🚫 آزمون ویرایش"},
            )
        assert await run_outbox(engine, vault) == "sent"

        # Owner chat (pairing itself needs the owner to send /start; the chat id comes from the env).
        async with engine.begin() as conn:
            await store.set_owner_chat(conn, owner, datetime.now(UTC))
        assert (await client.post("/api/telegram/test-alert")).status_code == 202
        assert await run_outbox(engine, vault) == "sent"

        # Alertmanager's real bodies, delivered directly; the same notification again is a duplicate.
        for raw in (FIRING_WORKER_STALE, RESOLVED_WORKER_STALE):
            r = await client.post(
                "/internal/alerts/alertmanager", content=raw, headers={"Content-Type": "application/json"}
            )
            assert r.status_code == 200 and r.json()["delivered"] == 1, r.text
        r = await client.post(
            "/internal/alerts/alertmanager",
            content=FIRING_WORKER_STALE,
            headers={"Content-Type": "application/json"},
        )
        assert r.json() == {"delivered": 0, "duplicates": 1}
        status = (await client.get("/api/telegram/status")).json()
    assert status["owner_paired"] and status["last_alert_at"] and status["outbox"]["failed"] == 0
    assert_no_token(token, logs.getvalue(), str(status))
    await assert_absent_from_database(engine, token)
    await assert_absent_from_database(engine, token.split(":", 1)[1])


@pytest.mark.db
async def test_api_watchdog_tells_the_owner_and_then_the_recovery(engine: AsyncEngine, vault: Vault) -> None:
    token, owner = _token(), _owner_chat()
    async with BotClient(token) as bot:
        me = await bot.get_me()
    async with engine.begin() as conn:
        now = datetime.now(UTC)
        await store.save_bot(
            conn, vault, info=store.BotInfo(id=me.id, username=me.username), token=SecretValue(token), now=now
        )
        await store.set_owner_chat(conn, owner, now)
    api = LiveApi()
    await api.start()
    dog = ApiWatchdogReconciler(url=api.url, vault_factory=lambda: vault)
    await dog.try_run_once(engine)
    await api.stop()
    for _ in range(3):
        await dog.try_run_once(engine)
    assert dog.alerted is True  # the owner got "API پنل از دسترس خارج است"
    await api.start()
    await dog.try_run_once(engine)
    assert dog.alerted is False  # and "API پنل دوباره در دسترس است"
    await api.stop()
