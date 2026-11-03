"""Outbox processor for Telegram jobs ``send_post``, ``edit_post`` and ``alert``.

Payloads::

    send_post  {"role": "main" | "test", "text": "<html>", "post_kind": "<short name>"}
    edit_post  {"post_id": <channel_posts.id>, "text": "<html>"}
    alert      {"text": "<html>"}                                  -> the owner's private chat

One job per step (:func:`process_one`), in three short transactions:

1. **pick + claim**: the first due job whose chat may be written to now (pacing: 3 s per channel,
   1 s for the owner chat, never before a 429 ``retry_after``). A ``send_post`` to the MAIN channel
   inside a no-post window (D21) or sooner than ``post_interval_min`` after the last main post is
   moved to the first allowed time instead (alerts and edits are never held back). The claim
   (``last_error = 'sending'``) is committed BEFORE the HTTP call.
2. **send** to Telegram (no transaction open).
3. **record**: on success the post is recorded in ``channel_posts`` with its ``message_id`` and the
   job is marked done IN ONE TRANSACTION (``channel_posts.outbox_id`` is unique, so one job can
   never be recorded as two posts).

Delivery guarantees (rule 6 makes a duplicate channel post permanent, so posts are at most once):

* ``send_post`` is **at most once**. If the outcome is unknown, the job is marked failed
  (``ambiguous_send``) and never sent again: a broken connection or timeout after the request was
  written, a 5xx or unreadable answer from Telegram, or a claim still present at the next step (the
  worker died, or the database failed, between the claim and step 3). The owner sees it in the
  failed count; a missed post costs less than a post that can never be removed.
* ``edit_post`` and ``alert`` are **at least once**: they are retried in those cases. Editing to
  the same text again is harmless ("message is not modified" counts as done); an alert may then
  arrive twice.

Retries: 429 waits exactly ``retry_after`` (not counted as an attempt); a revoked token pauses the
whole outbox for 5 minutes (not counted); other errors back off exponentially (10 s doubling, at
most 30 min) and after 8 attempts the job fails. Telegram's 400 (bad HTML, message not found, ...)
fails at once. Failed = ``done_at`` set with ``last_error`` not NULL (see proxyhub/telegram/store.py).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.db.models import Outbox
from proxyhub.db.models_telegram import ChannelPost
from proxyhub.telegram import store
from proxyhub.telegram.client import (
    BadRequest,
    BotClient,
    InvalidResponse,
    NetworkError,
    RetryAfter,
    ServerError,
    TelegramError,
    Unauthorized,
)
from proxyhub.telegram.schedule import CHANNEL_GAP, OWNER_GAP, backoff, next_post_time, next_send_time
from proxyhub.vault import SecretValue, Vault

log = logging.getLogger(__name__)

KINDS: Final = ("send_post", "edit_post", "alert")
MAX_ATTEMPTS: Final = 8
UNAUTHORIZED_PAUSE: Final = timedelta(minutes=5)
PICK_LIMIT: Final = 50
MAX_ERROR_CHARS: Final = 300

BotFactory = Callable[[SecretValue], BotClient]


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class Target:
    chat_id: int
    role: str | None = None  # send_post
    post_id: int | None = None  # edit_post
    message_id: int | None = None  # edit_post


# --- job state transitions (each runs inside the caller's transaction) ---------------------------


async def claim(conn: AsyncConnection, job_id: int) -> None:
    await conn.execute(update(Outbox).where(Outbox.id == job_id).values(last_error=store.SENDING))


async def finish_ok(conn: AsyncConnection, job_id: int, now: datetime) -> None:
    await conn.execute(update(Outbox).where(Outbox.id == job_id).values(done_at=now, last_error=None))


async def finish_failed(conn: AsyncConnection, job_id: int, now: datetime, reason: str) -> None:
    await conn.execute(
        update(Outbox)
        .where(Outbox.id == job_id)
        .values(done_at=now, last_error=f"failed: {reason}"[:MAX_ERROR_CHARS])
    )


async def retry_at(
    conn: AsyncConnection, job: Job, at: datetime, error: str, *, counted: bool, now: datetime
) -> str:
    """Try again at ``at``; a counted failure that reaches MAX_ATTEMPTS fails the job instead."""
    attempts = job.attempts + (1 if counted else 0)
    if counted and attempts >= MAX_ATTEMPTS:
        await conn.execute(update(Outbox).where(Outbox.id == job.id).values(attempts=attempts))
        await finish_failed(conn, job.id, now, f"{error} (after {attempts} attempts)")
        return "failed"
    await conn.execute(
        update(Outbox)
        .where(Outbox.id == job.id)
        .values(not_before=at, attempts=attempts, last_error=error[:MAX_ERROR_CHARS])
    )
    return "retry"


async def defer(conn: AsyncConnection, job_id: int, at: datetime) -> None:
    await conn.execute(update(Outbox).where(Outbox.id == job_id).values(not_before=at))


# --- picking -------------------------------------------------------------------------------------


async def recover_interrupted(conn: AsyncConnection, now: datetime) -> int:
    """Jobs still claimed from an earlier step whose outcome was never recorded."""
    rows = (
        await conn.execute(
            select(Outbox.id, Outbox.kind).where(
                Outbox.done_at.is_(None), Outbox.last_error == store.SENDING, Outbox.kind.in_(KINDS)
            )
        )
    ).all()
    for row in rows:
        if row.kind == "send_post":
            await finish_failed(conn, row.id, now, "ambiguous_send: interrupted while sending; not resent")
        else:
            await conn.execute(update(Outbox).where(Outbox.id == row.id).values(last_error="interrupted"))
    return len(rows)


async def due_jobs(conn: AsyncConnection, now: datetime, limit: int = PICK_LIMIT) -> list[Job]:
    rows = (
        await conn.execute(
            select(Outbox.id, Outbox.kind, Outbox.payload, Outbox.attempts, Outbox.last_error)
            .where(
                Outbox.done_at.is_(None),
                Outbox.kind.in_(KINDS),
                Outbox.not_before <= now,
                Outbox.last_error.is_distinct_from(store.SENDING),  # claimed: see recover_interrupted
            )
            .order_by(Outbox.not_before, Outbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    return [
        Job(
            id=int(r.id),
            kind=str(r.kind),
            payload=dict(r.payload) if isinstance(r.payload, dict) else {},
            attempts=int(r.attempts),
            last_error=r.last_error,
        )
        for r in rows
    ]


def _text(job: Job) -> str | None:
    value = job.payload.get("text")
    return value if isinstance(value, str) and value.strip() else None


async def _resolve(conn: AsyncConnection, job: Job, now: datetime) -> Target | str:
    """The chat for this job, or an outcome string after updating the job."""
    if _text(job) is None:
        await finish_failed(conn, job.id, now, "invalid_payload")
        return "failed"
    if job.kind == "alert":
        chat_id = await store.owner_chat_id(conn)
        if chat_id is None:
            return await retry_at(
                conn, job, now + backoff(job.attempts + 1), "owner_not_paired", counted=True, now=now
            )
        return Target(chat_id=chat_id)
    if job.kind == "send_post":
        role = job.payload.get("role")
        if role not in store.ROLES:
            await finish_failed(conn, job.id, now, "invalid_payload")
            return "failed"
        channel = (await store.channels(conn)).get(str(role))
        chat_id = channel.get("chat_id") if channel else None
        if not isinstance(chat_id, int):
            return await retry_at(
                conn, job, now + backoff(job.attempts + 1), "channel_not_set", counted=True, now=now
            )
        if role == "main":
            posting = await store.posting_settings(conn)
            later = next_post_time(
                now,
                windows=posting.quiet_windows,
                interval_min=posting.post_interval_min,
                last_post_at=await store.last_main_post_at(conn),
            )
            if later is not None:
                await defer(conn, job.id, later)
                return "deferred"
        return Target(chat_id=chat_id, role=str(role))
    post_id = job.payload.get("post_id")  # edit_post
    if not isinstance(post_id, int) or isinstance(post_id, bool):
        await finish_failed(conn, job.id, now, "invalid_payload")
        return "failed"
    row = (
        await conn.execute(
            select(ChannelPost.chat_id, ChannelPost.message_id).where(ChannelPost.id == post_id)
        )
    ).first()
    if row is None:
        await finish_failed(conn, job.id, now, "post_not_found")
        return "failed"
    return Target(chat_id=int(row.chat_id), post_id=post_id, message_id=int(row.message_id))


async def pick(conn: AsyncConnection, now: datetime) -> tuple[Job, Target] | None:
    """Choose and claim the next sendable job (caller commits)."""
    for job in await due_jobs(conn, now):
        target = await _resolve(conn, job, now)
        if isinstance(target, str):
            continue
        pace = await store.pacing(conn, target.chat_id)
        gap = OWNER_GAP if job.kind == "alert" else CHANNEL_GAP
        if next_send_time(now, last_sent_at=pace.last_sent_at, retry_until=pace.retry_until, gap=gap):
            continue
        await claim(conn, job.id)
        return job, target
    return None


# --- recording outcomes --------------------------------------------------------------------------


async def record_success(
    conn: AsyncConnection, job: Job, target: Target, message_id: int | None, now: datetime
) -> str:
    body = _text(job) or ""
    if job.kind == "send_post":
        assert target.role is not None and message_id is not None
        kind = job.payload.get("post_kind")
        await conn.execute(
            insert(ChannelPost).values(
                role=target.role,
                chat_id=target.chat_id,
                message_id=message_id,
                kind=(kind if isinstance(kind, str) and kind else "post")[:30],
                text_sha256=store.text_sha256(body),
                outbox_id=job.id,
                created_at=now,
            )
        )
    elif job.kind == "edit_post":
        await conn.execute(
            update(ChannelPost)
            .where(ChannelPost.id == target.post_id)
            .values(last_edited_at=now, text_sha256=store.text_sha256(body))
        )
    else:
        await store.mark_alert_delivered(conn, now)
    await store.record_sent(conn, target.chat_id, now)
    await finish_ok(conn, job.id, now)
    await store.set_health(conn, None, now)
    return "sent"


def _ambiguous(job: Job, exc: TelegramError) -> bool:
    """Telegram may have carried out the request although we got no clean answer."""
    if isinstance(exc, NetworkError):
        return exc.maybe_sent
    return isinstance(exc, ServerError | InvalidResponse)


async def record_failure(
    conn: AsyncConnection, job: Job, target: Target, exc: TelegramError, now: datetime
) -> str:
    error = str(exc)  # TelegramError text is token-free (redacted)
    if isinstance(exc, BadRequest) and exc.not_modified and job.kind == "edit_post":
        await store.record_sent(conn, target.chat_id, now)
        await finish_ok(conn, job.id, now)
        return "sent"
    if isinstance(exc, RetryAfter):
        until = now + timedelta(seconds=exc.seconds)
        await store.record_retry_after(conn, target.chat_id, until)
        return await retry_at(conn, job, until, f"retry_after {exc.seconds}s", counted=False, now=now)
    if isinstance(exc, Unauthorized):
        await store.set_health(conn, "bot_unauthorized", now)
        return await retry_at(conn, job, now + UNAUTHORIZED_PAUSE, "bot_unauthorized", counted=False, now=now)
    if job.kind == "send_post" and _ambiguous(job, exc):
        await finish_failed(conn, job.id, now, f"ambiguous_send: {error}")
        return "failed"
    if isinstance(exc, BadRequest):
        await finish_failed(conn, job.id, now, f"bad_request: {error}")
        return "failed"
    return await retry_at(conn, job, now + backoff(job.attempts + 1), error, counted=True, now=now)


# --- one step ------------------------------------------------------------------------------------


async def bot_paused(conn: AsyncConnection, now: datetime) -> bool:
    health = await store.get_setting(conn, store.KEY_HEALTH)
    if not health or health.get("error") != "bot_unauthorized":
        return False
    try:
        at = datetime.fromisoformat(str(health["at"]))
    except (KeyError, ValueError):
        return False
    return now - at < UNAUTHORIZED_PAUSE


async def process_one(
    engine: AsyncEngine, vault: Vault, make_bot: BotFactory, now: Callable[[], datetime] = store.now_utc
) -> str:
    """One outbox step. Returns what happened: no_bot, paused, idle, sent, retry, failed."""
    async with engine.begin() as conn:
        t0 = now()
        creds = await store.load_bot(conn, vault)
        if creds is None:
            return "no_bot"
        if await bot_paused(conn, t0):
            return "paused"
        await recover_interrupted(conn, t0)
        picked = await pick(conn, t0)
    if picked is None:
        return "idle"
    job, target = picked
    text_value = _text(job) or ""
    message_id: int | None = None
    failure: TelegramError | None = None
    try:
        async with make_bot(creds.token) as bot:
            if job.kind == "edit_post":
                assert target.message_id is not None
                await bot.edit_message_text(target.chat_id, target.message_id, text_value)
            else:
                message_id = (await bot.send_message(target.chat_id, text_value)).message_id
    except TelegramError as exc:
        failure = exc
    async with engine.begin() as conn:
        t1 = now()
        if failure is None:
            outcome = await record_success(conn, job, target, message_id, t1)
        else:
            outcome = await record_failure(conn, job, target, failure, t1)
    if failure is not None:
        log.warning("telegram outbox job %d (%s): %s -> %s", job.id, job.kind, failure, outcome)
    return outcome
