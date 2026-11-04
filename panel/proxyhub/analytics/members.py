"""Channel joins and leaves (decision D7) and the owner's tracking links ("از کجا آمدند").

The bot is an admin of our channels and asks Telegram for ``chat_member`` updates (Bot API: they
come only when asked for explicitly in ``allowed_updates``; finding T2). For each one about OUR main
or test channel, exactly these facts are stored (``analytics_member_events``):

* the person's numeric Telegram user id (the CRM key, D7),
* the time of the change (Telegram's ``date``),
* join or leave,
* where a join came from: the invite link's NAME (``link``), a chat folder link (``folder``), or
  anything else (``other``: the public @username, search, a forward). A leave carries the source
  of the same person's latest earlier join.

Nothing else is read into storage: not the first or last name, username, language, premium flag,
phone, the full invite link the person used, or who created it. An update about any other chat is
ignored and nothing about it is kept. Bots joining are ignored.

Tracking links: :func:`create_tracking_link` asks Telegram for a new named invite link of the MAIN
channel (``createChatInviteLink``) and stores it. Links are never revoked, edited at Telegram or
deleted here (a rename only changes the dashboard's label), so their joins stay countable for good.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_analytics import MemberEvent, TrackingLink
from proxyhub.fleet.actions import iso, log_action
from proxyhub.telegram import store
from proxyhub.telegram.alerts import BotFactory
from proxyhub.telegram.client import (
    INVITE_LINK_NAME_MAX,
    BadRequest,
    BotClient,
    Forbidden,
    TelegramError,
    Unauthorized,
    Update,
)
from proxyhub.vault import Vault

log = logging.getLogger(__name__)

Db = AsyncConnection | AsyncSession
Kind = Literal["join", "leave"]
Source = Literal["link", "folder", "other"]

MEMBER_STATUSES: Final = frozenset({"creator", "administrator", "member"})
PURPOSE_MAX: Final = 200
ACTOR: Final = "analytics"


# --- chat_member -> one join or leave --------------------------------------------------------------


@dataclass(frozen=True)
class MemberChange:
    chat_id: int
    user_id: int
    at: datetime
    kind: Kind
    source: Source
    link_name: str | None
    # Only to recognise one of our tracking links; never stored.
    link_url: str | None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_member(member: Any) -> bool:
    if not isinstance(member, dict):
        return False
    status = member.get("status")
    if status in MEMBER_STATUSES:
        return True
    return status == "restricted" and member.get("is_member") is True


def parse_member_change(raw: dict[str, Any]) -> MemberChange | None:
    """A ``chat_member`` update -> a join or a leave, or None (a promotion, a bot, a bad shape)."""
    body = raw.get("chat_member")
    if not isinstance(body, dict):
        return None
    chat = body.get("chat")
    new = body.get("new_chat_member")
    old = body.get("old_chat_member")
    if not isinstance(chat, dict) or not isinstance(new, dict) or not isinstance(old, dict):
        return None
    chat_id = _int(chat.get("id"))
    date = _int(body.get("date"))
    user = new.get("user")
    if chat_id is None or date is None or not isinstance(user, dict) or user.get("is_bot") is True:
        return None
    user_id = _int(user.get("id"))
    if user_id is None or user_id <= 0:
        return None
    was, now_member = _is_member(old), _is_member(new)
    if was == now_member:
        return None  # a promotion, a restriction change, a ban of someone not in the channel
    kind: Kind = "join" if now_member else "leave"
    source: Source = "other"
    link_name: str | None = None
    link_url: str | None = None
    link = body.get("invite_link")
    if kind == "join" and isinstance(link, dict):
        source = "link"
        name = link.get("name")
        link_name = name[:INVITE_LINK_NAME_MAX] if isinstance(name, str) and name else None
        url = link.get("invite_link")
        link_url = url if isinstance(url, str) else None
    elif kind == "join" and body.get("via_chat_folder_invite_link") is True:
        source = "folder"
    return MemberChange(
        chat_id=chat_id,
        user_id=user_id,
        at=datetime.fromtimestamp(date, UTC),
        kind=kind,
        source=source,
        link_name=link_name,
        link_url=link_url,
    )


async def _tracking_link_id(db: Db, chat_id: int, url: str | None) -> int | None:
    if not url:
        return None
    row = (
        await db.execute(
            select(TrackingLink.id).where(TrackingLink.chat_id == chat_id, TrackingLink.invite_link == url)
        )
    ).first()
    return None if row is None else int(row[0])


async def store_member_change(db: Db, role: str, change: MemberChange) -> bool:
    """Insert one event (idempotent). Returns False if the same event was already stored."""
    source: str = change.source
    link_name = change.link_name
    tracking_id: int | None = None
    if change.kind == "join":
        tracking_id = await _tracking_link_id(db, change.chat_id, change.link_url)
    else:
        joined = (
            await db.execute(
                select(MemberEvent.source, MemberEvent.link_name, MemberEvent.tracking_link_id)
                .where(
                    MemberEvent.chat_id == change.chat_id,
                    MemberEvent.telegram_user_id == change.user_id,
                    MemberEvent.kind == "join",
                    MemberEvent.at <= change.at,
                )
                .order_by(MemberEvent.at.desc(), MemberEvent.id.desc())
                .limit(1)
            )
        ).first()
        if joined is not None:
            source, link_name, tracking_id = joined.source, joined.link_name, joined.tracking_link_id
    stmt = (
        pg_insert(MemberEvent)
        .values(
            role=role,
            chat_id=change.chat_id,
            at=change.at,
            kind=change.kind,
            telegram_user_id=change.user_id,
            source=source,
            link_name=link_name,
            tracking_link_id=tracking_id,
        )
        .on_conflict_do_nothing()
        .returning(MemberEvent.id)
    )
    return (await db.execute(stmt)).first() is not None


async def record_member_update(db: Db, update: Update) -> bool:
    """Handle a ``chat_member`` update. Returns True if it was one (stored or deliberately ignored),
    False if the update is of another kind (the caller handles it)."""
    if "chat_member" not in update.raw:
        return False
    change = parse_member_change(update.raw)
    if change is None:
        return True
    channels = await store.channels(db)
    role = next((r for r, ch in channels.items() if ch.get("chat_id") == change.chat_id), None)
    if role is None:
        return True  # not one of our channels: nothing about it is kept
    await store_member_change(db, role, change)
    return True


# --- tracking links --------------------------------------------------------------------------------


class TrackingLinkError(Exception):
    """``code`` is one of proxyhub.analytics.messages.ERRORS_FA's keys."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise TrackingLinkError("invalid_link_name", 422)
    value = " ".join(name.split())
    if not 1 <= len(value) <= INVITE_LINK_NAME_MAX:
        raise TrackingLinkError("invalid_link_name", 422)
    return value


def clean_purpose(purpose: Any) -> str:
    if purpose is None:
        return ""
    if not isinstance(purpose, str):
        raise TrackingLinkError("invalid_link_name", 422)
    value = " ".join(purpose.split())
    if len(value) > PURPOSE_MAX:
        raise TrackingLinkError("invalid_link_name", 422)
    return value


@dataclass(frozen=True)
class LinkRow:
    id: int
    name: str
    purpose: str
    invite_link: str
    created_at: datetime
    renamed_at: datetime | None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "purpose": self.purpose,
            "invite_link": self.invite_link,
            "created_at": iso(self.created_at),
            "renamed_at": iso(self.renamed_at),
        }


async def _name_taken(db: Db, chat_id: int, name: str, *, except_id: int | None = None) -> bool:
    stmt = select(func.count()).where(TrackingLink.chat_id == chat_id, TrackingLink.name == name)
    if except_id is not None:
        stmt = stmt.where(TrackingLink.id != except_id)
    return int((await db.execute(stmt)).scalar_one()) > 0


async def create_tracking_link(
    db: AsyncSession,
    name: str,
    purpose: str,
    *,
    vault: Vault,
    bot_factory: BotFactory = BotClient,
    actor: str = ACTOR,
) -> LinkRow:
    """A named invite link of the main channel, made by Telegram and stored. Commits.

    No transaction is open while Telegram answers. Raises :class:`TrackingLinkError`.
    """
    name, purpose = clean_name(name), clean_purpose(purpose)
    main = (await store.channels(db)).get("main")
    chat_id = main.get("chat_id") if main else None
    if not isinstance(chat_id, int):
        raise TrackingLinkError("main_channel_not_set", 409)
    creds = await store.load_bot(db, vault)
    if creds is None:
        raise TrackingLinkError("bot_not_set", 409)
    if await _name_taken(db, chat_id, name):
        raise TrackingLinkError("link_name_taken", 409)
    await db.rollback()
    try:
        async with bot_factory(creds.token) as bot:
            link = await bot.create_chat_invite_link(chat_id, name=name)
    except (BadRequest, Forbidden):
        raise TrackingLinkError("bot_cannot_invite", 400) from None
    except Unauthorized:
        raise TrackingLinkError("bot_token_invalid", 400) from None
    except TelegramError as exc:
        log.warning("analytics: createChatInviteLink failed (%s)", exc)
        raise TrackingLinkError("telegram_unavailable", 502) from None
    try:
        row = (
            await db.execute(
                pg_insert(TrackingLink)
                .values(
                    chat_id=chat_id,
                    name=name,
                    purpose=purpose,
                    invite_link=link.invite_link,
                    telegram_name=(link.name or name)[:INVITE_LINK_NAME_MAX],
                    created_by=actor[:100],
                )
                .returning(*TrackingLink.__table__.c)
            )
        ).one()
    except IntegrityError:
        await db.rollback()
        raise TrackingLinkError("link_name_taken", 409) from None
    await log_action(
        db,
        actor=actor,
        action="analytics.tracking_link_created",
        target=name,
        dry_run=False,
        allowed=True,
        context={"tracking_link_id": int(row.id), "purpose": purpose},
    )
    await db.commit()
    return LinkRow(row.id, row.name, row.purpose, row.invite_link, row.created_at, row.renamed_at)


async def rename_tracking_link(
    db: AsyncSession, link_id: int, name: str, purpose: str, *, actor: str = ACTOR
) -> LinkRow:
    """New dashboard label and purpose (Telegram's own name of the link stays). Commits."""
    name, purpose = clean_name(name), clean_purpose(purpose)
    current = (await db.execute(select(TrackingLink.__table__).where(TrackingLink.id == link_id))).first()
    if current is None:
        raise TrackingLinkError("link_not_found", 404)
    if await _name_taken(db, current.chat_id, name, except_id=link_id):
        raise TrackingLinkError("link_name_taken", 409)
    old = current.name
    now = datetime.now(UTC)
    await db.execute(
        update(TrackingLink)
        .where(TrackingLink.id == link_id)
        .values(name=name, purpose=purpose, renamed_at=now if name != old else current.renamed_at)
    )
    await log_action(
        db,
        actor=actor,
        action="analytics.tracking_link_renamed",
        target=name,
        dry_run=False,
        allowed=True,
        context={"tracking_link_id": link_id, "old_name": old},
    )
    await db.commit()
    row = (await db.execute(select(TrackingLink.__table__).where(TrackingLink.id == link_id))).one()
    out = LinkRow(row.id, row.name, row.purpose, row.invite_link, row.created_at, row.renamed_at)
    await db.rollback()
    return out
