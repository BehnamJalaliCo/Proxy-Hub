"""Breaking news (decision D56): a fresh story several sources carry goes out at once.

The owner (2026-11-07): news like a war escalation or the unveiling of a new iPhone should go out
"لحظه‌ای", even at night, if it can be done reliably; a missed or late item is acceptable, a wrong
one much less. So code decides what is young and widely carried, Claude decides what matters, and
every gate errs towards NOT posting:

1. **Young**: the story's first post (the candidate's own or a duplicate's, by their own times) is
   at most ``breaking_max_age_min`` old (:func:`first_seen`).
2. **Carried by several sources**: at least ``breaking_min_sources`` different sources gave it
   (the candidate's own and its duplicates', proxyhub/content/candidates.py; one channel posting it
   twice counts once). A story only one channel tells is never breaking: this is the main guard
   against rumours.
3. **Claude** approves it AND says ``breaking`` (proxyhub/content/qc_prompt.py: a major event a large
   share of the audience wants now; never analysis, a minor update, or an unconfirmed claim).
4. **Budget**: at most ``breaking_per_day`` breaking posts in any 24 hours, ``breaking_gap_minutes``
   between two, and (:data:`MIN_AFTER_ANY`) a little room after any post.

How it runs:

* The supervisor (proxyhub/content/supervisor.py) reviews a young, widely carried candidate at once,
  out of the posting slots (:func:`stories`, at most :data:`REVIEWS_PER_HOUR` and
  :data:`REVIEWS_PER_DAY` such reviews), and after ANY review where Claude said ``breaking`` it
  checks the gates (:func:`promote`); a story whose second source arrived after its review is
  promoted then, with no new review.
* The publisher (proxyhub/posts/publisher.py :func:`~proxyhub.posts.publisher.publish_breaking`)
  posts a promoted candidate within :data:`POST_WITHIN` of its promotion, out of the day plan and in
  no-post hours too (the owner's choice), with the title ``🔴 فوری | ...``. It does not count
  against ``posts_per_day``. Past that time, or over the budget, it stays an ordinary approved item
  for the next slot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.content import candidates
from proxyhub.content.qc_settings import QcConfig
from proxyhub.db.models_content import ContentCandidate, ContentSource
from proxyhub.db.models_posts import Post
from proxyhub.fleet.actions import iso
from proxyhub.posts.entities import emoji_in
from proxyhub.telegram.templates import clean_title

POST_WITHIN: Final = timedelta(minutes=60)
MIN_AFTER_ANY: Final = timedelta(minutes=5)
REVIEWS_PER_HOUR: Final = 4
REVIEWS_PER_DAY: Final = 24
MARK: Final = "🔴"
WORD: Final = "فوری"
STATE_KEY: Final = "breaking_reviews"  # in the supervisor's state: the times of breaking reviews
POOL: Final = 100


def _when() -> Any:
    return func.coalesce(ContentCandidate.published_at, ContentCandidate.created_at)


@dataclass(frozen=True)
class Story:
    candidate_id: int
    first_seen: datetime
    sources: int


async def story_of(db: AsyncConnection, candidate_ids: Sequence[int]) -> dict[int, Story]:
    """For each candidate: when its story was first posted and by how many different sources
    (its own and its duplicates')."""
    if not candidate_ids:
        return {}
    own = (
        await db.execute(
            select(ContentCandidate.id, ContentCandidate.source_id, _when().label("when")).where(
                ContentCandidate.id.in_(candidate_ids)
            )
        )
    ).all()
    dups = (
        await db.execute(
            select(ContentCandidate.duplicate_of, ContentCandidate.source_id, _when().label("when")).where(
                ContentCandidate.duplicate_of.in_(candidate_ids)
            )
        )
    ).all()
    seen: dict[int, tuple[datetime, set[int]]] = {}
    for r in own:
        seen[int(r.id)] = (r.when, {int(r.source_id)} if r.source_id is not None else set())
    for r in dups:
        first, ids = seen.get(int(r.duplicate_of), (r.when, set()))
        if r.source_id is not None:
            ids.add(int(r.source_id))
        seen[int(r.duplicate_of)] = (min(first, r.when), ids)
    return {cid: Story(cid, first, len(ids)) for cid, (first, ids) in seen.items()}


def qualifies(story: Story | None, config: QcConfig, now: datetime) -> bool:
    """Gates 1 and 2: young and carried by enough different sources."""
    return (
        story is not None
        and now - story.first_seen <= timedelta(minutes=config.breaking_max_age_min)
        and story.first_seen <= now + timedelta(minutes=5)
        and story.sources >= config.breaking_min_sources
    )


async def stories(db: AsyncConnection, config: QcConfig, now: datetime) -> list[int]:
    """Pending, never reviewed candidates of enabled sources whose story passes gates 1 and 2: the
    ones the supervisor reviews at once, youngest story first."""
    since = now - timedelta(minutes=config.breaking_max_age_min) - timedelta(hours=1)
    rows = (
        await db.execute(
            select(ContentCandidate.id)
            .join(ContentSource, ContentSource.id == ContentCandidate.source_id)
            .where(
                ContentSource.enabled.is_(True),
                ContentCandidate.status == "pending",
                ContentCandidate.qc_at.is_(None),
                ContentCandidate.created_at >= since,
            )
            .order_by(ContentCandidate.created_at.desc())
            .limit(POOL)
        )
    ).all()
    found = await story_of(db, [int(r.id) for r in rows])
    good = [s for s in found.values() if qualifies(s, config, now)]
    return [s.candidate_id for s in sorted(good, key=lambda s: (-s.sources, now - s.first_seen))]


def _times(state: dict[str, Any], now: datetime) -> list[datetime]:
    out: list[datetime] = []
    for value in state.get(STATE_KEY) or []:
        try:
            at = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            at = None
        if at is not None and now - at < timedelta(hours=24):
            out.append(at)
    return out


def review_allowed(state: dict[str, Any], now: datetime) -> bool:
    """At most :data:`REVIEWS_PER_HOUR` and :data:`REVIEWS_PER_DAY` out-of-slot reviews."""
    times = _times(state, now)
    return len(times) < REVIEWS_PER_DAY and sum(1 for t in times if now - t < timedelta(hours=1)) < (
        REVIEWS_PER_HOUR
    )


def note_review(state: dict[str, Any], now: datetime) -> None:
    state[STATE_KEY] = [iso(t) for t in _times(state, now)] + [iso(now)]


def enabled(config: QcConfig) -> bool:
    return config.mode == "auto" and config.breaking_enabled and config.breaking_per_day > 0


async def posted_since(db: AsyncConnection, since: datetime) -> list[datetime]:
    rows = (
        await db.execute(select(Post.created_at).where(Post.breaking.is_(True), Post.created_at >= since))
    ).all()
    return [r.created_at for r in rows]


async def budget_left(db: AsyncConnection, config: QcConfig, now: datetime) -> str | None:
    """Gate 4: None when a breaking post may go now, else why not."""
    if not enabled(config):
        return "off"
    made = await posted_since(db, now - timedelta(hours=24))
    if len(made) >= config.breaking_per_day:
        return "day_full"
    if made and now - max(made) < timedelta(minutes=config.breaking_gap_minutes):
        return "gap"
    last = (await db.execute(select(func.max(Post.created_at)))).scalar_one_or_none()
    if isinstance(last, datetime) and now - last < MIN_AFTER_ANY:
        return "just_posted"
    return None


async def promote(db: AsyncConnection, config: QcConfig, now: datetime) -> list[int]:
    """Mark as breaking (``breaking_at``) the approved, unused candidates Claude called breaking whose
    story passes gates 1 and 2 now. Returns their ids."""
    if not enabled(config):
        return []
    since = now - timedelta(minutes=config.breaking_max_age_min) - POST_WITHIN
    rows = (
        await db.execute(
            select(ContentCandidate.id).where(
                ContentCandidate.status == "approved",
                ContentCandidate.qc_breaking.is_(True),
                ContentCandidate.breaking_at.is_(None),
                ContentCandidate.created_at >= since,
            )
        )
    ).all()
    found = await story_of(db, [int(r.id) for r in rows])
    ids = [cid for cid, story in found.items() if qualifies(story, config, now)]
    if ids:
        await db.execute(
            update(ContentCandidate)
            .where(ContentCandidate.id.in_(ids), ContentCandidate.breaking_at.is_(None))
            .values(breaking_at=now, updated_at=now)
        )
    return ids


async def next_to_post(db: AsyncConnection, now: datetime) -> candidates.Candidate | None:
    """The promoted candidate to post now: approved, unused, promoted within :data:`POST_WITHIN`,
    its file ready (or none, or failed: then it goes as text). The earliest promoted first."""
    row = (
        await db.execute(
            select(ContentCandidate.id)
            .where(
                ContentCandidate.status == "approved",
                ContentCandidate.breaking_at.is_not(None),
                ContentCandidate.breaking_at >= now - POST_WITHIN,
                ContentCandidate.media_state.in_(("none", "ready", "failed")),
            )
            .order_by(ContentCandidate.breaking_at, ContentCandidate.id)
            .limit(1)
        )
    ).first()
    return None if row is None else await candidates.get(db, int(row.id))


def title(text: str) -> str:
    """The breaking post's title: ``🔴 فوری | `` and the title without its own leading emoji."""
    try:
        clean = clean_title(text)
    except ValueError:
        return f"{MARK} {WORD}"
    first = next(emoji_in(clean), None)
    if first is not None and first.offset == 0:
        clean = clean[len(first.emoji) :].strip()
    return f"{MARK} {WORD} | {clean}" if clean else f"{MARK} {WORD}"
