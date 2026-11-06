"""The owner's content sources (``content_sources``): checks, reads and writes.

* **kind**: ``telegram`` (a public channel's username, from ``@name``, ``name`` or a t.me link) or
  ``rss`` (an https feed URL); each kind's plugin checks and normalizes the reference
  (proxyhub/content/plugins.py). The same source cannot be added twice.
* **label**: one line of plain text, 1 to 80 characters (the plugin's default when empty).
* At most :data:`MAX_SOURCES` sources. Removing a source drops its candidates that were never
  reviewed (and their media); approved, used and rejected candidates stay, with the source's
  name copied on them.
* **The supervisor** (decision D52, proxyhub/content/supervisor.py) turns a source off by itself
  when most of its recent items were rejected (:func:`auto_disable`, with the reason kept for the
  dashboard); the owner turning it back on forgets that verdict.
* **Reading** (proxyhub/content/engine.py): a source is read every :data:`FETCH_EVERY`; after an
  error the next read waits :func:`backoff` (5 min, 10, 20 ... at most 6 h), and the error's code
  is kept for the dashboard until a read works again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.content.items import SourceInvalid
from proxyhub.content.plugins import PLUGINS
from proxyhub.db.models_content import ContentCandidate, ContentSource
from proxyhub.fleet.actions import iso

Db = AsyncConnection | AsyncSession

MAX_SOURCES: Final = 50
MAX_LABEL: Final = 80
FETCH_EVERY: Final = timedelta(minutes=5)
MAX_BACKOFF: Final = timedelta(hours=6)
_SPACES = re.compile(r"\s+")


def backoff(failures: int) -> timedelta:
    """The wait after ``failures`` errors in a row: 5 min, 10, 20, 40 ... at most 6 h."""
    minutes: int = 5 * 2 ** min(max(0, failures - 1), 10)
    return min(timedelta(minutes=minutes), MAX_BACKOFF)


def check_kind(raw: Any) -> str:
    if not isinstance(raw, str) or raw not in PLUGINS:
        raise SourceInvalid("kind_invalid")
    return raw


def check_label(raw: Any, default: str) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default[:MAX_LABEL]
    if not isinstance(raw, str):
        raise SourceInvalid("label_invalid")
    label = _SPACES.sub(" ", raw).strip()
    if not label or len(label) > MAX_LABEL or any(ord(c) < 32 for c in label):
        raise SourceInvalid("label_invalid")
    return label


@dataclass(frozen=True)
class Source:
    id: int
    kind: str
    ref: str
    label: str
    enabled: bool
    created_at: datetime
    last_fetch_at: datetime | None
    last_ok_at: datetime | None
    last_error: str | None
    failures: int
    next_fetch_at: datetime | None
    last_count: int
    disabled_reason: str | None = None
    disabled_at: datetime | None = None
    disabled_detail: dict[str, Any] | None = None
    quality_reset_at: datetime | None = None

    @property
    def link(self) -> str:
        return PLUGINS[self.kind].link(self.ref)

    def as_json(self, pending: int = 0, quality: dict[str, Any] | None = None) -> dict[str, Any]:
        from proxyhub.content.messages import error_fa, source_disabled_fa

        return {
            "id": self.id,
            "kind": self.kind,
            "ref": self.ref,
            "label": self.label,
            "link": self.link,
            "enabled": self.enabled,
            "created_at": iso(self.created_at),
            "last_fetch_at": iso(self.last_fetch_at),
            "last_ok_at": iso(self.last_ok_at),
            "last_error": self.last_error,
            "last_error_fa": error_fa(self.last_error) if self.last_error else None,
            "failures": self.failures,
            "next_fetch_at": iso(self.next_fetch_at),
            "last_count": self.last_count,
            "pending": pending,
            # D52: the supervisor's verdict on this source.
            "disabled_reason": self.disabled_reason,
            "disabled_reason_fa": source_disabled_fa(self.disabled_reason, self.disabled_detail),
            "disabled_at": iso(self.disabled_at),
            "quality": quality,
        }


_COLS = (
    ContentSource.id,
    ContentSource.kind,
    ContentSource.ref,
    ContentSource.label,
    ContentSource.enabled,
    ContentSource.created_at,
    ContentSource.last_fetch_at,
    ContentSource.last_ok_at,
    ContentSource.last_error,
    ContentSource.failures,
    ContentSource.next_fetch_at,
    ContentSource.last_count,
    ContentSource.disabled_reason,
    ContentSource.disabled_at,
    ContentSource.disabled_detail,
    ContentSource.quality_reset_at,
)


def _source(r: Any) -> Source:
    return Source(
        id=int(r.id),
        kind=r.kind,
        ref=r.ref,
        label=r.label,
        enabled=bool(r.enabled),
        created_at=r.created_at,
        last_fetch_at=r.last_fetch_at,
        last_ok_at=r.last_ok_at,
        last_error=r.last_error,
        failures=int(r.failures),
        next_fetch_at=r.next_fetch_at,
        last_count=int(r.last_count),
        disabled_reason=r.disabled_reason,
        disabled_at=r.disabled_at,
        disabled_detail=dict(r.disabled_detail) if isinstance(r.disabled_detail, dict) else None,
        quality_reset_at=r.quality_reset_at,
    )


async def get(db: Db, source_id: int) -> Source | None:
    row = (await db.execute(select(*_COLS).where(ContentSource.id == source_id))).first()
    return None if row is None else _source(row)


async def list_all(db: Db) -> list[Source]:
    rows = (await db.execute(select(*_COLS).order_by(ContentSource.id))).all()
    return [_source(r) for r in rows]


async def pending_counts(db: Db) -> dict[int, int]:
    rows = (
        await db.execute(
            select(ContentCandidate.source_id, func.count())
            .where(ContentCandidate.status == "pending", ContentCandidate.source_id.is_not(None))
            .group_by(ContentCandidate.source_id)
        )
    ).all()
    return {int(r[0]): int(r[1]) for r in rows if r[0] is not None}


async def due(db: Db, now: datetime, limit: int) -> list[Source]:
    """Enabled sources whose next read is due, the longest-waiting first."""
    rows = (
        await db.execute(
            select(*_COLS)
            .where(
                ContentSource.enabled.is_(True),
                (ContentSource.next_fetch_at.is_(None)) | (ContentSource.next_fetch_at <= now),
            )
            .order_by(ContentSource.next_fetch_at.asc().nulls_first(), ContentSource.id)
            .limit(limit)
        )
    ).all()
    return [_source(r) for r in rows]


class SourceExists(Exception):
    pass


class TooManySources(Exception):
    pass


async def create(db: Db, *, kind: Any, ref: Any, label: Any, now: datetime) -> Source:
    """A new enabled source, read at the next round. SourceInvalid, SourceExists, TooManySources."""
    kind = check_kind(kind)
    plugin = PLUGINS[kind]
    if not isinstance(ref, str):
        raise SourceInvalid("telegram_name_invalid" if kind == "telegram" else "feed_url_invalid")
    ref = plugin.check_ref(ref)
    label = check_label(label, plugin.default_label(ref))
    count = int((await db.execute(select(func.count()).select_from(ContentSource))).scalar_one())
    if count >= MAX_SOURCES:
        raise TooManySources
    exists = (
        await db.execute(select(ContentSource.id).where(ContentSource.kind == kind, ContentSource.ref == ref))
    ).first()
    if exists is not None:
        raise SourceExists
    new_id = (
        await db.execute(
            insert(ContentSource)
            .values(kind=kind, ref=ref, label=label, enabled=True, created_at=now, updated_at=now)
            .returning(ContentSource.id)
        )
    ).scalar_one()
    found = await get(db, int(new_id))
    assert found is not None
    return found


async def update_source(
    db: Db, source_id: int, *, enabled: bool | None, label: Any, now: datetime
) -> Source | None:
    current = await get(db, source_id)
    if current is None:
        return None
    values: dict[str, Any] = {"updated_at": now}
    if enabled is not None:
        values["enabled"] = enabled
        if enabled and not current.enabled:  # turned on again: read soon, errors forgotten
            values.update(next_fetch_at=None, failures=0)
            # The supervisor's verdict is forgotten too: quality is judged afresh from now (D52).
            values.update(disabled_reason=None, disabled_at=None, disabled_detail=None, quality_reset_at=now)
        elif not enabled and current.enabled:  # the owner's own choice, not the supervisor's
            values.update(disabled_reason=None, disabled_at=now, disabled_detail=None)
    if label is not None:
        values["label"] = check_label(label, PLUGINS[current.kind].default_label(current.ref))
    await db.execute(update(ContentSource).where(ContentSource.id == source_id).values(**values))
    return await get(db, source_id)


async def fetch_now(db: Db, source_id: int, now: datetime) -> Source | None:
    """Read this source at the next round (the owner's «الان بخوان»), back-off or not."""
    done = (
        await db.execute(
            update(ContentSource)
            .where(ContentSource.id == source_id)
            .values(next_fetch_at=now, updated_at=now)
            .returning(ContentSource.id)
        )
    ).first()
    return None if done is None else await get(db, source_id)


async def remove(db: Db, source_id: int) -> int | None:
    """Remove a source and its never-reviewed candidates (their media goes with them). Returns how
    many candidates were dropped, or None when there is no such source."""
    if await get(db, source_id) is None:
        return None
    dropped = (
        await db.execute(
            delete(ContentCandidate)
            .where(
                ContentCandidate.source_id == source_id, ContentCandidate.status.in_(("pending", "duplicate"))
            )
            .returning(ContentCandidate.id)
        )
    ).all()
    await db.execute(delete(ContentSource).where(ContentSource.id == source_id))
    return len(dropped)


async def auto_disable(db: Db, source_id: int, now: datetime, reason: str, detail: dict[str, Any]) -> bool:
    """The supervisor turns a source off (decision D52). Returns False when it was already off."""
    done = (
        await db.execute(
            update(ContentSource)
            .where(ContentSource.id == source_id, ContentSource.enabled.is_(True))
            .values(
                enabled=False,
                disabled_reason=reason[:40],
                disabled_at=now,
                disabled_detail=detail,
                updated_at=now,
            )
            .returning(ContentSource.id)
        )
    ).first()
    return done is not None


async def record_ok(
    db: Db, source_id: int, now: datetime, count: int, *, every: timedelta = FETCH_EVERY
) -> None:
    """A read worked. ``every``: when to read again (a Telegram source read with the operator
    account waits longer, proxyhub/content/telegram_account.py)."""
    await db.execute(
        update(ContentSource)
        .where(ContentSource.id == source_id)
        .values(
            last_fetch_at=now,
            last_ok_at=now,
            last_error=None,
            failures=0,
            next_fetch_at=now + every,
            last_count=count,
        )
    )


async def record_error(db: Db, source: Source, now: datetime, code: str) -> None:
    failures = source.failures + 1
    await db.execute(
        update(ContentSource)
        .where(ContentSource.id == source.id)
        .values(
            last_fetch_at=now,
            last_error=code[:40],
            failures=failures,
            next_fetch_at=now + backoff(failures),
        )
    )
