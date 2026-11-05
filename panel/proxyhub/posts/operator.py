"""The operator poster's state (decision D38): who posts, the owner's emoji pack, the account's rights.

The owner may let the SECOND Telegram account (the operator account of D37, now with Telegram
Premium) post in our channels instead of the bot, so posts can carry animated custom emoji. Posting
itself is proxyhub/posts/operator_outbox.py; reading the pack and the account's rights is
proxyhub/posts/operator_checks.py. This module holds what both read, and the publisher's question
"can the operator post this NEW post now?" (:func:`unavailable`). No Telegram I/O here, and nothing
here is a secret.

``settings`` rows:

=========================  ==========================================================================
``posts.operator``         the owner's choice: ``{"poster": "bot" | "operator", "emoji_pack": name}``;
                           the bot until the owner switches (``emoji_pack``: the short name of the
                           pack link ``https://t.me/addemoji/<name>``, or null)
``posts.operator.pack``    the pack as Telegram last returned it: short name, title, ``entries``
                           (``[document id as a string, alt emoji]``), when, and the last error
``posts.operator.rights``  the account's own rights in each configured channel (role -> chat id,
                           status, can_post, can_edit, can_delete) and when they were read
``posts.operator.hold``    ``{"until"}``: Telegram asked the account to wait (FLOOD_WAIT)
=========================  ==========================================================================

Fail closed: the operator posts a NEW post only while the fleet's kill switch is OFF, the account is
connected, the channel has a public @username (the account finds our channels by name only: it may
not list its chats), Telegram has not asked it to wait, and its last rights check (if any) found the
post right. Otherwise the publisher queues the post for the bot and records why
(:func:`log_fallback`, shown in Persian on the Content page). An edit always goes through the poster
that sent the post: a bot cannot edit the account's message, nor the other way round.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import select

from proxyhub.db.models import ActionLog
from proxyhub.fleet.actions import iso, log_action
from proxyhub.mtproxy import state as mtp_state
from proxyhub.mtproxy import store as mtp_store
from proxyhub.posts import entities
from proxyhub.posts.messages import FALLBACK_FA, FALLBACK_OTHER_FA
from proxyhub.telegram import store as tg_store

KEY: Final = "posts.operator"
PACK_KEY: Final = "posts.operator.pack"
RIGHTS_KEY: Final = "posts.operator.rights"
HOLD_KEY: Final = "posts.operator.hold"

# The operator account's own outbox jobs (proxyhub/posts/operator_outbox.py); same payloads as the
# bot's ``send_post`` / ``edit_post``, so a post the operator cannot send is handed to the bot as is.
SEND: Final = "operator_send_post"
EDIT: Final = "operator_edit_post"
KINDS: Final = (SEND, EDIT)

POSTERS: Final = ("bot", "operator")
FALLBACK_ACTION: Final = "posts.poster_fallback"
MAX_FALLBACKS: Final = 20
MAX_HOLD: Final = timedelta(days=1)

PosterName = Literal["bot", "operator"]


# --- the owner's choice ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorConfig:
    poster: PosterName = "bot"
    emoji_pack: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"poster": self.poster, "emoji_pack": self.emoji_pack}


DEFAULT_CONFIG: Final = OperatorConfig()
_SHORT_NAME = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_PACK_LINK = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?P<kind>addemoji|addstickers)/(?P<a>[^/?#\s]+)/?"
    r"|tg://(?P<tkind>addemoji|addstickers)\?set=(?P<b>[^&#\s]+))$",
    re.IGNORECASE,
)


class PackLinkInvalid(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_pack_link(value: str) -> str:
    """``https://t.me/addemoji/<name>`` (also without https://, or tg://addemoji?set=<name>) -> the
    pack's short name. Raises :class:`PackLinkInvalid` (``pack_link_invalid``, or
    ``pack_link_stickers`` for a sticker pack's link)."""
    m = _PACK_LINK.fullmatch(value.strip())
    if m is None:
        raise PackLinkInvalid("pack_link_invalid")
    if (m.group("kind") or m.group("tkind") or "").lower() == "addstickers":
        raise PackLinkInvalid("pack_link_stickers")
    name = m.group("a") or m.group("b") or ""
    if not _SHORT_NAME.fullmatch(name):
        raise PackLinkInvalid("pack_link_invalid")
    return name


def pack_link(short_name: str | None) -> str | None:
    return f"https://t.me/addemoji/{short_name}" if short_name else None


def _config_from(value: dict[str, Any] | None) -> OperatorConfig:
    if not value:
        return DEFAULT_CONFIG
    poster = value.get("poster")
    pack = value.get("emoji_pack")
    return OperatorConfig(
        poster="operator" if poster == "operator" else "bot",
        emoji_pack=pack if isinstance(pack, str) and _SHORT_NAME.fullmatch(pack) else None,
    )


async def load_config(db: tg_store.Db) -> OperatorConfig:
    """The owner's choice; anything unreadable is the bot without a pack (fail closed)."""
    return _config_from(await tg_store.get_setting(db, KEY))


async def save_config(db: tg_store.Db, config: OperatorConfig) -> None:
    await tg_store.put_setting(db, KEY, config.as_json())


# --- the pack as Telegram returned it ----------------------------------------------------------------------


@dataclass(frozen=True)
class StoredPack:
    short_name: str
    title: str = ""
    entries: tuple[tuple[int, str], ...] = ()
    fetched_at: datetime | None = None  # the last read that worked
    attempted_at: datetime | None = None  # the last try, worked or not
    error: str | None = None
    rpc: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "short_name": self.short_name,
            "title": self.title,
            "entries": [[str(i), alt] for i, alt in self.entries],
            "fetched_at": iso(self.fetched_at),
            "attempted_at": iso(self.attempted_at),
            "error": self.error,
            "rpc": self.rpc,
        }


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _entries(value: Any) -> tuple[tuple[int, str], ...]:
    out: list[tuple[int, str]] = []
    for item in value if isinstance(value, list) else []:
        if (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and item[0].isascii()
            and item[0].lstrip("-").isdigit()
            and isinstance(item[1], str)
            and 0 < len(item[1]) <= 16
        ):
            out.append((int(item[0]), item[1]))
    return tuple(out)


async def load_pack(db: tg_store.Db) -> StoredPack | None:
    value = await tg_store.get_setting(db, PACK_KEY)
    if not value or not isinstance(value.get("short_name"), str):
        return None
    return StoredPack(
        short_name=str(value["short_name"]),
        title=str(value.get("title") or "")[:128],
        entries=_entries(value.get("entries")),
        fetched_at=parse_time(value.get("fetched_at")),
        attempted_at=parse_time(value.get("attempted_at")),
        error=value.get("error") if isinstance(value.get("error"), str) else None,
        rpc=value.get("rpc") if isinstance(value.get("rpc"), str) else None,
    )


async def save_pack(db: tg_store.Db, pack: StoredPack) -> None:
    await tg_store.put_setting(db, PACK_KEY, pack.as_json())


def same_pack(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.lower() == b.lower()


async def emoji_map(db: tg_store.Db) -> dict[str, int]:
    """alt emoji (without variation selectors) -> document id, from the pack the owner configured
    NOW (a pack read for an earlier link is never used)."""
    config = await load_config(db)
    pack = await load_pack(db)
    if pack is None or not same_pack(pack.short_name, config.emoji_pack):
        return {}
    return entities.pack_map(list(pack.entries))


# --- the account's rights in our channels ------------------------------------------------------------------


async def load_rights(db: tg_store.Db) -> dict[str, Any]:
    return await tg_store.get_setting(db, RIGHTS_KEY) or {}


async def save_rights(db: tg_store.Db, value: dict[str, Any]) -> None:
    await tg_store.put_setting(db, RIGHTS_KEY, value)


def rights_for(rights: dict[str, Any], role: str, chat_id: int | None) -> dict[str, Any] | None:
    """The stored rights of ``role``, only if they were read for the channel configured now."""
    found = (rights.get("channels") or {}).get(role)
    if isinstance(found, dict) and found.get("chat_id") == chat_id:
        return found
    return None


# --- Telegram asked the account to wait --------------------------------------------------------------------


async def hold_until(db: tg_store.Db) -> datetime | None:
    value = await tg_store.get_setting(db, HOLD_KEY) or {}
    return parse_time(value.get("until"))


async def set_hold(db: tg_store.Db, until: datetime, now: datetime) -> None:
    await tg_store.put_setting(db, HOLD_KEY, {"until": iso(min(until, now + MAX_HOLD)), "at": iso(now)})


# --- can the operator post now? -----------------------------------------------------------------------------


def public_username(channel: dict[str, Any] | None) -> str | None:
    """A channel's public username without the @ (lower case), or None for a private channel."""
    username = str((channel or {}).get("username") or "")
    name = username[1:] if username.startswith("@") else ""
    return name.lower() if _SHORT_NAME.fullmatch(name) else None


async def account_ready(db: tg_store.Db) -> str | None:
    """None while the account may work, else why not: ``kill_switch`` (an unreadable switch counts
    as ON) or ``not_connected``."""
    if await mtp_store.kill_switch_on(db):
        return "kill_switch"
    row = await mtp_store.load_row(db)
    if row.state != mtp_state.CONNECTED or not await mtp_store.has_session(db):
        return "not_connected"
    return None


async def unavailable(db: tg_store.Db, *, role: str, now: datetime) -> str | None:
    """Why the operator cannot take a NEW post for the channel of ``role`` now, or None."""
    reason = await account_ready(db)
    if reason is not None:
        return reason
    channel = (await tg_store.channels(db)).get(role)
    if public_username(channel) is None:
        return "no_username"
    until = await hold_until(db)
    if until is not None and until > now:
        return "hold"
    checked = rights_for(await load_rights(db), role, (channel or {}).get("chat_id"))
    if checked is not None and checked.get("can_post") is False:
        return "no_post_right"
    return None


# --- the fallback log (Content page) -----------------------------------------------------------------------


def fallback_text(reason: str, rpc: str | None = None) -> str:
    text = FALLBACK_FA.get(reason)
    if text is None:
        return FALLBACK_OTHER_FA.format(code=reason)
    return text.format(rpc=rpc or "?")


async def log_fallback(
    db: tg_store.Db,
    *,
    actor: str,
    role: str,
    post_id: int | None,
    reason: str,
    stage: str,
    outbox_id: int,
    rpc: str | None = None,
) -> None:
    """One actions_log line: this NEW post went with the bot instead of the operator account.
    ``stage``: ``queue`` (the publisher chose the bot) or ``send`` (the operator could not send it
    and handed it to the bot before anything went out)."""
    context: dict[str, Any] = {"stage": stage, "outbox_id": outbox_id}
    if rpc is not None:
        context["telegram_error"] = rpc
    await log_action(
        db,
        actor=actor,
        action=FALLBACK_ACTION,
        target=f"{role}:post:{post_id}" if post_id is not None else f"{role}:outbox:{outbox_id}",
        dry_run=False,
        allowed=True,
        reasons=[reason],
        context=context,
    )


async def recent_fallbacks(db: tg_store.Db, limit: int = MAX_FALLBACKS) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ActionLog.at, ActionLog.target, ActionLog.reasons)
            .where(ActionLog.action == FALLBACK_ACTION)
            .order_by(ActionLog.id.desc())
            .limit(limit)
        )
    ).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        stored = r.reasons if isinstance(r.reasons, dict) else {}
        reasons = stored.get("reasons") or ["?"]
        context = stored.get("context") or {}
        reason = str(reasons[0])
        rpc = context.get("telegram_error") if isinstance(context.get("telegram_error"), str) else None
        role = str(r.target or "").split(":", 1)[0]
        out.append(
            {
                "at": iso(r.at),
                "role": role if role in tg_store.ROLES else None,
                "reason": reason,
                "stage": context.get("stage") if context.get("stage") in ("queue", "send") else None,
                "text_fa": fallback_text(reason, rpc),
            }
        )
    return out


# --- the Content page's view --------------------------------------------------------------------------------

MAX_PACK_SHOWN: Final = 200


def warnings(found: dict[str, Any] | None) -> list[str]:
    """What the owner should fix about the account's rights in one channel: a missing post or edit
    right, or a delete right it must not have."""
    if found is None or found.get("status") not in ("admin", "creator"):
        return []
    out: list[str] = []
    if found.get("can_post") is not True:
        out.append("no_post_right")
    if found.get("can_edit") is not True:
        out.append("no_edit_right")
    if found.get("can_delete") is True:
        out.append("delete_right")
    return out


def _pack_json(pack: StoredPack | None) -> dict[str, Any] | None:
    if pack is None:
        return None
    shown: dict[str, dict[str, str]] = {}
    for document_id, alt in pack.entries:
        shown.setdefault(entities.emoji_key(alt), {"emoji": alt, "id": str(document_id)})
    return {
        "short_name": pack.short_name,
        "title": pack.title,
        "count": len(pack.entries),
        "fetched_at": iso(pack.fetched_at),
        "attempted_at": iso(pack.attempted_at),
        "error": pack.error,
        "telegram_error": pack.rpc,
        "emoji": list(shown.values())[:MAX_PACK_SHOWN],
    }


_RIGHT_FIELDS: Final = ("status", "can_post", "can_edit", "can_delete", "error", "telegram_error")


async def view(db: tg_store.Db, now: datetime) -> dict[str, Any]:
    """Everything the Content page shows about the operator poster. No secret, no phone."""
    config = await load_config(db)
    pack = await load_pack(db)
    rights = await load_rights(db)
    channels = await tg_store.channels(db)
    row = await mtp_store.load_row(db)
    until = await hold_until(db)
    shown: dict[str, Any] = {}
    for role in tg_store.ROLES:
        channel = channels.get(role)
        if channel is None:
            continue
        found = rights_for(rights, role, channel.get("chat_id"))
        shown[role] = {
            "username": channel.get("username"),
            "public": public_username(channel) is not None,
            "ready": await unavailable(db, role=role, now=now),
            "rights": {k: found.get(k) for k in _RIGHT_FIELDS} if found is not None else None,
            "warnings": warnings(found),
        }
    checked_at = rights.get("checked_at")
    return {
        "poster": config.poster,
        "emoji_pack": config.emoji_pack,
        "pack_link": pack_link(config.emoji_pack),
        "account": {
            "state": row.state,
            "first_name": row.first_name,
            "kill_switch": await mtp_store.kill_switch_on(db),
        },
        "hold_until": iso(until) if until is not None and until > now else None,
        "channels": shown,
        "rights_checked_at": checked_at if isinstance(checked_at, str) else None,
        "pack": _pack_json(
            pack if pack is not None and same_pack(pack.short_name, config.emoji_pack) else None
        ),
        "fallbacks": await recent_fallbacks(db),
    }
