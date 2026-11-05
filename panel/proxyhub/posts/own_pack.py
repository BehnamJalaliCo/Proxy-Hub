"""The channel's own animated emoji pack (D38): the bot makes it once, the operator poster uses it.

The owner asked for an emoji pack about Iran's internet and operators. No public pack fits, and a
pack made by someone else shows ITS title (often another channel's ad) when a reader taps one of
its emoji. So the bot makes our own pack from the drawings in proxyhub/posts/emoji_art.py:

* name ``<channel>_by_<bot>`` (Telegram requires the ``_by_<bot username>`` ending; only this bot can
  make or change a set with that name), title ``@<channel>`` (the main channel's public username),
  owner: the owner's own Telegram account (the private chat the owner paired with the bot, which
  is the owner's user id). The pack is the owner's: @Stickers shows it in the owner's sets.
* Bot API ``getStickerSet``; if there is no such set, ``createNewStickerSet`` with every drawing in
  one request; if there is, ``addStickerToSet`` for each drawing whose alt emoji it lacks (a drawing
  added to this repository later). Nothing in a pack is ever removed or replaced.
* When the owner has not chosen a pack (``posts.operator.emoji_pack`` empty), this pack becomes the
  one the operator poster uses (proxyhub/posts/operator.py, :func:`~proxyhub.posts.operator.emoji_map`).
  A pack the owner pasted is never replaced.

It runs while the fleet's kill switch is OFF and the bot, the owner's chat and a main channel with a
public @username exist; once a day it checks the pack is still there (an hour after a failure).

``settings`` row ``posts.own_pack``: ``{"name", "title", "owner_id", "alts", "created_at",
"checked_at", "attempted_at", "error"}`` (no secret).
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
    StickerSetInfo,
    TelegramError,
)
from proxyhub.vault import SecretValue, Vault, VaultError

log = logging.getLogger(__name__)

KEY: Final = "posts.own_pack"
RECHECK: Final = timedelta(days=1)
AFTER_FAILURE: Final = timedelta(hours=1)
CREATED_ACTION: Final = "posts.own_pack_created"
EXTENDED_ACTION: Final = "posts.own_pack_extended"
CHOSEN_ACTION: Final = "posts.own_pack_chosen"

BotFactory = Callable[[SecretValue], BotClient]


# --- pure parts ----------------------------------------------------------------------------------------


def pack_name(channel_username: str, bot_username: str) -> str | None:
    """``<channel>_by_<bot>`` (lower case), or None when Telegram would refuse that name."""
    channel = channel_username.lstrip("@").strip("_").lower()
    bot = bot_username.lstrip("@").lower()
    name = f"{channel}_by_{bot}"
    return name if channel and bot and STICKER_SET_NAME_RE.fullmatch(name) else None


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

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "owner_id": self.owner_id,
            "alts": list(self.alts),
            "created_at": iso(self.created_at),
            "checked_at": iso(self.checked_at),
            "attempted_at": iso(self.attempted_at),
            "error": self.error,
        }


def due(state: PackState | None, name: str, now: datetime) -> bool:
    """Is a check due now? Always for a new name or a drawing the pack lacks; else daily (hourly
    after a failure)."""
    if state is None or state.name != name:
        return True
    if state.error is not None:
        return state.attempted_at is None or now - state.attempted_at >= AFTER_FAILURE
    if missing(state.alts):
        return True
    return state.checked_at is None or now - state.checked_at >= RECHECK


def error_code(exc: TelegramError) -> str:
    """A short code for the Content page (no token, no URL: TelegramError text is redacted)."""
    return str(exc)[:200]


# --- state ----------------------------------------------------------------------------------------------


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
        created_at=operator.parse_time(value.get("created_at")),
        checked_at=operator.parse_time(value.get("checked_at")),
        attempted_at=operator.parse_time(value.get("attempted_at")),
        error=value.get("error") if isinstance(value.get("error"), str) else None,
    )


async def save_state(db: tg_store.Db, state: PackState) -> None:
    await tg_store.put_setting(db, KEY, state.as_json())


@dataclass(frozen=True)
class Target:
    name: str
    title: str
    owner_id: int


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
    return Target(name=name, title=pack_title(channel), owner_id=owner)


async def choose_if_unset(db: tg_store.Db, name: str, now: datetime) -> bool:
    """Make ``name`` the operator poster's pack when the owner has not chosen one."""
    config = await operator.load_config(db)
    if config.emoji_pack is not None:
        return False
    await operator.save_config(db, operator.OperatorConfig(poster=config.poster, emoji_pack=name))
    await log_action(
        db,
        actor="worker",
        action=CHOSEN_ACTION,
        target=name,
        dry_run=False,
        allowed=True,
        context={"at": iso(now)},
        result="ok",
    )
    return True


# --- the one Telegram step ---------------------------------------------------------------------------------


async def ensure(
    engine: AsyncEngine,
    vault: Vault,
    *,
    now: datetime,
    bot_factory: BotFactory = BotClient,
    force: bool = False,
) -> str:
    """Make or complete the pack when due. Returns the reason it cannot (see :func:`target`),
    ``vault_unavailable``, ``not_due``, ``created``, ``extended``, ``ok``, ``not_emoji_set`` or
    ``failed``. A pack that is ready becomes the poster's pack if the owner chose none."""
    async with engine.begin() as conn:
        found = await target(conn)
        if isinstance(found, str):
            return found
        state = await load_state(conn)
        if state is not None and state.name == found.name and state.error is None and state.alts:
            await choose_if_unset(conn, found.name, now)
        if not force and not due(state, found.name, now):
            return "not_due"
        try:
            creds = await tg_store.load_bot(conn, vault)
        except VaultError:
            return "vault_unavailable"
    if creds is None:
        return "no_bot"
    outcome, info, error = await _talk(bot_factory, creds.token, found)
    async with engine.begin() as conn:
        old = await load_state(conn)
        prev = old if old is not None and old.name == found.name else None
        if error is not None:
            await save_state(
                conn,
                PackState(
                    name=found.name,
                    title=prev.title if prev else found.title,
                    owner_id=found.owner_id,
                    alts=prev.alts if prev else (),
                    created_at=prev.created_at if prev else None,
                    checked_at=prev.checked_at if prev else None,
                    attempted_at=now,
                    error=error,
                ),
            )
            log.warning("own emoji pack: %s (%s)", outcome, error)
            return outcome
        assert info is not None
        await save_state(
            conn,
            PackState(
                name=found.name,
                title=info.title,
                owner_id=found.owner_id,
                alts=info.alts,
                created_at=now if outcome == "created" else (prev.created_at if prev else None),
                checked_at=now,
                attempted_at=now,
            ),
        )
        if outcome in ("created", "extended"):
            await log_action(
                conn,
                actor="worker",
                action=CREATED_ACTION if outcome == "created" else EXTENDED_ACTION,
                target=found.name,
                dry_run=False,
                allowed=True,
                context={"emoji": len(info.alts)},
                result="ok",
            )
        await choose_if_unset(conn, found.name, now)
    return outcome


async def _talk(
    bot_factory: BotFactory, token: SecretValue, found: Target
) -> tuple[str, StickerSetInfo | None, str | None]:
    """(outcome, the set as it is now, error). No transaction is open while Telegram answers."""
    try:
        async with bot_factory(token) as bot:
            try:
                info: StickerSetInfo | None = await bot.get_sticker_set(found.name)
            except BadRequest as exc:
                if not exc.sticker_set_missing:
                    raise
                info = None
            if info is None:
                art = [upload(a) for a in emoji_art.ART]
                await bot.create_custom_emoji_set(
                    user_id=found.owner_id, name=found.name, title=found.title, emoji=art
                )
                return "created", await bot.get_sticker_set(found.name), None
            if not info.is_custom_emoji:
                return "not_emoji_set", None, "not_emoji_set"
            lacking = missing(info.alts)
            for a in lacking:
                await bot.add_custom_emoji(user_id=found.owner_id, name=found.name, item=upload(a))
            if lacking:
                return "extended", await bot.get_sticker_set(found.name), None
            return "ok", info, None
    except TelegramError as exc:
        return "failed", None, error_code(exc)


async def status(db: tg_store.Db) -> dict[str, Any]:
    """The pack for the Content page."""
    state = await load_state(db)
    found = await target(db)
    if state is None:
        return {
            "state": "waiting" if isinstance(found, str) else "pending",
            "reason": found if isinstance(found, str) else None,
            "name": found.name if not isinstance(found, str) else None,
            "link": None,
            "emoji": 0,
            "total": len(emoji_art.ART),
            "error": None,
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
    }
