"""Outbox processor on a real PostgreSQL: claim before send, record message_id + done in one
transaction, pacing, no-post windows, 429/401/400 handling, at-most-once for channel posts.

Telegram outcomes come from three real sources only: the Bot API's real error JSON shapes parsed by
our real parser, the real Bot API answering a random token with 401, and a real refused connection.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from test_fleet_support import make_vault
from test_telegram_support import (
    BOT_API_ERRORS,
    assert_no_token,
    clean_telegram_tables,
    closed_port_bot,
    random_token,
    real_bot,
    telegram_reachable,
)

from proxyhub.reconcilers.telegram import TelegramOutboxReconciler
from proxyhub.telegram import outbox, store
from proxyhub.telegram.client import NetworkError, TelegramError, parse_api_response
from proxyhub.telegram.schedule import TEHRAN, QuietWindow
from proxyhub.vault import SecretValue, Vault

pytestmark = pytest.mark.db

OWNER = 111222333
TEST_CHAT = -1009876543210
MAIN_CHAT = -1001111111111
STATUS = {
    "unauthorized": 401,
    "flood": 429,
    "chat_not_found": 400,
    "not_modified": 400,
    "kicked": 403,
    "server": 502,
}


def api_error(name: str) -> TelegramError:
    try:
        parse_api_response("sendMessage", STATUS[name], json.dumps(BOT_API_ERRORS[name]).encode())
    except TelegramError as exc:
        return exc
    raise AssertionError("not an error shape")


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


def channel(chat_id: int) -> dict[str, Any]:
    return {"chat_id": chat_id, "username": str(chat_id), "title": "t", "can_post": True, "can_edit": True,
            "can_delete": False, "checked_at": "2026-11-03T20:00:00Z"}  # fmt: skip


async def setup(engine: AsyncEngine, vault: Vault, *, token: str | None = None) -> str:
    token = token or random_token()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=8123456789, username="proxyhub_test_bot"),
            token=SecretValue(token),
            now=now,
        )
        await store.set_owner_chat(conn, OWNER, now)
        await store.save_channel(conn, "test", channel(TEST_CHAT))
        await store.save_channel(conn, "main", channel(MAIN_CHAT))
    return token


async def enqueue(conn: AsyncConnection, kind: str, payload: dict[str, Any]) -> int:
    """Due a few seconds ago (the column default is the database's transaction start time)."""
    return await store.enqueue(conn, kind, payload, not_before=datetime.now(UTC) - timedelta(seconds=5))


async def job_row(conn: AsyncConnection, job_id: int) -> Any:
    return (
        await conn.execute(
            text("SELECT attempts, last_error, done_at, not_before FROM outbox WHERE id = :i"), {"i": job_id}
        )
    ).one()


async def due_now(conn: AsyncConnection, job_id: int) -> None:
    await conn.execute(
        text("UPDATE outbox SET not_before = now() - interval '1 second' WHERE id = :i"), {"i": job_id}
    )


# --- success path --------------------------------------------------------------------------------


async def test_post_is_recorded_with_message_id_and_done_in_one_step(
    engine: AsyncEngine, vault: Vault
) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        job_id = await enqueue(conn, "send_post", {"role": "test", "text": "<b>x</b>", "post_kind": "test"})
        picked = await outbox.pick(conn, now)
    assert picked is not None
    job, target = picked
    assert (job.id, target.chat_id, target.role) == (job_id, TEST_CHAT, "test")
    async with engine.connect() as conn:
        assert (await job_row(conn, job_id)).last_error == "sending"  # claim committed before the send
    async with engine.begin() as conn:
        assert await outbox.record_success(conn, job, target, 4242, now) == "sent"
    async with engine.connect() as conn:
        row = await job_row(conn, job_id)
        post = (
            await conn.execute(
                text("SELECT role, chat_id, message_id, kind, text_sha256, outbox_id FROM channel_posts")
            )
        ).one()
        pace = await store.pacing(conn, TEST_CHAT)
    assert row.done_at is not None and row.last_error is None
    assert tuple(post) == ("test", TEST_CHAT, 4242, "test", store.text_sha256("<b>x</b>"), job_id)
    assert pace.last_sent_at == now
    # The same job can never be recorded as a second post.
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await outbox.record_success(conn, job, target, 4243, now)


async def test_edit_updates_the_recorded_post(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        post_id = (
            await conn.execute(
                text("INSERT INTO channel_posts (role, chat_id, message_id, kind, text_sha256) "
                     "VALUES ('test', :c, 77, 'test', :h) RETURNING id"),
                {"c": TEST_CHAT, "h": store.text_sha256("old")},
            )
        ).scalar_one()  # fmt: skip
        job_id = await enqueue(conn, "edit_post", {"post_id": post_id, "text": "new ⌛️"})
        job, target = await outbox.pick(conn, now) or (None, None)
    assert job is not None and target is not None and (target.message_id, target.post_id) == (77, post_id)
    async with engine.begin() as conn:
        await outbox.record_success(conn, job, target, None, now)
    async with engine.connect() as conn:
        edited = (await conn.execute(text("SELECT text_sha256, last_edited_at FROM channel_posts"))).one()
        assert edited.text_sha256 == store.text_sha256("new ⌛️") and edited.last_edited_at == now
        assert (await job_row(conn, job_id)).done_at is not None


# --- crash between claim and record --------------------------------------------------------------


async def test_interrupted_post_is_never_resent_but_alert_is(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        post_job = await enqueue(conn, "send_post", {"role": "test", "text": "p"})
        alert_job = await enqueue(conn, "alert", {"text": "a"})
        await outbox.claim(conn, post_job)
        await outbox.claim(conn, alert_job)
    async with engine.begin() as conn:
        assert await outbox.recover_interrupted(conn, now) == 2
    async with engine.connect() as conn:
        post = await job_row(conn, post_job)
        alert = await job_row(conn, alert_job)
        assert post.done_at is not None and post.last_error.startswith("failed: ambiguous_send")
        assert alert.done_at is None and alert.last_error == "interrupted"
        assert await store.outbox_counts(conn) == (1, 1)
    async with engine.begin() as conn:
        picked = await outbox.pick(conn, now)
    assert picked is not None and picked[0].id == alert_job


# --- pacing, no-post windows, interval -----------------------------------------------------------


async def test_pacing_per_chat(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await store.record_sent(conn, TEST_CHAT, now - timedelta(seconds=1))
        await store.record_sent(conn, OWNER, now - timedelta(milliseconds=500))
        await enqueue(conn, "send_post", {"role": "test", "text": "p"})
        alert = await enqueue(conn, "alert", {"text": "a"})
        assert await outbox.pick(conn, now) is None  # channel: 3 s gap; owner: 1 s gap
        picked = await outbox.pick(conn, now + timedelta(seconds=1))
        assert picked is not None and picked[0].id == alert
        await store.record_retry_after(conn, TEST_CHAT, now + timedelta(seconds=35))
        assert await outbox.pick(conn, now + timedelta(seconds=10)) is None
        assert await outbox.pick(conn, now + timedelta(seconds=36)) is not None


async def test_main_posts_wait_for_the_quiet_window_but_alerts_and_test_posts_do_not(
    engine: AsyncEngine, vault: Vault
) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    local = now.astimezone(TEHRAN)
    start = (local - timedelta(minutes=30)).strftime("%H:%M")
    end_local = (local + timedelta(hours=1)).replace(second=0, microsecond=0)
    async with engine.begin() as conn:
        await store.save_posting_settings(
            conn,
            store.PostingSettings(
                quiet_windows=[QuietWindow.parse(start, end_local.strftime("%H:%M"))], post_interval_min=0
            ),
        )
        main = await enqueue(conn, "send_post", {"role": "main", "text": "m"})
        picked = await outbox.pick(conn, now)
        assert picked is None
        assert (await job_row(conn, main)).not_before == end_local.astimezone(UTC)
        test_post = await enqueue(conn, "send_post", {"role": "test", "text": "t"})
        alert = await enqueue(conn, "alert", {"text": "a"})
        first = await outbox.pick(conn, now)
        second = await outbox.pick(conn, now)
    assert first is not None and second is not None
    assert {first[0].id, second[0].id} == {test_post, alert}


async def test_post_interval_after_the_last_main_post(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await store.save_posting_settings(conn, store.PostingSettings(quiet_windows=[], post_interval_min=60))
        await conn.execute(
            text("INSERT INTO channel_posts (role, chat_id, message_id, kind, text_sha256, created_at) "
                 "VALUES ('main', :c, 1, 'post', :h, :at)"),
            {"c": MAIN_CHAT, "h": "0" * 64, "at": now - timedelta(minutes=10)},
        )  # fmt: skip
        job = await enqueue(conn, "send_post", {"role": "main", "text": "m"})
        assert await outbox.pick(conn, now) is None
        assert (await job_row(conn, job)).not_before == now + timedelta(minutes=50)
        assert await outbox.pick(conn, now + timedelta(minutes=50)) is not None


# --- failures ------------------------------------------------------------------------------------


async def claimed(
    engine: AsyncEngine, kind: str, payload: dict[str, Any]
) -> tuple[outbox.Job, outbox.Target]:
    async with engine.begin() as conn:
        await enqueue(conn, kind, payload)
        picked = await outbox.pick(conn, datetime.now(UTC))
    assert picked is not None
    return picked


async def test_retry_after_waits_exactly_and_is_not_an_attempt(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    job, target = await claimed(engine, "send_post", {"role": "test", "text": "p"})
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        assert await outbox.record_failure(conn, job, target, api_error("flood"), now) == "retry"
        row = await job_row(conn, job.id)
        assert (row.attempts, row.done_at, row.not_before) == (0, None, now + timedelta(seconds=35))
        assert row.last_error == "retry_after 35s"
        assert (await store.pacing(conn, TEST_CHAT)).retry_until == now + timedelta(seconds=35)


async def test_revoked_token_pauses_the_outbox(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    job, target = await claimed(engine, "alert", {"text": "a"})
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await outbox.record_failure(conn, job, target, api_error("unauthorized"), now)
        assert (await job_row(conn, job.id)).attempts == 0
        assert await outbox.bot_paused(conn, now + timedelta(minutes=4))
        assert not await outbox.bot_paused(conn, now + timedelta(minutes=5))
    assert await outbox.process_one(engine, vault, closed_port_bot()) == "paused"


async def test_bad_request_fails_at_once_and_not_modified_is_done(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    job, target = await claimed(engine, "send_post", {"role": "test", "text": "p"})
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        assert await outbox.record_failure(conn, job, target, api_error("chat_not_found"), now) == "failed"
        assert (await job_row(conn, job.id)).last_error.startswith("failed: bad_request: sendMessage: 400")
        post_id = (
            await conn.execute(
                text("INSERT INTO channel_posts (role, chat_id, message_id, kind, text_sha256) "
                     "VALUES ('test', :c, 9, 'test', :h) RETURNING id"),
                {"c": TEST_CHAT, "h": "1" * 64},
            )
        ).scalar_one()  # fmt: skip
    edit, edit_target = await claimed(engine, "edit_post", {"post_id": post_id, "text": "same"})
    async with engine.begin() as conn:
        assert await outbox.record_failure(conn, edit, edit_target, api_error("not_modified"), now) == "sent"
        row = await job_row(conn, edit.id)
        assert row.done_at is not None and row.last_error is None


async def test_unknown_outcome_never_resends_a_post_but_retries_an_alert(
    engine: AsyncEngine, vault: Vault
) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    post, post_target = await claimed(engine, "send_post", {"role": "test", "text": "p"})
    async with engine.begin() as conn:
        lost = NetworkError("sendMessage", "ReadTimeout", maybe_sent=True)
        assert await outbox.record_failure(conn, post, post_target, lost, now) == "failed"
        assert "ambiguous_send" in (await job_row(conn, post.id)).last_error
    server, server_target = await claimed(engine, "send_post", {"role": "test", "text": "q"})
    async with engine.begin() as conn:
        assert await outbox.record_failure(conn, server, server_target, api_error("server"), now) == "failed"
    alert, alert_target = await claimed(engine, "alert", {"text": "a"})
    async with engine.begin() as conn:
        assert await outbox.record_failure(conn, alert, alert_target, lost, now) == "retry"
        row = await job_row(conn, alert.id)
        assert (row.attempts, row.not_before, row.done_at) == (1, now + timedelta(seconds=10), None)


async def test_attempts_run_out(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    kicked = api_error("kicked")
    async with engine.begin() as conn:
        job_id = await enqueue(conn, "send_post", {"role": "test", "text": "p"})
    for attempt in range(1, outbox.MAX_ATTEMPTS + 1):
        async with engine.begin() as conn:
            await due_now(conn, job_id)
            await conn.execute(text("DELETE FROM telegram_pacing"))
            picked = await outbox.pick(conn, now)
            assert picked is not None
            outcome = await outbox.record_failure(conn, picked[0], picked[1], kicked, now)
        assert outcome == ("failed" if attempt == outbox.MAX_ATTEMPTS else "retry")
    async with engine.connect() as conn:
        row = await job_row(conn, job_id)
        assert row.done_at is not None and "after 8 attempts" in row.last_error
        assert await store.outbox_counts(conn) == (0, 1)


async def test_bad_payloads_and_missing_targets(engine: AsyncEngine, vault: Vault) -> None:
    await setup(engine, vault)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        no_text = await enqueue(conn, "alert", {})
        bad_role = await enqueue(conn, "send_post", {"role": "other", "text": "x"})
        no_post = await enqueue(conn, "edit_post", {"post_id": 999999, "text": "x"})
        assert await outbox.pick(conn, now) is None
        for job_id, reason in (
            (no_text, "invalid_payload"),
            (bad_role, "invalid_payload"),
            (no_post, "post_not_found"),
        ):
            assert (await job_row(conn, job_id)).last_error == f"failed: {reason}"
        await conn.execute(text("DELETE FROM settings WHERE key = 'telegram.owner'"))
        unpaired = await enqueue(conn, "alert", {"text": "x"})
        assert await outbox.pick(conn, now) is None
        row = await job_row(conn, unpaired)
        assert (row.last_error, row.attempts, row.done_at) == ("owner_not_paired", 1, None)


# --- one whole step, for real --------------------------------------------------------------------


async def test_step_without_bot_does_nothing(engine: AsyncEngine, vault: Vault) -> None:
    async with engine.begin() as conn:
        await enqueue(conn, "alert", {"text": "x"})
    loop = TelegramOutboxReconciler(vault_factory=lambda: vault, bot_factory=closed_port_bot())
    assert await loop.try_run_once(engine)
    assert loop.last_outcome == "no_bot"


async def test_step_with_refused_connection_retries_and_records_no_token(
    engine: AsyncEngine, vault: Vault
) -> None:
    token = await setup(engine, vault)
    async with engine.begin() as conn:
        job_id = await enqueue(conn, "alert", {"text": "x"})
    loop = TelegramOutboxReconciler(vault_factory=lambda: vault, bot_factory=closed_port_bot())
    assert await loop.try_run_once(engine)
    assert loop.last_outcome == "retry"
    async with engine.connect() as conn:
        row = await job_row(conn, job_id)
    assert row.attempts == 1 and "network error (ConnectError)" in row.last_error
    assert_no_token(token, row.last_error)


async def test_step_with_real_telegram_and_a_revoked_token(engine: AsyncEngine, vault: Vault) -> None:
    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    token = await setup(engine, vault)
    async with engine.begin() as conn:
        job_id = await enqueue(conn, "send_post", {"role": "test", "text": "x"})
    assert await outbox.process_one(engine, vault, real_bot) == "retry"
    assert await outbox.process_one(engine, vault, real_bot) == "paused"
    async with engine.connect() as conn:
        row = await job_row(conn, job_id)
        health = await store.get_setting(conn, store.KEY_HEALTH)
    assert row.last_error == "bot_unauthorized" and row.attempts == 0 and row.done_at is None
    assert health is not None and health["error"] == "bot_unauthorized"
    assert_no_token(token, row.last_error)
