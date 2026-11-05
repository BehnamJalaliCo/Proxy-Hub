"""The operator account sends its outbox jobs (decision D38): ``operator_send_post``, ``operator_edit_post``.

Payloads are the bot's (proxyhub/telegram/outbox.py)::

    operator_send_post  {"role": "main" | "test", "text": "<html>", "post_kind": "auto"}
    operator_edit_post  {"post_id": <channel_posts.id>, "text": "<html>"}

One job per step (:func:`process_one`), and only while a job is due:

1. **the account's lock** (``mtproxy_account``, shared with the session check, the channel
   statistics, the @MTProxybot conversations and the dashboard's login): one Telegram operation on
   the account at a time. Busy: nothing is claimed, the job waits for the next step.
2. **pick + claim**, in one short transaction: the first due job whose channel may be written to now
   (the bot's per-channel pacing, 3 s, shared with the bot; a post to the MAIN channel waits for the
   end of a no-post window or the least gap, exactly as the bot's). Then, read again under the lock:
   the fleet's kill switch is OFF, the account is connected and its session opens, the channel has
   a public @username, Telegram has not asked the account to wait, and a post to the MAIN channel
   still has the owner's main switch on. The text becomes plain text + entities with the owner's
   animated emoji (proxyhub/posts/entities.py). The claim (``last_error = 'sending'``) is committed
   before Telegram is called.
3. **Telegram**, with no transaction open: one connection whose :class:`ChannelScope` allows posts
   and edits in THIS channel only; ``contacts.resolveUsername`` (the channel's id must be the
   configured one; remembered per session), then ``messages.sendMessage`` / ``messages.editMessage``.
4. **record**: a post lands in ``channel_posts`` with the message id Telegram gave, exactly as the
   bot's (so status edits and the channel statistics work the same); an edit updates it.

Fail closed:

* A NEW post the operator cannot send (any check in step 2, a connection or lookup failure, or a
  clean refusal by Telegram: nothing went out) is **handed to the bot**: its job becomes the bot's
  ``send_post`` with the same payload, ``posts.poster`` becomes ``bot``, and the reason is logged
  (``posts.poster_fallback``). A post to the MAIN channel whose main switch was turned off meanwhile
  is not sent at all (``main_disabled``).
* A post whose outcome is unknown (the connection broke or timed out after the request was written,
  or Telegram's answer has no message id) is **never sent again** (at most once, like the bot's: rule
  6 makes a duplicate permanent): the job fails with ``ambiguous_send`` and the owner is told.
* An edit can only be made by the account itself: it is retried with the outbox's backoff and fails
  after :data:`~proxyhub.telegram.outbox.MAX_ATTEMPTS` (the publisher then backs off and finally tells
  the owner). While the kill switch is ON, or Telegram holds the account (FLOOD_WAIT: no new post goes
  to it meanwhile either), an edit just waits, uncounted. "Message is not modified" counts as done.

Nothing here deletes anything (CLAUDE.md rule 6): the only requests are the two above.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.db.models import Outbox
from proxyhub.db.models_posts import Post
from proxyhub.db.models_telegram import ChannelPost
from proxyhub.fleet.actions import log_action
from proxyhub.mtproxy import store as mtp_store
from proxyhub.mtproxy.channel_stats import bare_channel_id
from proxyhub.mtproxy.userclient import (
    ApiCredentials,
    ChannelRef,
    ChannelScope,
    ClientOptions,
    MtpError,
    RequestNotAllowed,
    operator_session,
)
from proxyhub.posts import entities, operator
from proxyhub.posts import settings as posting
from proxyhub.posts.messages import OPERATOR_POST_UNKNOWN_FA
from proxyhub.telegram import outbox as bot_outbox
from proxyhub.telegram import store
from proxyhub.telegram.schedule import CHANNEL_GAP, backoff, next_post_time, next_send_time
from proxyhub.vault import SecretValue, Vault, VaultError

log = logging.getLogger(__name__)

ACTOR: Final = "operator_poster"
PICK_LIMIT: Final = 20
DEFAULT_LOCK_WAIT_S: Final = 2.0
KILL_SWITCH_WAIT: Final = timedelta(minutes=5)
# Telegram's errors on an edit that no retry can fix (the post is gone, not ours, or not editable).
_EDIT_FINAL: Final = frozenset(
    {
        "MESSAGE_ID_INVALID",
        "MESSAGE_EDIT_TIME_EXPIRED",
        "MESSAGE_AUTHOR_REQUIRED",
        "CHAT_ADMIN_REQUIRED",
        "CHAT_WRITE_FORBIDDEN",
        "MESSAGE_EMPTY",
        "MESSAGE_TOO_LONG",
        "ENTITY_BOUNDS_INVALID",
        "ENTITIES_TOO_LONG",
    }
)
# A lookup that a cached channel reference may have caused: forget it.
_STALE_REF: Final = frozenset({"CHANNEL_INVALID", "CHANNEL_PRIVATE", "PEER_ID_INVALID"})

Clock = Callable[[], datetime]
Refs = dict[tuple[str, int], ChannelRef]


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int

    @property
    def text(self) -> str:
        value = self.payload.get("text")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class Target:
    role: str
    chat_id: int
    username: str | None  # the channel's public name (lower case, no @), or None
    post_id: int | None = None  # edit: channel_posts.id
    message_id: int | None = None  # edit

    @property
    def channel_id(self) -> int | None:
        return bare_channel_id(self.chat_id)


@dataclass(frozen=True)
class Ready:
    """What step 3 needs, read under the account's lock (the session is a secret: vault only)."""

    creds: ApiCredentials
    session: SecretValue
    session_id: str
    message: entities.MessageText

    def __repr__(self) -> str:
        return f"Ready(session_id={self.session_id!r}, ***)"


@dataclass
class Result:
    """How step 3 ended. ``stage``: where it stopped (connect, resolve, send, done)."""

    stage: str = "connect"
    message_id: int | None = None
    error: MtpError | None = None
    refused: bool = False


# --- step 2: pick, check, claim -------------------------------------------------------------------------


async def recover_interrupted(conn: AsyncConnection, now: datetime) -> int:
    """Jobs still claimed by an earlier step whose outcome was never recorded: a post is failed and
    never resent (at most once), an edit is tried again."""
    rows = (
        await conn.execute(
            select(Outbox.id, Outbox.kind).where(
                Outbox.done_at.is_(None), Outbox.last_error == store.SENDING, Outbox.kind.in_(operator.KINDS)
            )
        )
    ).all()
    for row in rows:
        if row.kind == operator.SEND:
            await bot_outbox.finish_failed(
                conn, row.id, now, "ambiguous_send: interrupted while sending; not resent"
            )
        else:
            await conn.execute(update(Outbox).where(Outbox.id == row.id).values(last_error="interrupted"))
    return len(rows)


def _due(now: datetime) -> Any:
    return select(Outbox.id, Outbox.kind, Outbox.payload, Outbox.attempts).where(
        Outbox.done_at.is_(None),
        Outbox.kind.in_(operator.KINDS),
        Outbox.not_before <= now,
        Outbox.last_error.is_distinct_from(store.SENDING),
    )


async def anything_due(conn: AsyncConnection, now: datetime) -> bool:
    """One cheap read, before the account's lock is even asked for."""
    return (await conn.execute(_due(now).limit(1))).first() is not None


async def _due_jobs(conn: AsyncConnection, now: datetime) -> list[Job]:
    rows = (
        await conn.execute(
            _due(now)
            .order_by(Outbox.not_before, Outbox.id)
            .limit(PICK_LIMIT)
            .with_for_update(skip_locked=True)
        )
    ).all()
    return [
        Job(
            id=int(r.id),
            kind=str(r.kind),
            payload=dict(r.payload) if isinstance(r.payload, dict) else {},
            attempts=int(r.attempts),
        )
        for r in rows
    ]


async def _target(conn: AsyncConnection, job: Job, now: datetime) -> Target | str:
    """Where this job goes, or an outcome after updating the job (failed, retry, deferred)."""
    if not job.text.strip():
        await bot_outbox.finish_failed(conn, job.id, now, "invalid_payload")
        return "failed"
    channels = await store.channels(conn)
    if job.kind == operator.SEND:
        role = job.payload.get("role")
        if role not in store.ROLES:
            await bot_outbox.finish_failed(conn, job.id, now, "invalid_payload")
            return "failed"
        channel = channels.get(str(role))
        chat_id = channel.get("chat_id") if channel else None
        if not isinstance(chat_id, int):
            return await _retry(conn, job, now, "channel_not_set")
        if role == "main":
            posting_settings = await store.posting_settings(conn)
            later = next_post_time(
                now,
                windows=posting_settings.quiet_windows,
                interval_min=posting_settings.post_interval_min,
                last_post_at=await store.last_main_post_at(conn),
            )
            if later is not None:
                await bot_outbox.defer(conn, job.id, later)
                return "deferred"
        return Target(role=str(role), chat_id=chat_id, username=operator.public_username(channel))
    post_id = job.payload.get("post_id")
    if not isinstance(post_id, int) or isinstance(post_id, bool):
        await bot_outbox.finish_failed(conn, job.id, now, "invalid_payload")
        return "failed"
    row = (
        await conn.execute(
            select(ChannelPost.role, ChannelPost.chat_id, ChannelPost.message_id).where(
                ChannelPost.id == post_id
            )
        )
    ).first()
    if row is None:
        await bot_outbox.finish_failed(conn, job.id, now, "post_not_found")
        return "failed"
    channel = channels.get(str(row.role))
    same = channel is not None and channel.get("chat_id") == int(row.chat_id)
    return Target(
        role=str(row.role),
        chat_id=int(row.chat_id),
        username=operator.public_username(channel) if same else None,
        post_id=post_id,
        message_id=int(row.message_id),
    )


async def pick(conn: AsyncConnection, now: datetime) -> tuple[Job, Target] | None:
    """The next job that may go now (not claimed yet)."""
    for job in await _due_jobs(conn, now):
        target = await _target(conn, job, now)
        if isinstance(target, str):
            continue
        pace = await store.pacing(conn, target.chat_id)
        if next_send_time(now, last_sent_at=pace.last_sent_at, retry_until=pace.retry_until, gap=CHANNEL_GAP):
            continue
        return job, target
    return None


async def readiness(
    conn: AsyncConnection, vault: Vault, job: Job, target: Target, now: datetime
) -> Ready | str:
    """Everything step 3 needs, or why the operator cannot send this job now (a short code)."""
    if job.kind == operator.SEND and not await _may_post(conn, target.role):
        return "main_disabled"
    reason = await operator.account_ready(conn)
    if reason is not None:
        return reason
    if target.username is None or target.channel_id is None:
        return "no_username"
    until = await operator.hold_until(conn)
    if until is not None and until > now:
        return "hold"
    try:
        creds = await mtp_store.load_creds(conn, vault)
        session = await mtp_store.load_session(conn, vault)
    except VaultError:
        return "session_unreadable"
    row = await mtp_store.load_row(conn)
    if creds is None or session is None or row.session_id is None:
        return "not_connected"
    message = entities.operator_message(job.text, await operator.emoji_map(conn))
    return Ready(creds=creds, session=session, session_id=row.session_id, message=message)


async def _retry(
    conn: AsyncConnection,
    job: Job,
    now: datetime,
    error: str,
    *,
    at: datetime | None = None,
    counted: bool = True,
) -> str:
    view = bot_outbox.Job(
        id=job.id, kind=job.kind, payload=job.payload, attempts=job.attempts, last_error=None
    )
    return await bot_outbox.retry_at(
        conn, view, at or now + backoff(job.attempts + 1), error, counted=counted, now=now
    )


async def _may_post(conn: AsyncConnection, role: str) -> bool:
    """A new post may go to the MAIN channel only while the owner's main switch is on."""
    return role != "main" or (await posting.load(conn)).main_enabled


async def _main_disabled(conn: AsyncConnection, job: Job, now: datetime) -> str:
    await bot_outbox.finish_failed(conn, job.id, now, "main_disabled: the owner turned main posting off")
    await log_action(
        conn,
        actor=ACTOR,
        action="posts.operator_not_sent",
        target=f"main:outbox:{job.id}",
        dry_run=False,
        allowed=False,
        reasons=["main_disabled"],
    )
    return "failed"


async def hand_to_bot(
    conn: AsyncConnection, job: Job, target: Target, reason: str, now: datetime, *, rpc: str | None = None
) -> str:
    """A NEW post the operator did not send goes to the bot as it is (nothing went out). Not to the
    MAIN channel once the owner turned main posting off."""
    if not await _may_post(conn, target.role):
        return await _main_disabled(conn, job, now)
    await conn.execute(
        update(Outbox)
        .where(Outbox.id == job.id)
        .values(kind="send_post", not_before=now, attempts=0, last_error=None)
    )
    post_id = (
        await conn.execute(
            update(Post).where(Post.outbox_id == job.id).values(poster="bot").returning(Post.id)
        )
    ).scalar_one_or_none()
    await operator.log_fallback(
        conn,
        actor=ACTOR,
        role=target.role,
        post_id=int(post_id) if post_id is not None else None,
        reason=reason,
        stage="send",
        outbox_id=job.id,
        rpc=rpc,
    )
    return "handed_to_bot"


async def not_now(conn: AsyncConnection, job: Job, target: Target, reason: str, now: datetime) -> str:
    """The operator cannot send ``job`` now (step 2): a post goes to the bot (or nowhere, if it was
    for the main channel whose switch is off now); an edit waits."""
    if job.kind == operator.SEND:
        if reason == "main_disabled":
            return await _main_disabled(conn, job, now)
        return await hand_to_bot(conn, job, target, reason, now)
    if reason == "hold":
        until = await operator.hold_until(conn)
        return await _retry(conn, job, now, "operator_hold", at=until, counted=False)
    if reason == "kill_switch":  # the owner stopped everything on purpose: edits wait, uncounted
        return await _retry(conn, job, now, "operator_kill_switch", at=now + KILL_SWITCH_WAIT, counted=False)
    return await _retry(conn, job, now, f"operator_{reason}")


# --- step 3: Telegram ---------------------------------------------------------------------------------------


async def talk(ready: Ready, job: Job, target: Target, options: ClientOptions | None, refs: Refs) -> Result:
    """Send or edit, on one connection allowed to post in this one channel."""
    assert target.username is not None and target.channel_id is not None
    channel_id = target.channel_id
    scope = ChannelScope(
        channel_ids=frozenset({channel_id}),
        usernames=frozenset({target.username}),
        post_ids=frozenset({channel_id}),
    )
    result = Result()
    key = (ready.session_id, channel_id)
    try:
        async with operator_session(ready.creds, ready.session, options=options, channels=scope) as op:
            result.stage = "resolve"
            ref = refs.get(key)
            if ref is None:
                ref = await op.resolve_channel(target.username, channel_id)
                refs[key] = ref
            result.stage = "send"
            if job.kind == operator.SEND:
                result.message_id = await op.post_in_channel(ref, ready.message.text, ready.message.entities)
            else:
                assert target.message_id is not None
                await op.edit_channel_post(ref, target.message_id, ready.message.text, ready.message.entities)
            result.stage = "done"
    except MtpError as exc:
        if exc.rpc in _STALE_REF or exc.code == "channel_not_found":
            refs.pop(key, None)
        result.error = exc
    except RequestNotAllowed as exc:  # a bug of ours: the allowlist stopped it before anything left
        log.error("operator poster: request refused by the allowlist (%s)", exc)
        result.refused = True
    return result


# --- step 4: record -----------------------------------------------------------------------------------------


async def _sent(conn: AsyncConnection, job: Job, target: Target, result: Result, now: datetime) -> str:
    body = job.text
    if job.kind == operator.SEND:
        if result.message_id is None:
            return await _unknown(conn, job, target, now, "no message id in Telegram's answer")
        kind = job.payload.get("post_kind")
        await conn.execute(
            insert(ChannelPost).values(
                role=target.role,
                chat_id=target.chat_id,
                message_id=result.message_id,
                kind=(kind if isinstance(kind, str) and kind else "post")[:30],
                text_sha256=store.text_sha256(body),
                outbox_id=job.id,
                created_at=now,
            )
        )
    else:
        await conn.execute(
            update(ChannelPost)
            .where(ChannelPost.id == target.post_id)
            .values(last_edited_at=now, text_sha256=store.text_sha256(body))
        )
    await store.record_sent(conn, target.chat_id, now)
    await bot_outbox.finish_ok(conn, job.id, now)
    return "sent"


async def _unknown(conn: AsyncConnection, job: Job, target: Target, now: datetime, why: str) -> str:
    """A post that may be in the channel: never sent again (at most once); the owner is told."""
    await bot_outbox.finish_failed(conn, job.id, now, f"ambiguous_send: {why}")
    await log_action(
        conn,
        actor=ACTOR,
        action="posts.operator_send_unknown",
        target=f"{target.role}:outbox:{job.id}",
        dry_run=False,
        allowed=False,
        reasons=["ambiguous_send"],
    )
    await store.enqueue_owner_alert(conn, OPERATOR_POST_UNKNOWN_FA)
    return "failed"


async def record(conn: AsyncConnection, job: Job, target: Target, result: Result, now: datetime) -> str:
    """Account for step 3 (see the module doc). Returns sent, handed_to_bot, retry or failed."""
    if result.stage == "done" and result.error is None and not result.refused:
        return await _sent(conn, job, target, result, now)
    if result.refused:
        if job.kind == operator.SEND:
            return await hand_to_bot(conn, job, target, "request_refused", now)
        await bot_outbox.finish_failed(conn, job.id, now, "request_refused")
        return "failed"
    error = result.error
    assert error is not None
    if error.code == "flood_wait":
        until = now + timedelta(seconds=max(1, error.seconds or 60))
        await operator.set_hold(conn, until, now)
        if job.kind == operator.SEND:
            return await hand_to_bot(conn, job, target, "flood_wait", now, rpc=error.rpc)
        return await _retry(conn, job, now, "operator_flood_wait", at=until, counted=False)
    if job.kind == operator.SEND:
        if result.stage == "send" and error.code == "telegram_unavailable":
            return await _unknown(conn, job, target, now, f"no clean answer from Telegram ({error})")
        return await hand_to_bot(conn, job, target, error.code, now, rpc=error.rpc)
    if error.rpc == "MESSAGE_NOT_MODIFIED":  # the channel shows this text already
        await store.record_sent(conn, target.chat_id, now)
        await bot_outbox.finish_ok(conn, job.id, now)
        return "sent"
    if error.rpc in _EDIT_FINAL:
        await bot_outbox.finish_failed(conn, job.id, now, f"bad_request: {error}")
        return "failed"
    return await _retry(conn, job, now, f"operator_{error.code}" + (f" ({error.rpc})" if error.rpc else ""))


# --- one step -----------------------------------------------------------------------------------------------


@dataclass
class Deps:
    engine: AsyncEngine
    vault: Vault
    options: ClientOptions | None = None
    clock: Clock = store.now_utc
    lock_wait_s: float = DEFAULT_LOCK_WAIT_S
    refs: Refs = field(default_factory=dict)  # channel references per (session, channel)


async def process_one(deps: Deps) -> str:
    """One step. Returns idle, busy, deferred, handed_to_bot, sent, retry or failed. The caller runs
    one step at a time (the loop's own advisory lock), so a claim found here is a dead step's."""
    async with deps.engine.begin() as conn:
        await recover_interrupted(conn, deps.clock())
        if not await anything_due(conn, deps.clock()):
            return "idle"
    try:
        async with mtp_store.account_lock(deps.engine, wait_s=deps.lock_wait_s):
            return await _locked(deps)
    except mtp_store.LockBusy:
        return "busy"


async def _locked(deps: Deps) -> str:
    async with deps.engine.begin() as conn:
        now = deps.clock()
        picked = await pick(conn, now)
        if picked is None:
            return "idle"
        job, target = picked
        ready = await readiness(conn, deps.vault, job, target, now)
        if isinstance(ready, str):
            return await not_now(conn, job, target, ready, now)
        await bot_outbox.claim(conn, job.id)
    result = await talk(ready, job, target, deps.options, deps.refs)
    async with deps.engine.begin() as conn:
        outcome = await record(conn, job, target, result, deps.clock())
    if outcome != "sent":
        log.warning(
            "operator poster: job %d (%s) %s at %s (%s)",
            job.id,
            job.kind,
            outcome,
            result.stage,
            result.error.code if result.error else ("refused" if result.refused else "-"),
        )
    return outcome
