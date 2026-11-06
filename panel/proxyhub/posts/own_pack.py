"""The channel's own animated emoji pack (D38): the bot makes it, the operator poster uses it.

The owner asked for an emoji pack about Iran's internet and operators. No public pack fits, and a
pack made by someone else shows ITS title (often another channel's ad) when a reader taps one of
its emoji. So the bot makes our own pack from the drawings in proxyhub/posts/emoji_art.py:

* name ``<channel>_by_<bot>`` for the first drawings, ``<channel>_v<N>_by_<bot>`` for version N of
  them (:data:`~proxyhub.posts.emoji_art.ART_VERSION`; Telegram requires the ``_by_<bot username>``
  ending, and only this bot can make or change a set with that name), title ``@<channel>`` (the
  main channel's public username), owner: the owner's own Telegram account (the private chat the
  owner paired with the bot, which is the owner's user id). The pack is the owner's: @Stickers
  shows it in the owner's sets.
* Bot API ``getStickerSet``; if there is no such set, ``createNewStickerSet`` with the FIRST drawing
  only; then, a minute apart, ``addStickerToSet`` for at most :data:`ADD_PER_RUN` drawings the pack
  lacks, until it has them all (P17: creating it with all 28 at once got ``429 retry after 301``
  twice). Nothing in a pack is ever removed or replaced (D43).
* When Telegram answers 429, the next try waits exactly its ``retry_after`` (plus a few seconds), not
  a fixed hour; any other failure is tried again an hour later.
* Once the pack has every drawing, it becomes the one the operator poster uses
  (``posts.operator.emoji_pack``, proxyhub/posts/operator.py) when the owner has chosen none, or when
  the posts still use an OLDER version of this same pack. With it, the emoji ids the bot just read
  are stored as the poster's pack (``posts.operator.pack``), so no post goes out without them; the
  operator account reads the pack again at its next check. A pack the owner pasted is never
  replaced.

Changed drawings (a new ``ART_VERSION``): the new version is a new pack, built beside the old one
with the same pacing while posts keep the old one, and taken over only once it is complete. The
old pack stays in the owner's account (nothing is deleted); the owner may remove it in @Stickers.

It runs while the fleet's kill switch is OFF and the bot, the owner's chat and a main channel with a
public @username exist; once complete, it checks once a day that the pack is still there.

``settings`` row ``posts.own_pack`` (the pack being made, or made; no secret): ``{"name", "title",
"owner_id", "alts", "entries" ([custom emoji id, alt] pairs), "created_at", "checked_at",
"attempted_at", "retry_at", "error"}``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.fleet.actions import iso, log_action
from proxyhub.mtproxy import store as mtp_store
from proxyhub.posts import emoji_art, operator
from proxyhub.posts.entities import emoji_key
from proxyhub.telegram import store as tg_store
from proxyhub.telegram.client import (
    STICKER_SET_NAME_RE,
    AnimatedEmoji,
    BadRequest,
    BotClient,
    RetryAfter,
    StickerSetInfo,
    TelegramError,
)
from proxyhub.vault import SecretValue, Vault, VaultError

log = logging.getLogger(__name__)

KEY: Final = "posts.own_pack"
RECHECK: Final = timedelta(days=1)
AFTER_FAILURE: Final = timedelta(hours=1)
RETRY_MARGIN: Final = timedelta(seconds=5)
ADD_PER_RUN: Final = 3
CREATED_ACTION: Final = "posts.own_pack_created"
EXTENDED_ACTION: Final = "posts.own_pack_extended"
CHOSEN_ACTION: Final = "posts.own_pack_chosen"

BotFactory = Callable[[SecretValue], BotClient]


# --- pure parts ----------------------------------------------------------------------------------------


def pack_name(channel_username: str, bot_username: str, version: int = emoji_art.ART_VERSION) -> str | None:
    """``<channel>_by_<bot>`` for version 1, ``<channel>_v<version>_by_<bot>`` after it (lower case),
    or None when Telegram would refuse that name."""
    channel = channel_username.lstrip("@").strip("_").lower()
    bot = bot_username.lstrip("@").lower()
    name = f"{channel}_by_{bot}" if version <= 1 else f"{channel}_v{version}_by_{bot}"
    return name if channel and bot and STICKER_SET_NAME_RE.fullmatch(name) else None


def older_names(
    channel_username: str, bot_username: str, version: int = emoji_art.ART_VERSION
) -> tuple[str, ...]:
    """The names of this pack's earlier versions (newest first): only this bot can make them."""
    names = (pack_name(channel_username, bot_username, v) for v in range(version - 1, 0, -1))
    return tuple(n for n in names if n is not None)


def pack_title(channel_username: str) -> str:
    return f"@{channel_username.lstrip('@')}"


def missing(
    present: tuple[str, ...] | list[str], art: tuple[emoji_art.EmojiArt, ...] = emoji_art.ART
) -> list[emoji_art.EmojiArt]:
    """The drawings whose alt emoji the pack does not have yet, in the pack's order."""
    have = {emoji_key(a) for a in present}
    return [a for a in art if emoji_key(a.alt) not in have]


def upload(art: emoji_art.EmojiArt) -> AnimatedEmoji:
    return AnimatedEmoji(tgs=emoji_art.tgs(art), alt=art.alt)


@dataclass(frozen=True)
class PackState:
    name: str
    title: str = ""
    owner_id: int | None = None
    alts: tuple[str, ...] = ()
    created_at: datetime | None = None
    checked_at: datetime | None = None  # the last check that worked
    attempted_at: datetime | None = None  # the last try, worked or not
    error: str | None = None
    retry_at: datetime | None = None  # Telegram asked us to wait until then (429)
    entries: tuple[tuple[str, str], ...] = ()  # (custom emoji id, alt) as the bot last read them

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "owner_id": self.owner_id,
            "alts": list(self.alts),
            "entries": [list(e) for e in self.entries],
            "created_at": iso(self.created_at),
            "checked_at": iso(self.checked_at),
            "attempted_at": iso(self.attempted_at),
            "retry_at": iso(self.retry_at),
            "error": self.error,
        }


def due(state: PackState | None, name: str, now: datetime) -> bool:
    """Is a step due now? Never before Telegram's ``retry_after`` has passed; always for a new name or
    a drawing the pack lacks; else daily (an hour after a failure without ``retry_after``)."""
    if state is None or state.name != name:
        return True
    if state.retry_at is not None:
        if now < state.retry_at:
            return False
        if state.error is not None:
            return True
    if state.error is not None:
        return state.attempted_at is None or now - state.attempted_at >= AFTER_FAILURE
    if missing(state.alts):
        return True
    return state.checked_at is None or now - state.checked_at >= RECHECK


def error_code(exc: TelegramError) -> str:
    """A short code for the Content page (no token, no URL: TelegramError text is redacted)."""
    return str(exc)[:200]


def complete(state: PackState | None) -> bool:
    return state is not None and state.error is None and bool(state.alts) and not missing(state.alts)


def emoji_ids(state: PackState) -> tuple[tuple[int, str], ...] | None:
    """The pack's (document id, alt) pairs for the poster, or None unless they cover every drawing."""
    if missing([alt for _, alt in state.entries]):
        return None
    return tuple((int(i), alt) for i, alt in state.entries)


# --- state ----------------------------------------------------------------------------------------------


def _entries(value: Any) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and item[0].isascii()
            and item[0].isdigit()
            and len(item[0]) <= 20
            and isinstance(item[1], str)
            and 0 < len(item[1]) <= 16
        ):
            out.append((item[0], item[1]))
    return tuple(out)


async def load_state(db: tg_store.Db) -> PackState | None:
    value = await tg_store.get_setting(db, KEY)
    if not value or not isinstance(value.get("name"), str):
        return None
    owner = value.get("owner_id")
    alts = value.get("alts")
    return PackState(
        name=str(value["name"]),
        title=str(value.get("title") or ""),
        owner_id=owner if isinstance(owner, int) and not isinstance(owner, bool) else None,
        alts=tuple(a for a in alts if isinstance(a, str)) if isinstance(alts, list) else (),
        entries=_entries(value.get("entries")),
        created_at=operator.parse_time(value.get("created_at")),
        checked_at=operator.parse_time(value.get("checked_at")),
        attempted_at=operator.parse_time(value.get("attempted_at")),
        retry_at=operator.parse_time(value.get("retry_at")),
        error=value.get("error") if isinstance(value.get("error"), str) else None,
    )


async def save_state(db: tg_store.Db, state: PackState) -> None:
    await tg_store.put_setting(db, KEY, state.as_json())


@dataclass(frozen=True)
class Target:
    name: str
    title: str
    owner_id: int
    older: tuple[str, ...] = ()  # this pack's earlier versions (see :func:`older_names`)


async def target(db: tg_store.Db) -> Target | str:
    """The pack to make, or why not: ``kill_switch``, ``no_bot``, ``no_owner``,
    ``no_public_channel`` or ``bad_name``."""
    if await mtp_store.kill_switch_on(db):
        return "kill_switch"
    bot = await tg_store.bot_info(db)
    if bot is None:
        return "no_bot"
    owner = await tg_store.owner_chat_id(db)
    if owner is None or owner <= 0:  # a private chat's id is the user's id (positive)
        return "no_owner"
    channel = operator.public_username((await tg_store.channels(db)).get("main"))
    if channel is None:
        return "no_public_channel"
    name = pack_name(channel, bot.username)
    if name is None:
        return "bad_name"
    return Target(
        name=name, title=pack_title(channel), owner_id=owner, older=older_names(channel, bot.username)
    )


async def choose(db: tg_store.Db, found: Target, state: PackState, now: datetime) -> bool:
    """Make the COMPLETE pack ``state`` (named ``found.name``) the operator poster's pack when the
    owner has chosen none, or when the poster still uses an older version of it; then also store its
    emoji ids as the poster's pack, so posts keep their animated emoji through the switch. An older
    version is only left once the new one's ids cover every drawing. A pack the owner pasted stays."""
    config = await operator.load_config(db)
    old = config.emoji_pack
    replacing = old is not None and any(operator.same_pack(old, n) for n in found.older)
    if old is not None and not replacing:
        return False
    ids = emoji_ids(state)
    if replacing and ids is None:
        return False
    await operator.save_config(db, operator.OperatorConfig(poster=config.poster, emoji_pack=found.name))
    if ids is not None:
        # attempted_at None: the operator account reads the pack again at its next check.
        await operator.save_pack(
            db, operator.StoredPack(short_name=found.name, title=state.title, entries=ids, fetched_at=now)
        )
    await log_action(
        db,
        actor="worker",
        action=CHOSEN_ACTION,
        target=found.name,
        dry_run=False,
        allowed=True,
        context={"at": iso(now), "replaces": old},
        result="ok",
    )
    return True


# --- the one Telegram step ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Talk:
    """What one step with Telegram gave: the outcome, the set as it is now, and on failure the error
    and Telegram's ``retry_after`` (seconds) when it asked us to wait."""

    outcome: str
    info: StickerSetInfo | None = None
    error: str | None = None
    retry_after_s: int | None = None


def failed_state(prev: PackState | None, found: Target, talk: Talk, now: datetime) -> PackState:
    """The state after a failed step: what was known stays; the next try waits Telegram's
    ``retry_after`` when it gave one (P17), else :data:`AFTER_FAILURE` (see :func:`due`)."""
    retry_at = now + timedelta(seconds=talk.retry_after_s) + RETRY_MARGIN if talk.retry_after_s else None
    return PackState(
        name=found.name,
        title=prev.title if prev else found.title,
        owner_id=found.owner_id,
        alts=prev.alts if prev else (),
        entries=prev.entries if prev else (),
        created_at=prev.created_at if prev else None,
        checked_at=prev.checked_at if prev else None,
        attempted_at=now,
        error=talk.error,
        retry_at=retry_at,
    )


async def ensure(
    engine: AsyncEngine,
    vault: Vault,
    *,
    now: datetime,
    bot_factory: BotFactory = BotClient,
    force: bool = False,
) -> str:
    """One step towards the complete pack, when due. Returns the reason it cannot (see :func:`target`),
    ``vault_unavailable``, ``not_due``, ``created``, ``extended``, ``ok``, ``not_emoji_set``,
    ``rate_limited`` or ``failed``. A COMPLETE pack becomes the poster's pack (see :func:`choose`)."""
    async with engine.begin() as conn:
        found = await target(conn)
        if isinstance(found, str):
            return found
        state = await load_state(conn)
        if state is not None and state.name == found.name and complete(state):
            await choose(conn, found, state, now)
        if not force and not due(state, found.name, now):
            return "not_due"
        try:
            creds = await tg_store.load_bot(conn, vault)
        except VaultError:
            return "vault_unavailable"
    if creds is None:
        return "no_bot"
    talk = await _talk(bot_factory, creds.token, found)
    async with engine.begin() as conn:
        old = await load_state(conn)
        prev = old if old is not None and old.name == found.name else None
        if talk.error is not None:
            await save_state(conn, failed_state(prev, found, talk, now))
            log.warning("own emoji pack: %s (%s)", talk.outcome, talk.error)
            return talk.outcome
        info = talk.info
        assert info is not None
        new = PackState(
            name=found.name,
            title=info.title,
            owner_id=found.owner_id,
            alts=info.alts,
            entries=info.entries,
            created_at=now if talk.outcome == "created" else (prev.created_at if prev else None),
            checked_at=now,
            attempted_at=now,
        )
        await save_state(conn, new)
        done = complete(new)
        if talk.outcome == "created" or (talk.outcome == "extended" and done):
            await log_action(
                conn,
                actor="worker",
                action=CREATED_ACTION if talk.outcome == "created" else EXTENDED_ACTION,
                target=found.name,
                dry_run=False,
                allowed=True,
                context={"emoji": len(info.alts), "of": len(emoji_art.ART), "version": emoji_art.ART_VERSION},
                result="ok",
            )
        if done:
            await choose(conn, found, new, now)
    return talk.outcome


async def _talk(bot_factory: BotFactory, token: SecretValue, found: Target) -> Talk:
    """One step. No transaction is open while Telegram answers."""
    try:
        async with bot_factory(token) as bot:
            try:
                info: StickerSetInfo | None = await bot.get_sticker_set(found.name)
            except BadRequest as exc:
                if not exc.sticker_set_missing:
                    raise
                info = None
            if info is None:
                await bot.create_custom_emoji_set(
                    user_id=found.owner_id,
                    name=found.name,
                    title=found.title,
                    emoji=[upload(emoji_art.ART[0])],
                )
                return Talk("created", await bot.get_sticker_set(found.name))
            if not info.is_custom_emoji:
                return Talk("not_emoji_set", error="not_emoji_set")
            lacking = missing(info.alts)[:ADD_PER_RUN]
            for a in lacking:
                await bot.add_custom_emoji(user_id=found.owner_id, name=found.name, item=upload(a))
            if lacking:
                return Talk("extended", await bot.get_sticker_set(found.name))
            return Talk("ok", info)
    except RetryAfter as exc:
        return Talk("rate_limited", error=error_code(exc), retry_after_s=max(1, exc.seconds))
    except TelegramError as exc:
        return Talk("failed", error=error_code(exc))


async def status(db: tg_store.Db) -> dict[str, Any]:
    """The pack for the Content page. ``replaces``: the older version of this pack the posts still
    use while this one is being made (they move over by themselves once it is complete)."""
    state = await load_state(db)
    found = await target(db)
    config = await operator.load_config(db)
    older = found.older if isinstance(found, Target) else ()
    replaces = next((n for n in older if operator.same_pack(config.emoji_pack, n)), None)
    if state is not None and operator.same_pack(state.name, replaces):
        replaces = None  # the new version's first step has not run yet
    if state is None:
        return {
            "state": "waiting" if isinstance(found, str) else "pending",
            "reason": found if isinstance(found, str) else None,
            "name": found.name if not isinstance(found, str) else None,
            "link": None,
            "emoji": 0,
            "total": len(emoji_art.ART),
            "error": None,
            "version": emoji_art.ART_VERSION,
            "replaces": replaces,
        }
    return {
        "state": "error" if state.error else ("ready" if state.alts else "pending"),
        "reason": found if isinstance(found, str) else None,
        "name": state.name,
        "link": operator.pack_link(state.name) if state.alts else None,
        "emoji": len(state.alts),
        "total": len(emoji_art.ART),
        "error": state.error,
        "checked_at": iso(state.checked_at),
        "retry_at": iso(state.retry_at),
        "version": emoji_art.ART_VERSION,
        "replaces": replaces,
    }
