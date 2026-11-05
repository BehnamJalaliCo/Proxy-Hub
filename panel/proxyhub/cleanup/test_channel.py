"""«پاک کردن کانال آزمایشی» (decision D44): empty the TEST Telegram channel. Never the main one.

The owner asked for it: "the test channel should be cleaned, whatever is in it". Rule 6 (posts are
never removed) keeps holding for the MAIN channel in every code path: the main channel's id is
passed to the client's one guarded method, which refuses it (proxyhub/telegram/client.py
``delete_test_channel_messages``), and a test channel id equal to the main one is refused here.

How it goes (:func:`clean`), under the advisory lock :data:`LOCK_NAME` (one cleaning at a time):

1. The bot, the stored test channel and, live from Telegram (``getChatMember``), the bot's rights
   there. The bot needs «حذف پیام‌ها» (``can_delete_messages``) to remove the operator account's
   posts too; without it the owner is asked to grant it for the time of the cleaning (and to take
   it back afterwards). The refusal is logged.
2. In one transaction: refuse while a post is on its way to the test channel; the highest message
   id we know there (``channel_posts``, the channel statistics) plus :data:`MARGIN`; and a hold on
   the channel's send pacing (``telegram_pacing.retry_until``, honoured by the bot's outbox and the
   operator's) so nothing new is posted there while it is cleaned.
3. The guarded removal request from the newest id down to :data:`FIRST_ID` (message 1 is Telegram's own
   "channel created" notice, which nobody can remove), 100 ids per request, paced; Telegram skips
   ids that do not exist. A request Telegram refuses (an id it does not let the bot remove, e.g.
   older than its 48-hour rule) is split into tens, then single ids; a single refused id stays in
   the channel and is counted. 429 waits ``retry_after`` (up to :data:`MAX_WAIT_S`). The whole run
   has a budget (:data:`MAX_CALLS`, :data:`BUDGET_S`); what is left can be done by pressing again.
4. In one transaction: the panel forgets the test-channel posts whose ids were removed
   (``channel_posts`` rows, their ``posts`` and ``post_links``), closes the edit jobs still waiting
   for them (nothing is left to edit), lifts the hold and writes one actions_log row with counts.
   The publisher then never tries to edit a removed post; an edit that was already in flight ends
   as Telegram's "message to edit not found", which the outbox fails gracefully.

Nothing secret is logged or returned: counts, the id range, and the channel's public name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.cleanup.messages import ERRORS_FA, STOPS_FA
from proxyhub.fleet.actions import iso, log_action
from proxyhub.reconcilers.base import lock_key
from proxyhub.telegram import store as tg_store
from proxyhub.telegram.client import (
    TEST_CHANNEL_CLEANUP_MAX_IDS,
    BadRequest,
    BotClient,
    Forbidden,
    MemberRights,
    NotFound,
    RetryAfter,
    TelegramError,
    Unauthorized,
)
from proxyhub.vault import SecretValue, Vault

log = logging.getLogger(__name__)

CONFIRM_TEXT: Final = "پاک کن"
FIRST_ID: Final = 2
MARGIN: Final = 200
BATCH: Final = TEST_CHANNEL_CLEANUP_MAX_IDS
SPLIT: Final = 10
CALL_GAP_S: Final = 0.5
MAX_CALLS: Final = 300
BUDGET_S: Final = 150.0
MAX_WAIT_S: Final = 30
NETWORK_RETRY_S: Final = 2.0
HOLD: Final = timedelta(minutes=5)
LOCK_NAME: Final = "test_channel_cleanup"
SEND_KINDS: Final = ("send_post", "operator_send_post")
EDIT_KINDS: Final = ("edit_post", "operator_edit_post")
ACTION: Final = "telegram.test_channel_cleaned"
ACTION_REFUSED: Final = "telegram.test_channel_clean_refused"

BotFactory = Callable[[SecretValue], BotClient]
Sleep = Callable[[float], Awaitable[None]]


class Refused(Exception):
    """The cleaning did not start (nothing was removed). ``code`` keys :data:`ERRORS_FA`."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status

    @property
    def message_fa(self) -> str:
        return ERRORS_FA.get(self.code, self.code)


# --- the owner's typed confirmation --------------------------------------------------------------

_SPACES = re.compile(r"[\s‌‍‎‏]+")


def normalize_confirm(value: str) -> str:
    """Spacing, zero-width joiners and Arabic letter forms do not matter («پاک‌کن» = «پاک کن»)."""
    value = unicodedata.normalize("NFC", value).replace("ك", "ک").replace("ي", "ی")
    return _SPACES.sub(" ", value).strip()


def confirmed(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 40 and normalize_confirm(value) == CONFIRM_TEXT


# --- where -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    test_chat_id: int
    main_chat_id: int | None
    username: str
    title: str


def _chat_id(channel: dict[str, Any] | None) -> int | None:
    value = (channel or {}).get("chat_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def target(db: tg_store.Db) -> Target:
    """The stored test channel, or :class:`Refused`. Only a TEST channel ever comes back: with
    only a main channel set this refuses, and so it does when both are the same chat."""
    channels = await tg_store.channels(db)
    test = channels.get("test")
    test_id = _chat_id(test)
    if test is None or test_id is None:
        raise Refused("test_channel_not_set", 409)
    main_id = _chat_id(channels.get("main"))
    if main_id is not None and main_id == test_id:
        raise Refused("test_is_main", 409)
    return Target(
        test_chat_id=test_id,
        main_chat_id=main_id,
        username=str(test.get("username") or test_id),
        title=str(test.get("title") or ""),
    )


async def highest_known_id(db: tg_store.Db, chat_id: int) -> int | None:
    value = (
        await db.execute(
            text(
                "SELECT GREATEST((SELECT max(message_id) FROM channel_posts WHERE chat_id = :c), "
                "(SELECT max(message_id) FROM analytics_post_views WHERE chat_id = :c))"
            ),
            {"c": chat_id},
        )
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def id_range(highest: int | None) -> tuple[int, int]:
    """From :data:`FIRST_ID` to the highest id we know there plus :data:`MARGIN` (posts the panel
    did not record, e.g. the owner's own, are usually just above the last one we know)."""
    return FIRST_ID, max(FIRST_ID, (highest or 0) + MARGIN)


def request_count(first: int, last: int) -> int:
    return 0 if last < first else (last - first) // BATCH + 1


async def pending_sends(db: tg_store.Db) -> int:
    """New posts on their way to the TEST channel (bot or operator account)."""
    return int(
        (
            await db.execute(
                text(
                    "SELECT count(*) FROM outbox WHERE done_at IS NULL AND kind = ANY(:kinds) "
                    "AND payload->>'role' = 'test'"
                ),
                {"kinds": list(SEND_KINDS)},
            )
        ).scalar_one()
    )


async def known_rows(db: tg_store.Db, t: Target) -> dict[str, int]:
    """The panel's records of the test channel's posts (what a full cleaning makes it forget)."""
    channel_posts, posts = (
        await db.execute(
            text(
                "SELECT (SELECT count(*) FROM channel_posts WHERE role = 'test' AND chat_id = :c), "
                "(SELECT count(*) FROM posts p WHERE p.role = 'test' AND EXISTS (SELECT 1 "
                "FROM channel_posts cp WHERE cp.outbox_id = p.outbox_id AND cp.role = 'test' "
                "AND cp.chat_id = :c))"
            ),
            {"c": t.test_chat_id},
        )
    ).one()
    return {"channel_posts": int(channel_posts), "posts": int(posts)}


# --- the bot's rights --------------------------------------------------------------------------------


async def bot_rights(make_bot: BotFactory, creds: tg_store.BotCredentials, chat_id: int) -> MemberRights:
    """The bot's own membership in the test channel, live from Telegram (:class:`Refused` if it
    cannot be read)."""
    try:
        async with make_bot(creds.token) as bot:
            return await bot.get_chat_member(chat_id, creds.info.id)
    except (Unauthorized, NotFound):
        raise Refused("bot_token_invalid", 400) from None
    except Forbidden:
        raise Refused("bot_not_admin", 409) from None
    except BadRequest:
        raise Refused("channel_not_found", 409) from None
    except TelegramError as exc:
        log.warning("test channel cleanup: rights not read (%s)", exc)
        raise Refused("telegram_unavailable", 502) from None


def rights_problem(rights: MemberRights) -> str | None:
    if rights.status not in ("administrator", "creator"):
        return "bot_not_admin"
    if not rights.can_remove:
        return "bot_cannot_delete"
    return None


# --- the sweep (pure): which ids next, and what came of them ---------------------------------------


def _chunks(low: int, high: int, size: int) -> list[tuple[int, int]]:
    """``low..high`` in chunks of ``size``, the highest first."""
    out: list[tuple[int, int]] = []
    hi = high
    while hi >= low:
        lo = max(low, hi - size + 1)
        out.append((lo, hi))
        hi = lo - 1
    return out


@dataclass
class Sweep:
    """The ranges still to send (inclusive, next first, newest ids first), the ranges Telegram
    accepted, and the single ids it refused. The caller sends :meth:`next` and reports with
    :meth:`ok` or :meth:`refused`."""

    todo: list[tuple[int, int]]
    done: list[tuple[int, int]] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)

    @classmethod
    def over(cls, first: int, last: int) -> Sweep:
        return cls(todo=_chunks(first, last, BATCH))

    def next(self) -> tuple[int, int] | None:
        return self.todo[0] if self.todo else None

    def ok(self) -> None:
        self.done.append(self.todo.pop(0))

    def refused(self) -> None:
        """Telegram refused the whole request: try its tens, then its single ids; a single id it
        refuses stays in the channel."""
        low, high = self.todo.pop(0)
        size = high - low + 1
        if size == 1:
            self.kept.append(low)
            return
        self.todo[0:0] = _chunks(low, high, SPLIT if size > SPLIT else 1)

    def removed_ids(self) -> list[int]:
        return sorted(i for low, high in self.done for i in range(low, high + 1))

    @property
    def covered(self) -> int:
        return sum(high - low + 1 for low, high in self.done)


@dataclass
class Outcome:
    calls: int = 0
    stop: str | None = None  # why it ended early: budget, flood_wait, bot_cannot_delete, ...
    flood_until: datetime | None = None


async def sweep_channel(
    bot: BotClient,
    t: Target,
    sweep: Sweep,
    *,
    now: Callable[[], datetime] = tg_store.now_utc,
    sleep: Sleep = asyncio.sleep,
    budget_s: float = BUDGET_S,
    max_calls: int = MAX_CALLS,
) -> Outcome:
    """Send the sweep's requests until it is done, refused for good, or out of budget."""
    out = Outcome()
    started = time.monotonic()
    retried: tuple[int, int] | None = None
    while (chunk := sweep.next()) is not None:
        if out.calls >= max_calls or time.monotonic() - started >= budget_s:
            out.stop = "budget"
            break
        if out.calls:
            await sleep(CALL_GAP_S)
        out.calls += 1
        try:
            await bot.delete_test_channel_messages(
                t.test_chat_id,
                list(range(chunk[0], chunk[1] + 1)),
                test_chat_id=t.test_chat_id,
                main_chat_id=t.main_chat_id,
            )
        except RetryAfter as exc:
            left = budget_s - (time.monotonic() - started)
            if exc.seconds > MAX_WAIT_S or exc.seconds >= left:
                out.stop, out.flood_until = "flood_wait", now() + timedelta(seconds=exc.seconds)
                break
            await sleep(float(exc.seconds))
        except BadRequest as exc:
            if exc.chat_not_found:
                out.stop = "channel_not_found"
                break
            sweep.refused()
        except Forbidden:
            out.stop = "bot_cannot_delete"
            break
        except (Unauthorized, NotFound):
            out.stop = "bot_token_invalid"
            break
        except TelegramError as exc:  # network, 5xx, unreadable answer: once more, then stop
            if retried == chunk:
                log.warning("test channel cleanup: stopped (%s)", exc)
                out.stop = "telegram_unavailable"
                break
            retried = chunk
            await sleep(NETWORK_RETRY_S)
        else:
            sweep.ok()
    return out


# --- after the sweep: what the panel forgets ---------------------------------------------------------


async def forget_removed(conn: AsyncConnection, t: Target, ids: list[int], now: datetime) -> dict[str, int]:
    """The panel's records of the removed test-channel posts go, and edits waiting for them close
    (nothing is left to edit). Never a row of the main channel's chat."""
    counts = {"channel_posts": 0, "posts": 0, "post_links": 0, "edit_jobs_closed": 0}
    if not ids:
        return counts
    rows = (
        await conn.execute(
            text(
                "SELECT id, outbox_id FROM channel_posts WHERE role = 'test' AND chat_id = :c "
                "AND chat_id IS DISTINCT FROM :main AND message_id = ANY(:ids) FOR UPDATE"
            ),
            {"c": t.test_chat_id, "main": t.main_chat_id, "ids": ids},
        )
    ).all()
    if not rows:
        return counts
    cp_ids = [int(r.id) for r in rows]
    outbox_ids = [int(r.outbox_id) for r in rows if r.outbox_id is not None]
    counts["edit_jobs_closed"] = int(
        (
            await conn.execute(
                text(
                    "UPDATE outbox SET done_at = :now, last_error = NULL WHERE done_at IS NULL "
                    "AND kind = ANY(:kinds) AND payload->>'post_id' = ANY(:cp)"
                ),
                {"now": now, "kinds": list(EDIT_KINDS), "cp": [str(i) for i in cp_ids]},
            )
        ).rowcount
        or 0
    )
    post_ids = list(
        (
            await conn.execute(
                text("SELECT id FROM posts WHERE role = 'test' AND outbox_id = ANY(:o) FOR UPDATE"),
                {"o": outbox_ids},
            )
        ).scalars()
    )
    if post_ids:
        counts["post_links"] = int(
            (
                await conn.execute(text("DELETE FROM post_links WHERE post_id = ANY(:p)"), {"p": post_ids})
            ).rowcount
            or 0
        )
        counts["posts"] = int(
            (
                await conn.execute(
                    text("DELETE FROM posts WHERE id = ANY(:p) AND role = 'test'"), {"p": post_ids}
                )
            ).rowcount
            or 0
        )
    counts["channel_posts"] = int(
        (
            await conn.execute(
                text(
                    "DELETE FROM channel_posts WHERE id = ANY(:i) AND role = 'test' AND chat_id = :c "
                    "AND chat_id IS DISTINCT FROM :main"
                ),
                {"i": cp_ids, "c": t.test_chat_id, "main": t.main_chat_id},
            )
        ).rowcount
        or 0
    )
    return counts


# --- the whole cleaning ------------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanResult:
    channel: str
    first_id: int
    last_id: int
    requests: int
    covered: int  # ids in requests Telegram accepted (removed, or not there)
    refused: int  # single ids Telegram did not let the bot remove (still in the channel)
    stop: str | None
    removed: dict[str, int]

    def as_json(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "message_ids": {"first": self.first_id, "last": self.last_id},
            "requests": self.requests,
            "covered": self.covered,
            "refused": self.refused,
            "finished": self.stop is None,
            "stop": self.stop,
            "stop_fa": STOPS_FA.get(self.stop) if self.stop else None,
            "removed": dict(self.removed),
        }


@contextlib.asynccontextmanager
async def _one_at_a_time(engine: AsyncEngine) -> AsyncIterator[None]:
    """The session advisory lock :data:`LOCK_NAME`, on its own connection without a transaction."""
    async with engine.connect() as conn:
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key(LOCK_NAME)})
        ).scalar_one()
        await conn.commit()
        if not got:
            raise Refused("cleanup_running", 409)
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(LOCK_NAME)})
            await conn.commit()


async def _save_rights(conn: AsyncConnection, rights: MemberRights, now: datetime) -> None:
    """The test channel's stored rights as Telegram just reported them (the Telegram page warns
    while the bot keeps «حذف پیام‌ها»)."""
    channels = await tg_store.channels(conn)
    test = channels.get("test")
    if test is None:
        return
    test.update(
        can_post=rights.can_post, can_edit=rights.can_edit, can_delete=rights.can_remove, checked_at=iso(now)
    )
    await tg_store.save_channel(conn, "test", test)


async def _release_hold(
    conn: AsyncConnection, chat_id: int, before: datetime | None, flood_until: datetime | None, now: datetime
) -> None:
    """The pacing hold ends: back to what it was (a real flood wait is kept)."""
    until = max(x for x in (before, flood_until, now) if x is not None)
    await tg_store.record_retry_after(conn, chat_id, until)


async def preview(engine: AsyncEngine, vault: Vault, make_bot: BotFactory) -> dict[str, Any]:
    """What a cleaning would do now, and what blocks it (``blocked`` with its Persian reason)."""
    out: dict[str, Any] = {
        "channel": None,
        "main_channel_set": False,
        "can_delete": None,
        "message_ids": None,
        "requests": 0,
        "rows": {"channel_posts": 0, "posts": 0},
        "pending_sends": 0,
        "blocked": None,
        "blocked_fa": None,
    }

    def blocked(code: str) -> dict[str, Any]:
        out["blocked"], out["blocked_fa"] = code, ERRORS_FA.get(code, code)
        return out

    async with engine.connect() as conn:
        creds = await tg_store.load_bot(conn, vault)
        channels = await tg_store.channels(conn)
        out["main_channel_set"] = _chat_id(channels.get("main")) is not None
        if creds is None:
            return blocked("bot_not_set")
        try:
            t = await target(conn)
        except Refused as exc:
            return blocked(exc.code)
        out["channel"] = {"username": t.username, "title": t.title, "chat_id": t.test_chat_id}
        first, last = id_range(await highest_known_id(conn, t.test_chat_id))
        out["message_ids"] = {"first": first, "last": last}
        out["requests"] = request_count(first, last)
        out["rows"] = await known_rows(conn, t)
        out["pending_sends"] = await pending_sends(conn)
        await conn.rollback()
    try:
        rights = await bot_rights(make_bot, creds, t.test_chat_id)
    except Refused as exc:
        return blocked(exc.code)
    out["can_delete"] = rights.can_remove
    problem = rights_problem(rights)
    if problem is not None:
        return blocked(problem)
    if out["pending_sends"]:
        return blocked("send_pending")
    return out


async def clean(
    engine: AsyncEngine,
    vault: Vault,
    make_bot: BotFactory,
    *,
    actor: str,
    now: Callable[[], datetime] = tg_store.now_utc,
    sleep: Sleep = asyncio.sleep,
    budget_s: float = BUDGET_S,
) -> CleanResult:
    """Empty the test channel (module doc). Raises :class:`Refused` before anything is removed."""
    async with engine.connect() as conn:
        creds = await tg_store.load_bot(conn, vault)
        if creds is None:
            raise Refused("bot_not_set", 409)
        t = await target(conn)
        if await pending_sends(conn):  # early, before asking Telegram; checked again under the hold
            raise Refused("send_pending", 409)
        await conn.rollback()
    async with _one_at_a_time(engine):
        rights = await bot_rights(make_bot, creds, t.test_chat_id)
        problem = rights_problem(rights)
        if problem is not None:
            async with engine.begin() as conn:
                await _save_rights(conn, rights, now())
                await log_action(
                    conn,
                    actor=actor,
                    action=ACTION_REFUSED,
                    target=f"test:{t.username}"[:200],
                    dry_run=False,
                    allowed=False,
                    reasons=[problem],
                )
            raise Refused(problem, 409)
        async with engine.begin() as conn:
            if await target(conn) != t:
                raise Refused("channels_changed", 409)
            if await pending_sends(conn):
                raise Refused("send_pending", 409)
            first, last = id_range(await highest_known_id(conn, t.test_chat_id))
            started = now()
            hold_before = (await tg_store.pacing(conn, t.test_chat_id)).retry_until
            await tg_store.record_retry_after(conn, t.test_chat_id, started + HOLD)
            await _save_rights(conn, rights, started)
        sweep = Sweep.over(first, last)
        outcome = Outcome(stop="telegram_unavailable")
        try:
            async with make_bot(creds.token) as bot:
                outcome = await sweep_channel(bot, t, sweep, now=now, sleep=sleep, budget_s=budget_s)
        finally:
            async with engine.begin() as conn:
                finished_at = now()
                removed = await forget_removed(conn, t, sweep.removed_ids(), finished_at)
                await _release_hold(conn, t.test_chat_id, hold_before, outcome.flood_until, finished_at)
                result = CleanResult(
                    channel=t.username,
                    first_id=first,
                    last_id=last,
                    requests=outcome.calls,
                    covered=sweep.covered,
                    refused=len(sweep.kept),
                    stop=outcome.stop,
                    removed=removed,
                )
                await log_action(
                    conn,
                    actor=actor,
                    action=ACTION,
                    target=f"test:{t.username}"[:200],
                    dry_run=False,
                    allowed=True,
                    reasons=["owner_request", "D44", *([outcome.stop] if outcome.stop else [])],
                    context=result.as_json() | {"stop_fa": None},
                    result=(
                        f"test channel: {result.covered} ids swept in {result.requests} requests, "
                        f"{result.refused} refused by Telegram, "
                        f"{removed['channel_posts']} known posts forgotten"
                    ),
                )
    return result
