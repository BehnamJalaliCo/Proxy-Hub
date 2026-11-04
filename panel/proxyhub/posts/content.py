"""The content library (``content_items``): checks, reads and writes.

* **Title**: one line of plain text, 1 to 120 characters (HTML is escaped when posted).
* **Body**: Telegram HTML, rebuilt with the body tags only (``BODY_TAGS``: bold, italic, underline,
  strike, spoiler, inline code, http(s)/tg links); at most 1500 visible characters. Line breaks
  stay. A body is refused (not cut) when it is too long.
* **Tags**: up to 8 one-word tags (letters, digits, ``_``, ``-``, ZWNJ), 1 to 32 characters.
* **Status**: ``draft`` -> ``approved`` -> ``used`` (set by the publisher only) and ``retired``. The
  owner may move an item to draft, approved or retired from any status. Nothing is ever removed.

Which item goes next: approved items the owner wrote first, then the seeded ones, oldest first.
When none is left, the ``used`` item used longest ago comes again, but only after
``recycle_after_days``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_posts import CONTENT_STATUSES, ContentItem
from proxyhub.fleet.actions import iso
from proxyhub.telegram.templates import (
    MAX_TITLE_CHARS,
    compose_content,
    sanitize_body,
    utf16_len,
    visible_text,
)

Db = AsyncConnection | AsyncSession

MAX_BODY_VISIBLE: Final = 1500
MAX_BODY_HTML: Final = 3000
MAX_TAGS: Final = 8
MAX_TAG_CHARS: Final = 32
OWNER_STATUSES: Final = ("draft", "approved", "retired")
_NO_LIMIT: Final = 1_000_000
_TAG_RE = re.compile(r"^[\w‌-]{1,32}$")
_SPACES = re.compile(r"\s+")


class ContentInvalid(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ContentIn:
    title: str
    body: str
    tags: list[str]


def check_title(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ContentInvalid("title_invalid")
    title = _SPACES.sub(" ", raw).strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        raise ContentInvalid("title_invalid")
    return title


def check_body(raw: Any) -> str:
    """The body rebuilt with the allowed tags; refused if empty or too long."""
    if not isinstance(raw, str) or len(raw) > 4 * MAX_BODY_HTML:
        raise ContentInvalid("body_invalid")
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    # Rebuilt without any budget (nothing is cut here), then measured: too long is refused.
    body = sanitize_body(text, max_html=_NO_LIMIT, max_visible=_NO_LIMIT).strip()
    shown = visible_text(body)
    if not shown.strip() or utf16_len(shown) > MAX_BODY_VISIBLE or len(body) > MAX_BODY_HTML:
        raise ContentInvalid("body_invalid")
    return body


def check_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TAGS:
        raise ContentInvalid("tags_invalid")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ContentInvalid("tags_invalid")
        tag = item.strip().lstrip("#")
        tag = tag.lower() if tag.isascii() else tag
        if not _TAG_RE.fullmatch(tag):
            raise ContentInvalid("tags_invalid")
        if tag not in out:
            out.append(tag)
    return out


def check_input(title: Any, body: Any, tags: Any) -> ContentIn:
    return ContentIn(title=check_title(title), body=check_body(body), tags=check_tags(tags))


# --- reads ---------------------------------------------------------------------------------------

_COLS = (
    ContentItem.id,
    ContentItem.title,
    ContentItem.body,
    ContentItem.tags,
    ContentItem.status,
    ContentItem.origin,
    ContentItem.used_count,
    ContentItem.last_used_at,
    ContentItem.created_at,
    ContentItem.updated_at,
)


@dataclass(frozen=True)
class Item:
    id: int
    title: str
    body: str
    tags: list[str]
    status: str
    origin: str
    used_count: int
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def content_html(self) -> str:
        return compose_content(self.title, self.body)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "tags": list(self.tags),
            "status": self.status,
            "origin": self.origin,
            "used_count": self.used_count,
            "last_used_at": iso(self.last_used_at) if self.last_used_at else None,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


def _item(r: Any) -> Item:
    return Item(
        id=int(r.id),
        title=r.title,
        body=r.body,
        tags=list(r.tags or []),
        status=r.status,
        origin=r.origin,
        used_count=int(r.used_count),
        last_used_at=r.last_used_at,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


async def get_item(db: Db, item_id: int, *, lock: bool = False) -> Item | None:
    stmt = select(*_COLS).where(ContentItem.id == item_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).first()
    return None if row is None else _item(row)


async def list_items(db: Db, status: str | None = None, limit: int = 500) -> list[Item]:
    stmt = select(*_COLS)
    if status is not None:
        stmt = stmt.where(ContentItem.status == status)
    stmt = stmt.order_by(_queue_order(), ContentItem.id.desc()).limit(limit)
    return [_item(r) for r in (await db.execute(stmt)).all()]


def _queue_order() -> Any:
    """Approved first (in the order they will be posted), then drafts, used, retired."""
    return case(
        (ContentItem.status == "approved", 0),
        (ContentItem.status == "draft", 1),
        (ContentItem.status == "used", 2),
        else_=3,
    )


async def counts(db: Db) -> dict[str, int]:
    rows = (await db.execute(select(ContentItem.status, func.count()).group_by(ContentItem.status))).all()
    found = {str(r[0]): int(r[1]) for r in rows}
    return {s: found.get(s, 0) for s in CONTENT_STATUSES}


async def approved_queue(db: Db, limit: int = 1) -> list[Item]:
    """The approved items in posting order: the owner's own first, then the seeded ones."""
    stmt = (
        select(*_COLS)
        .where(ContentItem.status == "approved")
        .order_by(case((ContentItem.origin == "owner", 0), else_=1), ContentItem.id)
        .limit(limit)
    )
    return [_item(r) for r in (await db.execute(stmt)).all()]


async def next_item(db: Db, now: datetime, recycle_after_days: int) -> tuple[Item | None, bool]:
    """The item for the next post and whether it is a recycled (already used) one."""
    fresh = await approved_queue(db, 1)
    if fresh:
        return fresh[0], False
    row = (
        await db.execute(
            select(*_COLS)
            .where(
                ContentItem.status == "used",
                ContentItem.last_used_at.is_not(None),
                ContentItem.last_used_at <= now - timedelta(days=recycle_after_days),
            )
            .order_by(ContentItem.last_used_at, ContentItem.used_count, ContentItem.id)
            .limit(1)
        )
    ).first()
    return (None, False) if row is None else (_item(row), True)


# --- writes --------------------------------------------------------------------------------------


async def create_item(db: Db, data: ContentIn, status: str, now: datetime) -> Item:
    if status not in ("draft", "approved"):
        raise ContentInvalid("status_invalid")
    item_id = (
        await db.execute(
            insert(ContentItem)
            .values(
                title=data.title,
                body=data.body,
                tags=data.tags,
                status=status,
                origin="owner",
                created_at=now,
                updated_at=now,
            )
            .returning(ContentItem.id)
        )
    ).scalar_one()
    item = await get_item(db, int(item_id))
    assert item is not None
    return item


async def update_item(db: Db, item_id: int, data: ContentIn, now: datetime) -> Item | None:
    done = (
        await db.execute(
            update(ContentItem)
            .where(ContentItem.id == item_id)
            .values(title=data.title, body=data.body, tags=data.tags, updated_at=now)
            .returning(ContentItem.id)
        )
    ).first()
    return None if done is None else await get_item(db, item_id)


async def set_status(db: Db, item_id: int, status: str, now: datetime) -> tuple[Item | None, str | None]:
    """The owner moves an item. Returns the item and its previous status."""
    if status not in OWNER_STATUSES:
        raise ContentInvalid("status_invalid")
    current = await get_item(db, item_id, lock=True)
    if current is None:
        return None, None
    if current.status != status:
        await db.execute(
            update(ContentItem).where(ContentItem.id == item_id).values(status=status, updated_at=now)
        )
    return await get_item(db, item_id), current.status


async def mark_used(db: Db, item_id: int, now: datetime) -> None:
    await db.execute(
        update(ContentItem)
        .where(ContentItem.id == item_id)
        .values(status="used", used_count=ContentItem.used_count + 1, last_used_at=now, updated_at=now)
    )
