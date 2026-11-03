"""Database access for the Telegram part. Nothing here logs, and nothing returns the bot token
except :func:`load_bot`, whose token stays a :class:`~proxyhub.vault.SecretValue`.

Non-secret state lives in ``settings`` under these keys:

=====================  ==================================================================
``telegram.bot``       ``{"id", "username", "set_at"}`` of the bot whose token is in the vault
``telegram.owner``     ``{"chat_id", "paired_at"}``: the owner's private chat (not a secret)
``telegram.channels``  ``{"main"?: Channel, "test"?: Channel}`` with the bot's rights in each
``telegram.posting``   ``{"quiet_windows": [{"start","end"}], "post_interval_min"}`` (D21)
``telegram.updates``   ``{"bot_id", "offset", "last_poll_at", "error"}`` of the getUpdates loop
``telegram.health``    ``{"error", "at"}``: last bot-wide problem (e.g. the token was revoked)
``telegram.last_alert``  ``{"at"}``: last message delivered to the owner's chat
=====================  ==================================================================

Outbox convention (table ``outbox``, shared): a job is finished when ``done_at`` is set; it
*failed* if ``last_error`` is not NULL at that point (a successful job has ``last_error`` NULL).
While a send is in flight, ``last_error`` is the marker ``'sending'`` (see proxyhub/telegram/outbox.py).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models import Outbox, Setting
from proxyhub.db.models_telegram import ChannelPost, TelegramPacing, TelegramPairing
from proxyhub.fleet.actions import iso
from proxyhub.telegram import pairing
from proxyhub.telegram.schedule import QuietWindow
from proxyhub.vault import SecretValue, Vault, secret_exists

Db = AsyncConnection | AsyncSession

BOT_SECRET: Final = "telegram:bot"  # noqa: S105 - the vault NAME of the token, not a token
KEY_BOT: Final = "telegram.bot"
KEY_OWNER: Final = "telegram.owner"
KEY_CHANNELS: Final = "telegram.channels"
KEY_POSTING: Final = "telegram.posting"
KEY_UPDATES: Final = "telegram.updates"
KEY_HEALTH: Final = "telegram.health"
KEY_LAST_ALERT: Final = "telegram.last_alert"
ROLES: Final = ("main", "test")
SENDING: Final = "sending"
DEFAULT_POST_INTERVAL_MIN: Final = 60
DEFAULT_CHANNEL_TAG: Final = "@ipoxxo"  # decision D23


def now_utc() -> datetime:
    return datetime.now(UTC)


def text_sha256(text_value: str) -> str:
    return hashlib.sha256(text_value.encode()).hexdigest()


# --- settings rows -------------------------------------------------------------------------------


async def get_setting(db: Db, key: str) -> dict[str, Any] | None:
    row = (await db.execute(select(Setting.value).where(Setting.key == key))).first()
    if row is None or not isinstance(row[0], dict):
        return None
    return dict(row[0])


async def put_setting(db: Db, key: str, value: dict[str, Any]) -> None:
    stmt = pg_insert(Setting).values(key=key, value=value)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value, "updated_at": func.now()}
        )
    )


async def drop_setting(db: Db, key: str) -> None:
    await db.execute(text("DELETE FROM settings WHERE key = :k"), {"k": key})


# --- bot -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BotInfo:
    id: int
    username: str


@dataclass(frozen=True)
class BotCredentials:
    info: BotInfo
    token: SecretValue

    def __repr__(self) -> str:
        return f"BotCredentials(info={self.info!r}, token=***)"


async def bot_info(db: Db) -> BotInfo | None:
    """The configured bot, if both its settings row and its vault secret exist."""
    value = await get_setting(db, KEY_BOT)
    if value is None or not await secret_exists(db, BOT_SECRET):
        return None
    try:
        return BotInfo(id=int(value["id"]), username=str(value["username"]))
    except (KeyError, TypeError, ValueError):
        return None


async def load_bot(db: Db, vault: Vault) -> BotCredentials | None:
    info = await bot_info(db)
    if info is None:
        return None
    token = await vault.get(db, BOT_SECRET)
    return None if token is None else BotCredentials(info=info, token=token)


async def save_bot(db: Db, vault: Vault, *, info: BotInfo, token: SecretValue, now: datetime) -> bool:
    """Store the token in the vault and the bot's identity in settings. Returns True if this is a
    different bot than before; then the owner pairing and the channels are cleared (the new bot
    must be started by the owner and made admin again before it can reach them)."""
    previous = await get_setting(db, KEY_BOT)
    changed = previous is None or previous.get("id") != info.id
    await vault.put(db, BOT_SECRET, token)
    await put_setting(db, KEY_BOT, {"id": info.id, "username": info.username, "set_at": iso(now)})
    await drop_setting(db, KEY_HEALTH)
    if changed:
        await drop_setting(db, KEY_OWNER)
        await drop_setting(db, KEY_CHANNELS)
        await drop_setting(db, KEY_UPDATES)
        await cancel_open_pairings(db, now)
    return changed


async def set_health(db: Db, error: str | None, now: datetime) -> None:
    if error is None:
        await drop_setting(db, KEY_HEALTH)
    else:
        await put_setting(db, KEY_HEALTH, {"error": error, "at": iso(now)})


# --- owner chat ----------------------------------------------------------------------------------


async def owner_chat_id(db: Db) -> int | None:
    value = await get_setting(db, KEY_OWNER)
    if value is None:
        return None
    chat_id = value.get("chat_id")
    return chat_id if isinstance(chat_id, int) and not isinstance(chat_id, bool) else None


async def set_owner_chat(db: Db, chat_id: int, now: datetime) -> None:
    await put_setting(db, KEY_OWNER, {"chat_id": chat_id, "paired_at": iso(now)})


async def mark_alert_delivered(db: Db, now: datetime) -> None:
    await put_setting(db, KEY_LAST_ALERT, {"at": iso(now)})


# --- pairing -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenPairing:
    id: int
    expires_at: datetime


async def cancel_open_pairings(db: Db, now: datetime) -> None:
    await db.execute(
        update(TelegramPairing)
        .where(TelegramPairing.used_at.is_(None), TelegramPairing.cancelled_at.is_(None))
        .values(cancelled_at=now)
    )


async def create_pairing(db: Db, code: str, now: datetime) -> OpenPairing:
    """A new single-use code; every older open code is cancelled."""
    await cancel_open_pairings(db, now)
    expires = now + pairing.TTL
    row = (
        await db.execute(
            insert(TelegramPairing)
            .values(code_hash=pairing.code_hash(code), created_at=now, expires_at=expires)
            .returning(TelegramPairing.id)
        )
    ).scalar_one()
    return OpenPairing(id=int(row), expires_at=expires)


async def open_pairing(db: Db, now: datetime) -> OpenPairing | None:
    row = (
        await db.execute(
            select(TelegramPairing.id, TelegramPairing.expires_at)
            .where(
                TelegramPairing.used_at.is_(None),
                TelegramPairing.cancelled_at.is_(None),
                TelegramPairing.expires_at > now,
            )
            .order_by(TelegramPairing.id.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else OpenPairing(id=int(row.id), expires_at=row.expires_at)


async def consume_pairing(db: Db, code: str, now: datetime) -> bool:
    """Use the code if it is open and unexpired (atomic, single use)."""
    row = (
        await db.execute(
            update(TelegramPairing)
            .where(
                TelegramPairing.code_hash == pairing.code_hash(code),
                TelegramPairing.used_at.is_(None),
                TelegramPairing.cancelled_at.is_(None),
                TelegramPairing.expires_at > now,
            )
            .values(used_at=now)
            .returning(TelegramPairing.id)
        )
    ).first()
    return row is not None


async def count_wrong_code(db: Db, now: datetime) -> bool:
    """One more wrong ``/start`` code against the open pairing. Returns True if this cancelled it."""
    current = await open_pairing(db, now)
    if current is None:
        return False
    wrong = (
        await db.execute(
            update(TelegramPairing)
            .where(TelegramPairing.id == current.id)
            .values(wrong_codes=TelegramPairing.wrong_codes + 1)
            .returning(TelegramPairing.wrong_codes)
        )
    ).scalar_one()
    if int(wrong) >= pairing.MAX_WRONG_CODES:
        await db.execute(
            update(TelegramPairing).where(TelegramPairing.id == current.id).values(cancelled_at=now)
        )
        return True
    return False


# --- channels ------------------------------------------------------------------------------------


async def channels(db: Db) -> dict[str, dict[str, Any]]:
    value = await get_setting(db, KEY_CHANNELS) or {}
    return {role: dict(value[role]) for role in ROLES if isinstance(value.get(role), dict)}


async def save_channel(db: Db, role: str, channel: dict[str, Any]) -> None:
    current = await channels(db)
    current[role] = channel
    await put_setting(db, KEY_CHANNELS, current)


def public_channel(channel: dict[str, Any] | None) -> dict[str, Any] | None:
    """The contract's Channel object (plus ``chat_id``, documented) from a stored channel."""
    if channel is None:
        return None
    return {
        "username": str(channel.get("username", "")),
        "title": str(channel.get("title", "")),
        "can_post": bool(channel.get("can_post")),
        "can_edit": bool(channel.get("can_edit")),
        "can_delete": bool(channel.get("can_delete")),
        "checked_at": channel.get("checked_at"),
        "chat_id": channel.get("chat_id"),
    }


# --- posting settings ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PostingSettings:
    quiet_windows: list[QuietWindow]
    post_interval_min: int

    def as_json(self) -> dict[str, Any]:
        return {
            "quiet_windows": [w.as_json() for w in self.quiet_windows],
            "post_interval_min": self.post_interval_min,
        }


DEFAULT_POSTING = PostingSettings(quiet_windows=[], post_interval_min=DEFAULT_POST_INTERVAL_MIN)


async def posting_settings(db: Db) -> PostingSettings:
    value = await get_setting(db, KEY_POSTING)
    if value is None:
        return DEFAULT_POSTING
    try:
        windows = [QuietWindow.parse(w["start"], w["end"]) for w in value.get("quiet_windows", [])]
        return PostingSettings(quiet_windows=windows, post_interval_min=int(value["post_interval_min"]))
    except (KeyError, TypeError, ValueError):
        return DEFAULT_POSTING  # a stored value that no longer validates: defaults (still paced)


async def save_posting_settings(db: Db, s: PostingSettings) -> None:
    await put_setting(db, KEY_POSTING, s.as_json())


async def last_main_post_at(db: Db) -> datetime | None:
    value = (
        await db.execute(select(func.max(ChannelPost.created_at)).where(ChannelPost.role == "main"))
    ).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


# --- outbox --------------------------------------------------------------------------------------


async def enqueue(db: Db, kind: str, payload: dict[str, Any], *, not_before: datetime | None = None) -> int:
    values: dict[str, Any] = {"kind": kind, "payload": payload}
    if not_before is not None:
        values["not_before"] = not_before
    return int((await db.execute(insert(Outbox).values(**values).returning(Outbox.id))).scalar_one())


async def outbox_counts(db: Db) -> tuple[int, int]:
    pending, failed = (
        await db.execute(
            text(
                "SELECT count(*) FILTER (WHERE done_at IS NULL), "
                "count(*) FILTER (WHERE done_at IS NOT NULL AND last_error IS NOT NULL) FROM outbox"
            )
        )
    ).one()
    return int(pending), int(failed)


# --- pacing --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pacing:
    last_sent_at: datetime | None
    retry_until: datetime | None


async def pacing(db: Db, chat_id: int) -> Pacing:
    row = (
        await db.execute(
            select(TelegramPacing.last_sent_at, TelegramPacing.retry_until).where(
                TelegramPacing.chat_id == chat_id
            )
        )
    ).first()
    return Pacing(None, None) if row is None else Pacing(row.last_sent_at, row.retry_until)


async def record_sent(db: Db, chat_id: int, at: datetime) -> None:
    stmt = pg_insert(TelegramPacing).values(chat_id=chat_id, last_sent_at=at)
    await db.execute(
        stmt.on_conflict_do_update(index_elements=[TelegramPacing.chat_id], set_={"last_sent_at": at})
    )


async def record_retry_after(db: Db, chat_id: int, until: datetime) -> None:
    stmt = pg_insert(TelegramPacing).values(chat_id=chat_id, retry_until=until)
    await db.execute(
        stmt.on_conflict_do_update(index_elements=[TelegramPacing.chat_id], set_={"retry_until": until})
    )
