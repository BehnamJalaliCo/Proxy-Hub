"""The operator poster on the real PostgreSQL (decision D38): who posts, fallback, sending, edits.

Links come from the posts team's test providers over the real ``nodes`` table; the publisher, the
outbox, the advisory locks and the vault are the real ones. Telegram is never reached: see
tests/test_operator_poster_support.py for the two ways a test meets "Telegram" (a real refusal on
a closed local port, or Telegram's answer as a real TL object behind the real client and allowlist).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from telethon import errors, functions, types
from test_mtproxy_support import seed_connected
from test_mtproxy_units import closed_port
from test_operator_poster_support import (
    EMOJI_SET,
    MAIN_ID,
    MAIN_NAME,
    TEST_ID,
    TEST_NAME,
    channel_answers,
    clean_operator,
    fallbacks,
    setup_operator,
    store_pack,
    telegram_answers,
)
from test_posts_support import (
    MAIN_CHAT,
    PROVIDERS,
    TEST_CHAT,
    channel,
    clean_posts_tables,
    deliver,
    fleet_kill_switch,
    jobs,
    make_node,
    node_secret,
    node_vless_id,
    rows,
    set_node,
    vault_at,
)
from test_telegram_support import all_logs

from proxyhub.db.models import Outbox
from proxyhub.mtproxy import store as mtp_store
from proxyhub.mtproxy.userclient import ChannelRef
from proxyhub.posts import entities, links, operator, operator_outbox, plan, publisher, views
from proxyhub.posts import settings as posting
from proxyhub.posts.messages import FALLBACK_FA
from proxyhub.posts.poster import BOT, OPERATOR, POSTERS, configured
from proxyhub.reconcilers.base import lock_key
from proxyhub.reconcilers.publisher import PublisherReconciler
from proxyhub.telegram import store
from proxyhub.telegram.schedule import TEHRAN
from proxyhub.vault import Vault

pytestmark = pytest.mark.db

PACK = [(5_001, "📡"), (5_002, "⌛"), (5_003, "🔗"), (5_004, "🔐"), (5_005, "🔒")]


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


def slot_time(i: int = 0) -> datetime:
    today = datetime.now(UTC).astimezone(TEHRAN).date()
    return plan.day_plan(today, count=10, start=time(8), end=time(0), quiet=[])[i].at + timedelta(seconds=30)


async def publish(engine: AsyncEngine, now: datetime, poster: Any = OPERATOR) -> str:
    async with engine.begin() as conn:

        async def harvest() -> links.Harvest:
            return await links.harvest(conn, PROVIDERS)

        return await publisher.publish_due(conn, now, harvest, poster)


async def refresh(engine: AsyncEngine, now: datetime) -> int:
    async with engine.begin() as conn:

        async def harvest() -> links.Harvest:
            return await links.harvest(conn, PROVIDERS)

        return await publisher.refresh_posts(conn, now, harvest)


async def no_gap(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await store.save_posting_settings(conn, store.PostingSettings(quiet_windows=[], post_interval_min=0))


async def fleet(engine: AsyncEngine, n: int = 2) -> list[Any]:
    return [await make_node(engine, slot=i, ipv4=f"203.0.113.{10 + i}") for i in range(1, n + 1)]


class Ticking:
    """A clock that moves 10 s at every reading: each step is past the 3 s channel pacing of the one
    before (a test that checks the pacing itself passes the real clock)."""

    def __init__(self) -> None:
        self.at = datetime.now(UTC)

    def __call__(self) -> datetime:
        self.at += timedelta(seconds=10)
        return self.at


def deps(engine: AsyncEngine, vault: Vault, **kw: Any) -> operator_outbox.Deps:
    kw.setdefault("clock", Ticking())
    return operator_outbox.Deps(engine=engine, vault=vault, lock_wait_s=0.5, **kw)


async def post_row(engine: AsyncEngine, job_id: int) -> Any:
    return (await rows(engine, "SELECT id, poster, outbox_id FROM posts WHERE outbox_id = :j", j=job_id))[0]


# --- who takes a new post ---------------------------------------------------------------------------------


async def test_the_bot_posts_until_the_owner_switches(engine: AsyncEngine, vault: Vault) -> None:
    await fleet(engine)
    await setup_operator(engine, vault, poster="bot")
    async with engine.connect() as conn:
        assert await configured(conn) is BOT
    loop = PublisherReconciler(now=lambda: slot_time(1), providers=PROVIDERS)
    assert await loop.try_run_once(engine) and loop.last["post"] == "queued"
    [job] = await jobs(engine)
    assert job.kind == "send_post" and (await post_row(engine, job.id)).poster == "bot"
    assert await fallbacks(engine) == []  # the owner chose the bot: nothing to explain


async def test_a_ready_operator_takes_the_post(engine: AsyncEngine, vault: Vault) -> None:
    await fleet(engine)
    await setup_operator(engine, vault)
    async with engine.connect() as conn:
        assert await configured(conn) is OPERATOR
        assert await operator.unavailable(conn, role="test", now=slot_time(1)) is None
    loop = PublisherReconciler(now=lambda: slot_time(1), providers=PROVIDERS)  # reads the owner's choice
    assert await loop.try_run_once(engine) and loop.last["post"] == "queued"
    [job] = await jobs(engine)
    assert job.kind == "operator_send_post" and set(job.payload) == {"role", "text", "post_kind"}
    assert job.payload["role"] == "test" and "tg-emoji" not in job.payload["text"]  # plain HTML queued
    assert (await post_row(engine, job.id)).poster == "operator"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"connected": False}, "not_connected"),
        ({"kill_switch": True}, "kill_switch"),
        ({"public_test": False}, "no_username"),
        ("hold", "hold"),
        ("no_post_right", "no_post_right"),
    ],
)
async def test_an_operator_that_cannot_post_hands_the_new_post_to_the_bot(
    engine: AsyncEngine, vault: Vault, change: Any, reason: str
) -> None:
    await fleet(engine)
    now = slot_time(1)
    await setup_operator(engine, vault, **(change if isinstance(change, dict) else {}))
    async with engine.begin() as conn:
        if change == "hold":
            await operator.set_hold(conn, now + timedelta(minutes=10), now)
        if change == "no_post_right":
            await operator.save_rights(
                conn,
                {
                    "checked_at": now.isoformat(),
                    "channels": {
                        "test": {
                            "chat_id": TEST_CHAT,
                            "status": "admin",
                            "can_post": False,
                            "can_edit": True,
                            "can_delete": False,
                        }
                    },
                },
            )
    assert await publish(engine, now) == "queued"
    [job] = await jobs(engine)
    assert job.kind == "send_post"
    post = await post_row(engine, job.id)
    assert post.poster == "bot"
    [logged] = await fallbacks(engine)
    assert (logged["role"], logged["reason"], logged["stage"]) == ("test", reason, "queue")
    assert logged["text_fa"] == FALLBACK_FA[reason] and "ربات" in logged["text_fa"]
    [action] = await rows(
        engine, "SELECT actor, target FROM actions_log WHERE action = 'posts.poster_fallback'"
    )
    assert (action.actor, action.target) == ("publisher", f"test:post:{post.id}")


async def test_a_hold_that_ended_and_rights_of_another_channel_do_not_count(
    engine: AsyncEngine, vault: Vault
) -> None:
    await setup_operator(engine, vault)
    now = slot_time(1)
    async with engine.begin() as conn:
        await operator.set_hold(conn, now - timedelta(seconds=1), now - timedelta(minutes=5))
        await operator.save_rights(
            conn, {"channels": {"test": {"chat_id": TEST_CHAT - 1, "status": "admin", "can_post": False}}}
        )
        assert await operator.unavailable(conn, role="test", now=now) is None


# --- edits go through the poster that sent the post --------------------------------------------------------


async def test_status_edits_go_through_the_poster_that_sent_the_post(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    nodes = await fleet(engine, 1)
    await setup_operator(engine, vault)
    await no_gap(engine)
    # One post by the bot (the owner had not switched yet) and one by the operator account.
    assert await publish(engine, slot_time(1), BOT) == "queued"
    await deliver(engine, (await jobs(engine, "send_post"))[0].id, message_id=500)
    assert await publish(engine, slot_time(2)) == "queued"
    with telegram_answers(monkeypatch, channel_answers(message_id=901)):
        assert await operator_outbox.process_one(deps(engine, vault)) == "sent"
    await set_node(engine, nodes[0], state="burned")
    assert await refresh(engine, slot_time(2) + timedelta(minutes=1)) == 2
    kinds = sorted(j.kind for j in await jobs(engine) if j.kind.endswith("edit_post"))
    assert kinds == ["edit_post", "operator_edit_post"]
    assert set(POSTERS) == {"bot", "operator"}


# --- the operator's outbox step ---------------------------------------------------------------------------


async def queue_operator_post(engine: AsyncEngine, vault: Vault, now: datetime | None = None) -> int:
    await fleet(engine, 1)
    assert await publish(engine, now or slot_time(1)) == "queued"
    [job] = await jobs(engine, "operator_send_post")
    return int(job.id)


async def test_a_post_goes_out_with_the_owners_animated_emoji_and_its_message_id_is_recorded(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault)
    await store_pack(engine, PACK)
    job_id = await queue_operator_post(engine, vault)
    refs: dict[Any, Any] = {}
    with telegram_answers(monkeypatch, channel_answers(message_id=901)) as requests:
        assert await operator_outbox.process_one(deps(engine, vault, refs=refs)) == "sent"
    resolve, send = requests
    assert isinstance(resolve, functions.contacts.ResolveUsernameRequest) and resolve.username == TEST_NAME
    assert isinstance(send, functions.messages.SendMessageRequest)
    assert send.peer == types.InputPeerChannel(channel_id=TEST_ID, access_hash=7_001) and send.no_webpage
    assert (send.reply_markup, send.schedule_date, send.send_as, send.reply_to) == (None, None, None, None)
    [job] = await jobs(engine)
    # The text is the post's visible text; the animated emoji are entities over exactly their units.
    assert send.message == entities.parse_html(job.payload["text"]).text
    animated = [e for e in send.entities if isinstance(e, types.MessageEntityCustomEmoji)]
    shown = {entities.utf16_slice(send.message, e.offset, e.length): e.document_id for e in animated}
    # The seeded title brings its own emoji (not in the pack); the lock above the config and the
    # signature are animated (D48: no 📡 or 🔐 header any more).
    assert shown == {"🔒": 5_005, "🔗": 5_003}
    assert any(isinstance(e, types.MessageEntityTextUrl) for e in send.entities)  # the proxy links
    # Recorded exactly like the bot's post: channel_posts with Telegram's message id, job done.
    [cp] = await rows(engine, "SELECT role, chat_id, message_id, outbox_id, text_sha256 FROM channel_posts")
    assert (cp.role, cp.chat_id, cp.message_id, cp.outbox_id) == ("test", TEST_CHAT, 901, job_id)
    assert cp.text_sha256 == store.text_sha256(job.payload["text"])
    assert job.done_at is not None and job.last_error is None
    async with engine.connect() as conn:
        assert (await store.pacing(conn, TEST_CHAT)).last_sent_at is not None  # the shared channel pacing
        recent = await views.recent(conn, datetime.now(UTC))
    assert recent[0]["poster"] == "operator" and recent[0]["message_url"] == f"https://t.me/{TEST_NAME}/901"
    assert next(iter(refs.values())).channel_id == TEST_ID  # the channel reference is remembered


async def test_an_edit_reuses_the_channel_reference_and_counts_not_modified_as_done(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault)
    await no_gap(engine)
    job_id = await queue_operator_post(engine, vault)
    shared = deps(engine, vault)
    with telegram_answers(monkeypatch, channel_answers(message_id=901)):
        assert await operator_outbox.process_one(shared) == "sent"
    [cp] = await rows(engine, "SELECT id FROM channel_posts WHERE outbox_id = :j", j=job_id)
    async with engine.begin() as conn:
        first = await OPERATOR.queue_edit(conn, channel_post_id=cp.id, text_html="<b>📡 ویرایش</b>")
    with telegram_answers(monkeypatch, channel_answers()) as requests:
        assert await operator_outbox.process_one(shared) == "sent"
    [edit] = requests  # no second username lookup
    assert isinstance(edit, functions.messages.EditMessageRequest)
    assert (edit.id, edit.message, edit.media, edit.reply_markup) == (901, "📡 ویرایش", None, None)
    [cp2] = await rows(engine, "SELECT text_sha256, last_edited_at FROM channel_posts WHERE id = :i", i=cp.id)
    assert cp2.text_sha256 == store.text_sha256("<b>📡 ویرایش</b>") and cp2.last_edited_at is not None
    async with engine.begin() as conn:
        second = await OPERATOR.queue_edit(conn, channel_post_id=cp.id, text_html="<b>📡 ویرایش</b>")
    not_modified = errors.MessageNotModifiedError(None)
    with telegram_answers(monkeypatch, channel_answers(edit_error=not_modified)):
        assert await operator_outbox.process_one(shared) == "sent"
    done = {j.id: j for j in await jobs(engine)}
    assert done[first].last_error is None and done[second].last_error is None and done[second].done_at


@pytest.mark.parametrize(
    ("error", "outcome", "last_error"),
    [
        (errors.MessageIdInvalidError(None), "failed", "failed: bad_request"),
        (errors.ChatAdminRequiredError(None), "failed", "failed: bad_request"),
        (errors.ServerError(None, "RPC_CALL_FAIL", 500), "retry", "operator_telegram_unavailable"),
        (TimeoutError(), "retry", "operator_telegram_unavailable"),
    ],
)
async def test_a_failed_edit_is_retried_or_failed_never_handed_over(
    engine: AsyncEngine,
    vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    outcome: str,
    last_error: str,
) -> None:
    await setup_operator(engine, vault)
    await no_gap(engine)
    job_id = await queue_operator_post(engine, vault)
    shared = deps(engine, vault)
    with telegram_answers(monkeypatch, channel_answers()):
        assert await operator_outbox.process_one(shared) == "sent"
    [cp] = await rows(engine, "SELECT id FROM channel_posts WHERE outbox_id = :j", j=job_id)
    async with engine.begin() as conn:
        edit = await OPERATOR.queue_edit(conn, channel_post_id=cp.id, text_html="<b>ویرایش</b>")
    with telegram_answers(monkeypatch, channel_answers(edit_error=error)):
        assert await operator_outbox.process_one(shared) == outcome
    row = {j.id: j for j in await jobs(engine)}[edit]
    assert row.kind == "operator_edit_post" and row.last_error.startswith(last_error)


@pytest.mark.parametrize(
    ("error", "reason", "rpc"),
    [
        (errors.ChatAdminRequiredError(None), "telegram_refused", "CHAT_ADMIN_REQUIRED"),
        (errors.ChatWriteForbiddenError(None), "telegram_refused", "CHAT_WRITE_FORBIDDEN"),
        (errors.AuthKeyUnregisteredError(None), "session_terminated", "AUTH_KEY_UNREGISTERED"),
    ],
)
async def test_a_clean_refusal_hands_the_post_to_the_bot(
    engine: AsyncEngine,
    vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    reason: str,
    rpc: str,
) -> None:
    await setup_operator(engine, vault)
    job_id = await queue_operator_post(engine, vault)
    with telegram_answers(monkeypatch, channel_answers(send_error=error)):
        assert await operator_outbox.process_one(deps(engine, vault)) == "handed_to_bot"
    [job] = await jobs(engine)
    assert (job.id, job.kind, job.done_at, job.last_error) == (job_id, "send_post", None, None)
    assert (await post_row(engine, job_id)).poster == "bot"
    [logged] = await fallbacks(engine)
    assert (logged["reason"], logged["stage"]) == (reason, "send")
    assert rpc in logged["text_fa"] or reason != "telegram_refused"
    assert await rows(engine, "SELECT id FROM channel_posts") == []


async def test_a_flood_wait_holds_the_account_and_hands_the_post_to_the_bot(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault)
    job_id = await queue_operator_post(engine, vault)
    flood = errors.FloodWaitError(None, capture=120)
    with telegram_answers(monkeypatch, channel_answers(send_error=flood)):
        assert await operator_outbox.process_one(deps(engine, vault)) == "handed_to_bot"
    assert [j.kind for j in await jobs(engine)] == ["send_post"]
    async with engine.connect() as conn:
        until = await operator.hold_until(conn)
        assert until is not None and timedelta(seconds=100) < until - datetime.now(UTC) < timedelta(minutes=4)
        assert await operator.unavailable(conn, role="test", now=datetime.now(UTC)) == "hold"
    assert job_id == (await jobs(engine))[0].id


@pytest.mark.parametrize("answer", ["timeout", "no_message_id"])
async def test_a_post_whose_outcome_is_unknown_is_never_sent_again(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """At most once (rule 6 makes a duplicate permanent): the job fails, the owner is told."""
    await setup_operator(engine, vault)
    job_id = await queue_operator_post(engine, vault)
    base = channel_answers()

    def unknown(request: Any) -> Any:
        if isinstance(request, functions.messages.SendMessageRequest):
            if answer == "timeout":
                return TimeoutError()  # the request was written, the answer never came
            return types.Updates(updates=[], users=[], chats=[], date=datetime.now(UTC), seq=0)
        return base(request)

    with telegram_answers(monkeypatch, unknown):
        assert await operator_outbox.process_one(deps(engine, vault)) == "failed"
    [job, alert] = await jobs(engine)
    assert (job.id, job.kind) == (job_id, "operator_send_post")
    assert job.done_at is not None and job.last_error.startswith("failed: ambiguous_send")
    assert alert.kind == "alert" and "دوباره فرستاده نمیشود" in alert.payload["text"]
    assert (await post_row(engine, job_id)).poster == "operator"  # not handed to the bot
    assert await fallbacks(engine) == []
    with telegram_answers(monkeypatch, base) as requests:
        assert await operator_outbox.process_one(deps(engine, vault)) == "idle"
    assert requests == []


async def test_a_real_connection_failure_hands_the_post_over_and_leaks_nothing(
    engine: AsyncEngine, vault: Vault
) -> None:
    """No transport replaced here: the session points at a local port nobody listens on."""
    await setup_operator(engine, vault, connected=False)
    _, session, _ = await seed_connected(engine, vault, port=closed_port())
    job_id = await queue_operator_post(engine, vault)
    async with engine.begin() as conn:
        await conn.execute(update(Outbox).where(Outbox.id == job_id).values(not_before=datetime.now(UTC)))
    with all_logs(logging.DEBUG) as logs:
        outcome = await operator_outbox.process_one(deps(engine, vault))
    assert outcome == "handed_to_bot"
    [logged] = await fallbacks(engine)
    assert (logged["reason"], logged["stage"]) == ("telegram_unavailable", "send")
    dumps = logs.getvalue() + str(await rows(engine, "SELECT * FROM actions_log")) + str(await jobs(engine))
    assert session.reveal() not in dumps


@pytest.mark.parametrize(
    ("change", "reason"),
    [("kill_switch", "kill_switch"), ("disconnect", "not_connected"), ("private", "no_username")],
)
async def test_what_changed_after_queueing_is_checked_again_before_sending(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch, change: str, reason: str
) -> None:
    await setup_operator(engine, vault)
    job_id = await queue_operator_post(engine, vault)
    if change == "kill_switch":
        await fleet_kill_switch(engine, on=True)
    elif change == "disconnect":
        async with engine.begin() as conn:
            await mtp_store.drop_secrets(conn, session_only=True)
    else:
        async with engine.begin() as conn:
            await store.save_channel(conn, "test", channel(TEST_CHAT))
    with telegram_answers(monkeypatch, channel_answers()) as requests:
        assert await operator_outbox.process_one(deps(engine, vault)) == "handed_to_bot"
    assert requests == []  # nothing reached even the transport
    assert [(j.id, j.kind) for j in await jobs(engine)] == [(job_id, "send_post")]
    assert (await fallbacks(engine))[0]["reason"] == reason


async def test_an_edit_waits_while_the_operator_cannot_work(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault)
    await no_gap(engine)
    job_id = await queue_operator_post(engine, vault)
    shared = deps(engine, vault)
    with telegram_answers(monkeypatch, channel_answers()):
        assert await operator_outbox.process_one(shared) == "sent"
    [cp] = await rows(engine, "SELECT id FROM channel_posts WHERE outbox_id = :j", j=job_id)
    async with engine.begin() as conn:
        edit = await OPERATOR.queue_edit(conn, channel_post_id=cp.id, text_html="<b>x</b>")
    await fleet_kill_switch(engine, on=True)
    with telegram_answers(monkeypatch, channel_answers()) as requests:
        assert await operator_outbox.process_one(shared) == "retry"
    row = {j.id: j for j in await jobs(engine)}[edit]
    assert (
        row.kind == "operator_edit_post" and row.done_at is None and row.last_error == "operator_kill_switch"
    )
    assert requests == []
    waiting = (await rows(engine, "SELECT attempts FROM outbox WHERE id = :i", i=edit))[0]
    assert waiting.attempts == 0  # the owner's kill switch is not a failure of the edit
    now = datetime.now(UTC)
    await fleet_kill_switch(engine, on=False)
    async with engine.begin() as conn:
        await operator.set_hold(conn, now + timedelta(minutes=30), now)
        await conn.execute(update(Outbox).where(Outbox.id == edit).values(not_before=now))
    assert await operator_outbox.process_one(shared) == "retry"
    held = (await rows(engine, "SELECT attempts, not_before FROM outbox WHERE id = :i", i=edit))[0]
    assert held.attempts == 0  # nor is Telegram's hold
    assert held.not_before > now + timedelta(minutes=29)


async def test_main_gets_no_post_once_the_owner_turned_it_off(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault, main=True)
    async with engine.begin() as conn:
        await store.save_channel(conn, "main", channel(MAIN_CHAT, f"@{MAIN_NAME}"))
        job = await OPERATOR.queue_post(conn, role="main", text_html="<b>📡 x</b>")  # queued while it was on
    await no_gap(engine)
    shared = deps(engine, vault)
    with telegram_answers(monkeypatch, channel_answers()) as requests:
        assert await operator_outbox.process_one(shared) == "failed"
    assert requests == []
    [row] = await jobs(engine)
    assert (row.id, row.kind, row.last_error) == (
        job,
        "operator_send_post",
        "failed: main_disabled: the owner turned main posting off",
    )
    # Not handed to the bot either, even when the operator also could not have sent it.
    await fleet_kill_switch(engine, on=True)
    async with engine.begin() as conn:
        again = await OPERATOR.queue_post(conn, role="main", text_html="<b>x</b>")
    assert await operator_outbox.process_one(shared) == "failed"
    assert {j.id: j.kind for j in await jobs(engine)}[again] == "operator_send_post"
    # With the switch on, the main channel's post goes out like the test channel's.
    await fleet_kill_switch(engine, on=False)
    async with engine.begin() as conn:
        await posting.save(conn, posting.parse({**posting.DEFAULTS.as_json(), "main_enabled": True}))
        third = await OPERATOR.queue_post(conn, role="main", text_html="<b>📡 x</b>")
    with telegram_answers(monkeypatch, channel_answers(message_id=77)) as requests:
        assert await operator_outbox.process_one(shared) == "sent"
    assert requests[-1].peer.channel_id == MAIN_ID
    assert (await rows(engine, "SELECT outbox_id FROM channel_posts WHERE message_id = 77"))[
        0
    ].outbox_id == third


async def test_the_shared_pacing_the_lock_and_an_interrupted_send(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault)
    job_id = await queue_operator_post(engine, vault)
    # The bot wrote to the same channel a second ago: the operator waits its 3 s too.
    async with engine.begin() as conn:
        await store.record_sent(conn, TEST_CHAT, datetime.now(UTC))
    real = deps(engine, vault, clock=store.now_utc)
    with telegram_answers(monkeypatch, channel_answers()) as requests:
        assert await operator_outbox.process_one(real) == "idle"
        assert requests == []
        async with engine.begin() as conn:
            await store.record_sent(conn, TEST_CHAT, datetime.now(UTC) - timedelta(seconds=10))
        # Another operation on the account holds its lock: nothing is claimed.
        async with engine.connect() as holder:
            await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key(mtp_store.LOCK_NAME)})
            await holder.commit()
            assert await operator_outbox.process_one(real) == "busy"
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(mtp_store.LOCK_NAME)})
            await holder.commit()
        assert requests == []
        [job] = await jobs(engine)
        assert job.last_error is None and job.done_at is None
        # A claim left behind by a worker that died mid-send: never sent again.
        async with engine.begin() as conn:
            await conn.execute(update(Outbox).where(Outbox.id == job_id).values(last_error=store.SENDING))
        assert await operator_outbox.process_one(real) == "idle"
    [job] = await jobs(engine)
    assert job.last_error.startswith("failed: ambiguous_send") and requests == []


async def test_a_pack_for_another_link_is_never_used(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    await setup_operator(engine, vault)
    await store_pack(engine, PACK, short_name="an_older_pack")
    await queue_operator_post(engine, vault)
    with telegram_answers(monkeypatch, channel_answers()) as requests:
        assert await operator_outbox.process_one(deps(engine, vault)) == "sent"
    assert not [e for e in requests[-1].entities or [] if isinstance(e, types.MessageEntityCustomEmoji)]


async def test_a_request_the_allowlist_refuses_never_leaves_and_the_post_goes_to_the_bot(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If our own code ever addressed another channel (here: a wrong remembered reference), the
    connection's scope (this one channel) stops the post before it leaves."""
    await setup_operator(engine, vault)
    await queue_operator_post(engine, vault)
    async with engine.connect() as conn:
        session_id = (await mtp_store.load_row(conn)).session_id
    wrong = {(session_id, TEST_ID): ChannelRef(channel_id=TEST_ID + 5, access_hash=1)}
    with telegram_answers(monkeypatch, channel_answers()) as requests:
        assert await operator_outbox.process_one(deps(engine, vault, refs=wrong)) == "handed_to_bot"
    assert requests == []
    assert (await fallbacks(engine))[0]["reason"] == "request_refused"
    assert [j.kind for j in await jobs(engine)] == ["send_post"]


async def test_preview_lists_which_emoji_become_animated(engine: AsyncEngine, vault: Vault) -> None:
    await fleet(engine, 1)
    await setup_operator(engine, vault)
    await store_pack(engine, PACK)
    async with engine.connect() as conn:
        h = await links.harvest(conn, PROVIDERS)
        preview = await views.preview(conn, datetime.now(UTC), h)
    shown = {x["emoji"]: x["animated"] for x in preview["emoji"]}
    assert shown["🔒"] is True and shown["🔗"] is True
    assert "📡" not in shown and "🔐" not in shown  # the headers went with D48
    assert all(isinstance(v, bool) for v in shown.values())


async def test_no_secret_reaches_logs_or_tables(
    engine: AsyncEngine, vault: Vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    nodes = await fleet(engine, 2)
    await setup_operator(engine, vault)
    with caplog.at_level(logging.DEBUG):
        assert await publish(engine, slot_time(1)) == "queued"
        with telegram_answers(monkeypatch, channel_answers(send_error=errors.ChatWriteForbiddenError(None))):
            assert await operator_outbox.process_one(deps(engine, vault)) == "handed_to_bot"
    dumps = [
        caplog.text,
        str(await rows(engine, "SELECT * FROM actions_log")),
        str(await rows(engine, "SELECT * FROM settings")),
        str(await rows(engine, "SELECT * FROM posts")),
    ]
    for n in nodes:
        for value in (node_secret(n), node_vless_id(n)):
            assert all(value not in d for d in dumps)


async def test_the_loops_are_registered_and_need_the_vault(engine: AsyncEngine, vault: Vault) -> None:
    from proxyhub.reconcilers.operator_poster import OperatorChecksReconciler, OperatorOutboxReconciler
    from proxyhub.vault import VaultKeyError
    from proxyhub.worker import reconcilers

    loops = {r.name: r for r in reconcilers()}
    assert isinstance(loops["operator_outbox"], OperatorOutboxReconciler)
    assert isinstance(loops["operator_checks"], OperatorChecksReconciler)

    def no_vault() -> Vault:
        raise VaultKeyError("no master key")

    for broken in (
        OperatorOutboxReconciler(vault_factory=no_vault),
        OperatorChecksReconciler(vault_factory=no_vault),
    ):
        assert await broken.try_run_once(engine) and broken.last_outcome == "vault_unavailable"
    ok = OperatorOutboxReconciler(vault_factory=lambda: vault)
    assert await ok.try_run_once(engine) and ok.last_outcome == "idle"
    checks = OperatorChecksReconciler(vault_factory=lambda: vault)
    assert await checks.try_run_once(engine) and checks.last_outcome == "idle"  # the bot, no pack
    with pytest.raises(NotImplementedError):
        async with engine.begin() as conn:
            await ok.run_once(conn)
    assert EMOJI_SET
