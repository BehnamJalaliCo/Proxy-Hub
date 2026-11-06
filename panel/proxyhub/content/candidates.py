"""The review queue (``content_candidates``, ``content_media``): storing what sources gave, the
review (the content supervisor's, decision D52, or the owner's), and what the publisher takes.

**Storing** (:func:`store_items`, from proxyhub/content/engine.py): a post of a source is stored once
(``source_id`` + ``external_id``); a later read only refreshes its view count. Posts older than
:data:`MAX_AGE` (by their own time) and posts with neither text nor media are skipped. The text is
cleaned (proxyhub/content/clean.py) and the caption prefilled from it; the content supervisor's
Claude review replaces it when it approves (proxyhub/content/supervisor.py, decision D52). A post
whose cleaned text tells what a candidate of the last :data:`MAX_AGE` already tells (any source,
:func:`~proxyhub.content.clean.similar`) is stored as ``duplicate`` of it and never shown in the
queue; the original counts it as one more source.

**Statuses**: ``pending`` (in the queue) -> ``approved`` (waits for a post) -> ``used`` (posted; set by
the publisher only). The content supervisor (decision D52, proxyhub/content/supervisor.py) approves or
rejects by itself in its ``auto`` mode (``decided_by = 'qc'``); the owner can still approve, reject
(``decided_by = 'owner'``) or move an item back to the queue.
A pending or approved item older than :data:`MAX_AGE` becomes ``expired`` (news goes stale).

**The caption** the owner edits: a title (one line, up to 120 characters; the post shows it bold,
with one emoji, proxyhub/telegram/templates.py :func:`title_line`) and a body (plain text, up to
:data:`MAX_BODY` characters; HTML is escaped). The publisher shortens the body further when the
post's links need the room (docs/04): a caption never pushes a link out.

**What the publisher takes** (:func:`next_for_post`): the oldest approved candidate whose media is
ready (or which has none, or whose media failed: then the post goes as text), before the old
content bank (proxyhub/posts/content.py).
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.content import clean
from proxyhub.content.items import FetchedItem
from proxyhub.content.media import MediaInfo
from proxyhub.content.messages import media_error_fa
from proxyhub.content.sources import Source
from proxyhub.db.models import Outbox
from proxyhub.db.models_content import CANDIDATE_STATUSES, ContentCandidate, ContentMedia
from proxyhub.db.models_posts import Post
from proxyhub.fleet.actions import iso
from proxyhub.telegram.templates import MAX_TITLE_CHARS, compose_content

Db = AsyncConnection | AsyncSession

MAX_AGE: Final = timedelta(hours=48)
MAX_BODY: Final = clean.MAX_BODY_CHARS
KEEP_ENDED: Final = timedelta(days=14)  # rows of ended candidates (no media) kept for de-duplication
OWNER_STATUSES: Final = ("pending", "approved", "rejected")
EDITABLE: Final = ("pending", "approved")
Sort = Literal["new", "views", "hot"]
_SPACES = re.compile(r"[ \t]+")


class CandidateInvalid(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_caption(title: Any, body: Any) -> tuple[str, str]:
    """The owner's (or a writer's) caption: a one-line title and a plain-text body."""
    if not isinstance(title, str) or not isinstance(body, str):
        raise CandidateInvalid("caption_invalid")
    title = _SPACES.sub(" ", title.replace("\n", " ").replace("\r", " ")).strip()
    body = clean.tidy(body)
    if len(title) > MAX_TITLE_CHARS or len(body) > MAX_BODY or not (title or body):
        raise CandidateInvalid("caption_invalid")
    if any(ord(c) < 32 and c != "\n" for c in title + body):
        raise CandidateInvalid("caption_invalid")
    return title, body


def caption_html(title: str, body: str) -> str:
    """The content part of a post: the bold title line and the body (escaped) in a quote."""
    return compose_content(title or None, html.escape(body, quote=False) if body else None)


@dataclass(frozen=True)
class MediaRow:
    id: int
    role: str
    mime: str
    size: int
    width: int | None
    height: int | None
    duration: int | None


@dataclass(frozen=True)
class Candidate:
    id: int
    source_id: int | None
    source_kind: str
    source_label: str
    url: str | None
    published_at: datetime | None
    views: int | None
    source_text: str
    title: str
    body: str
    status: str
    media_kind: str | None
    media_state: str
    media_error: str | None
    created_at: datetime
    reviewed_at: datetime | None
    used_at: datetime | None
    qc_decision: str | None = None
    qc_reasons: list[str] | None = None
    qc_flags: list[str] | None = None
    qc_checks: list[str] | None = None
    qc_category: str | None = None
    qc_error: str | None = None
    qc_backend: str | None = None
    qc_model: str | None = None
    qc_at: datetime | None = None
    qc_tries: int = 0
    decided_by: str | None = None

    def content_html(self) -> str:
        return caption_html(self.title, self.body)

    @property
    def when(self) -> datetime:
        return self.published_at or self.created_at


_COLS = (
    ContentCandidate.id,
    ContentCandidate.source_id,
    ContentCandidate.source_kind,
    ContentCandidate.source_label,
    ContentCandidate.url,
    ContentCandidate.published_at,
    ContentCandidate.views,
    ContentCandidate.source_text,
    ContentCandidate.title,
    ContentCandidate.body,
    ContentCandidate.status,
    ContentCandidate.media_kind,
    ContentCandidate.media_state,
    ContentCandidate.media_error,
    ContentCandidate.created_at,
    ContentCandidate.reviewed_at,
    ContentCandidate.used_at,
    ContentCandidate.qc_decision,
    ContentCandidate.qc_reasons,
    ContentCandidate.qc_flags,
    ContentCandidate.qc_checks,
    ContentCandidate.qc_category,
    ContentCandidate.qc_error,
    ContentCandidate.qc_backend,
    ContentCandidate.qc_model,
    ContentCandidate.qc_at,
    ContentCandidate.qc_tries,
    ContentCandidate.decided_by,
)


def _candidate(r: Any) -> Candidate:
    return Candidate(
        id=int(r.id),
        source_id=r.source_id,
        source_kind=r.source_kind,
        source_label=r.source_label,
        url=r.url,
        published_at=r.published_at,
        views=r.views,
        source_text=r.source_text,
        title=r.title,
        body=r.body,
        status=r.status,
        media_kind=r.media_kind,
        media_state=r.media_state,
        media_error=r.media_error,
        created_at=r.created_at,
        reviewed_at=r.reviewed_at,
        used_at=r.used_at,
        qc_decision=r.qc_decision,
        qc_reasons=list(r.qc_reasons) if isinstance(r.qc_reasons, list) else None,
        qc_flags=list(r.qc_flags) if isinstance(r.qc_flags, list) else None,
        qc_checks=list(r.qc_checks) if isinstance(r.qc_checks, list) else None,
        qc_category=r.qc_category,
        qc_error=r.qc_error,
        qc_backend=r.qc_backend,
        qc_model=r.qc_model,
        qc_at=r.qc_at,
        qc_tries=int(r.qc_tries),
        decided_by=r.decided_by,
    )


async def get(db: Db, candidate_id: int, *, lock: bool = False) -> Candidate | None:
    stmt = select(*_COLS).where(ContentCandidate.id == candidate_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).first()
    return None if row is None else _candidate(row)


# --- storing what a source gave ------------------------------------------------------------------------


@dataclass(frozen=True)
class Stored:
    new: int = 0
    duplicates: int = 0
    seen: int = 0
    skipped: int = 0


def _when(item: FetchedItem, now: datetime) -> datetime:
    return item.published_at or now


async def _recent_norms(db: Db, now: datetime) -> list[tuple[int, str]]:
    rows = (
        await db.execute(
            select(ContentCandidate.id, ContentCandidate.norm_text).where(
                ContentCandidate.created_at >= now - MAX_AGE,
                ContentCandidate.status != "duplicate",
                ContentCandidate.norm_text != "",
            )
        )
    ).all()
    return [(int(r.id), str(r.norm_text)) for r in rows]


async def store_items(
    db: Db,
    source: Source,
    items: list[FetchedItem],
    now: datetime,
) -> Stored:
    """Store the new posts of ``source`` (see the module doc)."""
    if not items:
        return Stored()
    ids = [i.external_id for i in items]
    known = {
        str(r.external_id): r
        for r in (
            await db.execute(
                select(ContentCandidate.id, ContentCandidate.external_id, ContentCandidate.views).where(
                    ContentCandidate.source_id == source.id, ContentCandidate.external_id.in_(ids)
                )
            )
        ).all()
    }
    norms = await _recent_norms(db, now)
    new = dups = seen = skipped = 0
    for item in items:
        if item.external_id in known:
            seen += 1
            row = known[item.external_id]
            if item.views is not None and item.views != row.views:
                await db.execute(
                    update(ContentCandidate).where(ContentCandidate.id == row.id).values(views=item.views)
                )
            continue
        if now - _when(item, now) > MAX_AGE or _when(item, now) > now + timedelta(hours=1):
            skipped += 1
            continue
        text = clean.clean_source_text(item.text, (source.ref, source.label))
        if not text and (item.media is None or (item.media.url is None and item.media.thumb_url is None)):
            skipped += 1
            continue
        norm = clean.norm_text(text)
        original = next((cid for cid, other in norms if clean.similar(norm, other)), None)
        title, body = clean.split_caption(text)  # Claude's caption replaces it on approval (QC)
        title, body = title[:MAX_TITLE_CHARS], body[:MAX_BODY]
        media = item.media
        media_state = "none"
        if media is not None:
            media_state = "waiting" if media.url else "failed"
        inserted = (
            await db.execute(
                pg_insert(ContentCandidate)
                .values(
                    source_id=source.id,
                    source_kind=source.kind,
                    source_label=source.label,
                    external_id=item.external_id,
                    url=item.url,
                    published_at=item.published_at,
                    views=item.views,
                    source_text=text,
                    norm_text=norm,
                    title=title,
                    body=body,
                    status="duplicate" if original is not None else "pending",
                    duplicate_of=original,
                    media_kind=media.kind if media else None,
                    media_url=media.url if media else None,
                    thumb_url=media.thumb_url if media else None,
                    media_state=media_state,
                    media_error="no_url" if media is not None and not media.url else None,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_content_candidates_source_external")
                .returning(ContentCandidate.id)
            )
        ).first()
        if inserted is None:
            seen += 1
            continue
        if original is not None:
            dups += 1
        else:
            new += 1
            if norm:
                norms.append((int(inserted.id), norm))
    return Stored(new=new, duplicates=dups, seen=seen, skipped=skipped)


# --- media ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaTask:
    candidate_id: int
    role: Literal["photo", "thumb", "video"]
    url: str
    source_kind: str = "rss"
    post_url: str | None = None  # the original post (a Telegram video's address is read again there)


async def media_tasks(db: Db, limit: int) -> list[MediaTask]:
    """What to download next: approved videos first (a post waits for them), then photos and video
    thumbnails of pending candidates, newest first. A candidate is tried at most 3 times."""
    out: list[MediaTask] = []
    videos = (
        await db.execute(
            select(
                ContentCandidate.id,
                ContentCandidate.media_url,
                ContentCandidate.source_kind,
                ContentCandidate.url,
            )
            .where(
                ContentCandidate.status == "approved",
                ContentCandidate.media_kind == "video",
                ContentCandidate.media_state == "waiting",
                ContentCandidate.media_url.is_not(None),
                ContentCandidate.media_tries < 3,
            )
            .order_by(ContentCandidate.reviewed_at, ContentCandidate.id)
            .limit(limit)
        )
    ).all()
    out += [MediaTask(int(r.id), "video", str(r.media_url), r.source_kind, r.url) for r in videos]
    photos = (
        await db.execute(
            select(ContentCandidate.id, ContentCandidate.media_url)
            .where(
                ContentCandidate.status.in_(("pending", "approved")),
                ContentCandidate.media_kind == "photo",
                ContentCandidate.media_state == "waiting",
                ContentCandidate.media_url.is_not(None),
                ContentCandidate.media_tries < 3,
            )
            .order_by(ContentCandidate.created_at.desc(), ContentCandidate.id.desc())
            .limit(max(0, limit - len(out)))
        )
    ).all()
    out += [MediaTask(int(r.id), "photo", str(r.media_url)) for r in photos]
    has_thumb = select(ContentMedia.id).where(
        ContentMedia.candidate_id == ContentCandidate.id, ContentMedia.role == "thumb"
    )
    thumbs = (
        await db.execute(
            select(ContentCandidate.id, ContentCandidate.thumb_url)
            .where(
                ContentCandidate.status.in_(("pending", "approved")),
                ContentCandidate.media_kind == "video",
                ContentCandidate.thumb_url.is_not(None),
                ContentCandidate.media_tries < 3,
                ~has_thumb.exists(),
            )
            .order_by(ContentCandidate.created_at.desc(), ContentCandidate.id.desc())
            .limit(max(0, limit - len(out)))
        )
    ).all()
    out += [MediaTask(int(r.id), "thumb", str(r.thumb_url)) for r in thumbs]
    return out


async def video_task(db: Db, candidate_id: int) -> MediaTask | None:
    """The download of a pending candidate's video before its review (the content supervisor, D52),
    or None when there is no address to fetch."""
    row = (
        await db.execute(
            select(
                ContentCandidate.id,
                ContentCandidate.media_url,
                ContentCandidate.source_kind,
                ContentCandidate.url,
            ).where(
                ContentCandidate.id == candidate_id,
                ContentCandidate.status == "pending",
                ContentCandidate.media_kind == "video",
                ContentCandidate.media_state == "waiting",
                ContentCandidate.media_url.is_not(None),
            )
        )
    ).first()
    return (
        None if row is None else MediaTask(int(row.id), "video", str(row.media_url), row.source_kind, row.url)
    )


async def drop_video(db: Db, candidate_id: int, now: datetime) -> bool:
    """The file of a video the content supervisor rejected goes at once (it was downloaded for the
    review only, D52). Never the file of a candidate that is approved or posted (the owner may have
    approved it meanwhile). The candidate waits for its download again, so an owner's later approval
    (``review`` mode, or moving it back) fetches it as before. A video with no file is left as it is."""
    found = (
        await db.execute(
            update(ContentCandidate)
            .where(
                ContentCandidate.id == candidate_id,
                ContentCandidate.media_kind == "video",
                ContentCandidate.media_state == "ready",
                ContentCandidate.status.in_(("pending", "rejected")),
            )
            .values(
                media_state=case((ContentCandidate.media_url.is_not(None), "waiting"), else_="failed"),
                updated_at=now,
            )
            .returning(ContentCandidate.id)
        )
    ).first()
    if found is None:
        return False
    await db.execute(
        delete(ContentMedia).where(ContentMedia.candidate_id == candidate_id, ContentMedia.role == "main")
    )
    return True


async def media_bytes_total(db: Db) -> int:
    return int((await db.execute(select(func.coalesce(func.sum(ContentMedia.size), 0)))).scalar_one())


async def save_media(db: Db, task: MediaTask, info: MediaInfo, data: bytes, now: datetime) -> int | None:
    """Store the downloaded file. A photo whose bytes another live candidate already has makes this
    candidate a duplicate of that one (the same photo from two sources). Returns the media id."""
    sha = hashlib.sha256(data).hexdigest()
    role = "thumb" if task.role == "thumb" else "main"
    if task.role == "photo":
        twin = (
            await db.execute(
                select(ContentMedia.candidate_id)
                .join(ContentCandidate, ContentCandidate.id == ContentMedia.candidate_id)
                .where(
                    ContentMedia.sha256 == sha,
                    ContentMedia.candidate_id != task.candidate_id,
                    ContentCandidate.status.in_(("pending", "approved", "used")),
                )
                .limit(1)
            )
        ).first()
        if twin is not None:
            await db.execute(
                update(ContentCandidate)
                .where(ContentCandidate.id == task.candidate_id, ContentCandidate.status == "pending")
                .values(status="duplicate", duplicate_of=int(twin.candidate_id), updated_at=now)
            )
    media_id = (
        await db.execute(
            pg_insert(ContentMedia)
            .values(
                candidate_id=task.candidate_id,
                role=role,
                mime=info.mime,
                size=len(data),
                width=info.width,
                height=info.height,
                duration=info.duration,
                sha256=sha,
                data=data,
                created_at=now,
            )
            .on_conflict_do_nothing(constraint="uq_content_media_candidate_role")
            .returning(ContentMedia.id)
        )
    ).first()
    if task.role != "thumb":
        await db.execute(
            update(ContentCandidate)
            .where(ContentCandidate.id == task.candidate_id)
            .values(media_state="ready", media_error=None, updated_at=now)
        )
    return int(media_id.id) if media_id is not None else None


async def media_failed(db: Db, task: MediaTask, code: str, now: datetime, *, final: bool) -> None:
    """A download did not work. ``final``: no retry can help (wrong type, too big); else the
    candidate is tried again next round, at most 3 times in all. A thumbnail is tried once: without
    it the queue shows no picture for the video, and the video itself is still downloaded on
    approval."""
    if task.role == "thumb":
        await db.execute(
            update(ContentCandidate)
            .where(ContentCandidate.id == task.candidate_id)
            .values(thumb_url=None, updated_at=now)
        )
        return
    values: dict[str, Any] = {
        "media_tries": ContentCandidate.media_tries + 1,
        "media_error": code[:40],
        "updated_at": now,
        "media_state": "failed"
        if final
        else case((ContentCandidate.media_tries >= 2, "failed"), else_=ContentCandidate.media_state),
    }
    await db.execute(
        update(ContentCandidate).where(ContentCandidate.id == task.candidate_id).values(**values)
    )


async def media_rows(db: Db, candidate_ids: list[int]) -> dict[int, dict[str, MediaRow]]:
    """candidate id -> role -> its stored media (without the bytes)."""
    if not candidate_ids:
        return {}
    rows = (
        await db.execute(
            select(
                ContentMedia.id,
                ContentMedia.candidate_id,
                ContentMedia.role,
                ContentMedia.mime,
                ContentMedia.size,
                ContentMedia.width,
                ContentMedia.height,
                ContentMedia.duration,
            ).where(ContentMedia.candidate_id.in_(candidate_ids))
        )
    ).all()
    out: dict[int, dict[str, MediaRow]] = {}
    for r in rows:
        out.setdefault(int(r.candidate_id), {})[r.role] = MediaRow(
            id=int(r.id),
            role=r.role,
            mime=r.mime,
            size=int(r.size),
            width=r.width,
            height=r.height,
            duration=r.duration,
        )
    return out


async def media_data(db: Db, media_id: int) -> tuple[str, bytes] | None:
    row = (
        await db.execute(select(ContentMedia.mime, ContentMedia.data).where(ContentMedia.id == media_id))
    ).first()
    return None if row is None else (str(row.mime), bytes(row.data))


# --- the owner's review -------------------------------------------------------------------------------------


def _order(sort: str, now: datetime) -> list[Any]:
    when = func.coalesce(ContentCandidate.published_at, ContentCandidate.created_at)
    if sort == "views":
        return [func.coalesce(ContentCandidate.views, -1).desc(), when.desc(), ContentCandidate.id.desc()]
    if sort == "hot":  # views per hour since the post appeared: how fast the news spreads
        hours = func.greatest(func.extract("epoch", now - when) / 3600.0, 0.25)
        return [
            (func.coalesce(ContentCandidate.views, 0) / hours).desc(),
            when.desc(),
            ContentCandidate.id.desc(),
        ]
    return [when.desc(), ContentCandidate.id.desc()]


async def queue(db: Db, status: str, sort: str, now: datetime, limit: int = 100) -> list[Candidate]:
    if status not in CANDIDATE_STATUSES or status == "duplicate":
        raise CandidateInvalid("status_invalid")
    order = (
        [ContentCandidate.reviewed_at.asc().nulls_last(), ContentCandidate.id]
        if status == "approved"
        else _order(sort, now)
    )
    rows = (
        await db.execute(
            select(*_COLS).where(ContentCandidate.status == status).order_by(*order).limit(limit)
        )
    ).all()
    return [_candidate(r) for r in rows]


async def counts(db: Db) -> dict[str, int]:
    rows = (
        await db.execute(select(ContentCandidate.status, func.count()).group_by(ContentCandidate.status))
    ).all()
    found = {str(r[0]): int(r[1]) for r in rows}
    return {s: found.get(s, 0) for s in CANDIDATE_STATUSES}


async def also_in(db: Db, candidate_ids: list[int]) -> dict[int, list[str]]:
    """The other sources that gave the same news: candidate id -> their labels."""
    if not candidate_ids:
        return {}
    rows = (
        await db.execute(
            select(ContentCandidate.duplicate_of, ContentCandidate.source_label)
            .where(ContentCandidate.duplicate_of.in_(candidate_ids))
            .order_by(ContentCandidate.id)
        )
    ).all()
    out: dict[int, list[str]] = {}
    for r in rows:
        labels = out.setdefault(int(r.duplicate_of), [])
        if r.source_label not in labels:
            labels.append(r.source_label)
    return out


async def edit(db: Db, candidate_id: int, title: Any, body: Any, now: datetime) -> Candidate | None:
    title, body = check_caption(title, body)
    current = await get(db, candidate_id, lock=True)
    if current is None:
        return None
    if current.status not in EDITABLE:
        raise CandidateInvalid("not_editable")
    await db.execute(
        update(ContentCandidate)
        .where(ContentCandidate.id == candidate_id)
        .values(title=title, body=body, updated_at=now)
    )
    return await get(db, candidate_id)


async def set_status(
    db: Db, candidate_id: int, status: str, now: datetime
) -> tuple[Candidate | None, str | None]:
    """The owner moves a candidate (pending, approved, rejected). Returns it and its old status."""
    if status not in OWNER_STATUSES:
        raise CandidateInvalid("status_invalid")
    current = await get(db, candidate_id, lock=True)
    if current is None:
        return None, None
    if current.status not in OWNER_STATUSES:
        raise CandidateInvalid("not_editable")
    if status == "approved" and not (current.title or current.body):
        raise CandidateInvalid("caption_invalid")
    if current.status != status:
        await db.execute(
            update(ContentCandidate)
            .where(ContentCandidate.id == candidate_id)
            .values(
                status=status,
                reviewed_at=now if status != "pending" else None,
                decided_by="owner" if status != "pending" else None,
                updated_at=now,
            )
        )
    return await get(db, candidate_id), current.status


# --- the publisher's side ----------------------------------------------------------------------------------


async def next_for_post(db: Db, now: datetime) -> Candidate | None:
    """The oldest approved candidate that can go now (see the module doc)."""
    when = func.coalesce(ContentCandidate.published_at, ContentCandidate.created_at)
    row = (
        await db.execute(
            select(*_COLS)
            .where(
                ContentCandidate.status == "approved",
                ContentCandidate.media_state.in_(("none", "ready", "failed")),
                when >= now - MAX_AGE,
            )
            .order_by(ContentCandidate.reviewed_at.asc().nulls_last(), ContentCandidate.id)
            .limit(1)
        )
    ).first()
    return None if row is None else _candidate(row)


async def post_media(db: Db, candidate: Candidate) -> MediaRow | None:
    """The file a post of ``candidate`` carries: its main photo or video, when it is ready."""
    if candidate.media_state != "ready":
        return None
    return (await media_rows(db, [candidate.id])).get(candidate.id, {}).get("main")


async def mark_used(db: Db, candidate_id: int, now: datetime) -> None:
    await db.execute(
        update(ContentCandidate)
        .where(ContentCandidate.id == candidate_id)
        .values(status="used", used_at=now, updated_at=now)
    )


# --- housekeeping -------------------------------------------------------------------------------------------


async def expire(db: Db, now: datetime) -> int:
    """Pending and approved candidates older than :data:`MAX_AGE` end as ``expired``."""
    when = func.coalesce(ContentCandidate.published_at, ContentCandidate.created_at)
    done = (
        await db.execute(
            update(ContentCandidate)
            .where(ContentCandidate.status.in_(("pending", "approved")), when < now - MAX_AGE)
            .values(status="expired", updated_at=now)
            .returning(ContentCandidate.id)
        )
    ).all()
    return len(done)


async def drop_media(db: Db, now: datetime, posted_keep: timedelta = timedelta(hours=1)) -> int:
    """Remove files nobody needs any more: those of ended candidates (rejected, expired, duplicate),
    and those of used candidates once their post's job finished (sent, or failed for good) and an
    hour passed: the outbox reads the file only when it sends, and a status edit changes only the
    caption."""
    still_sending = (
        select(Post.content_candidate_id)
        .join(Outbox, Outbox.id == Post.outbox_id)
        .where(Outbox.done_at.is_(None), Post.content_candidate_id.is_not(None))
    )
    ended = select(ContentCandidate.id).where(
        or_(
            ContentCandidate.status.in_(("rejected", "expired", "duplicate")),
            and_(
                ContentCandidate.status == "used",
                ContentCandidate.used_at < now - posted_keep,
                ContentCandidate.id.not_in(still_sending),
            ),
        )
    )
    done = (
        await db.execute(
            delete(ContentMedia).where(ContentMedia.candidate_id.in_(ended)).returning(ContentMedia.id)
        )
    ).all()
    return len(done)


async def prune(db: Db, now: datetime) -> int:
    """Rows of candidates that ended more than :data:`KEEP_ENDED` ago (their media is gone already)."""
    done = (
        await db.execute(
            delete(ContentCandidate)
            .where(
                ContentCandidate.status.in_(("rejected", "expired", "duplicate", "used")),
                ContentCandidate.updated_at < now - KEEP_ENDED,
            )
            .returning(ContentCandidate.id)
        )
    ).all()
    return len(done)


def qc_json(c: Candidate) -> dict[str, Any] | None:
    """The content supervisor's review of ``c`` for the dashboard (decision D52), or None."""
    from proxyhub.content.messages import qc_check_fa, qc_outcome_fa

    if c.qc_at is None and c.qc_error is None and not c.qc_tries:
        return None
    return {
        "decision": c.qc_decision,
        "reasons": c.qc_reasons or [],
        "category": c.qc_category,
        "flags": c.qc_flags or [],
        "checks": c.qc_checks or [],
        "checks_fa": [qc_check_fa(code) for code in c.qc_checks or []],
        "error": c.qc_error,
        "error_fa": qc_outcome_fa(c.qc_error) if c.qc_error else None,
        "backend": c.qc_backend,
        "model": c.qc_model,
        "at": iso(c.qc_at),
        "tries": c.qc_tries,
    }


def as_json(
    c: Candidate,
    media: dict[str, MediaRow] | None = None,
    also: list[str] | None = None,
) -> dict[str, Any]:
    """A candidate for the dashboard. The source's media URLs are never included: the dashboard
    shows the panel's own copy (``/api/content/media/<id>``)."""
    media = media or {}
    main = media.get("main")
    thumb = media.get("thumb")
    preview = main if main is not None and main.mime != "video/mp4" else thumb
    shown = main or thumb
    return {
        "id": c.id,
        "status": c.status,
        "source": {"id": c.source_id, "kind": c.source_kind, "label": c.source_label},
        "url": c.url,
        "published_at": iso(c.published_at),
        "created_at": iso(c.created_at),
        "reviewed_at": iso(c.reviewed_at),
        "used_at": iso(c.used_at),
        "views": c.views,
        "title": c.title,
        "body": c.body,
        "source_text": c.source_text,
        "also_in": also or [],
        "decided_by": c.decided_by,
        "qc": qc_json(c),
        "media": None
        if c.media_kind is None
        else {
            "kind": c.media_kind,
            "state": c.media_state,
            "error": c.media_error,
            "error_fa": media_error_fa(c.media_error),
            "preview_id": preview.id if preview is not None else None,
            "width": shown.width if shown is not None else None,
            "height": shown.height if shown is not None else None,
            "duration": main.duration if main is not None else None,
            "size": main.size if main is not None else None,
        },
    }
