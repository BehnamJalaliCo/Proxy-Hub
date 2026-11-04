"""The publisher's two steps, each run in the caller's transaction (proxyhub/reconcilers/publisher.py).

**New posts** (:func:`publish_due`): when a slot of today's plan is due (proxyhub/posts/plan.py),
the post is the next content item + the live links of every provider + the channel tag, queued for
the TEST channel, or for the MAIN channel only when the owner turned ``main_enabled`` on. Checks,
in order: publisher on, a bot, the target channel set, a due slot not yet posted, the day not full,
not inside a no-post window (D21), at least ``post_interval_min`` since the last post (both
channels), and no earlier post still waiting in the outbox. **A post never goes without at least
one live link** (docs/04): then nothing is queued, the slot stays due until its grace ends, and the
owner hears it at most once a day (not while the fleet's kill switch is on: nothing can run then).

**Status edits** (:func:`refresh_posts`): every sent post younger than ``edit_window_hours`` is
rendered again from what it carries: a link the provider now REPORTS expired/full/burned (or whose
own expiry passed) gets its status mark and loses its link; a live link takes its provider's current
URL, so a new active domain (proxyhub.cloudflare.rotation) or a node's slot name replaces the old
host. When the text differs from the one the channel shows (the last edit that finished without an
error, else ``channel_posts.text_sha256``) an ``edit_post`` job is queued, through the poster that
sent the post. Bounds: one edit job per post at a time, :data:`EDIT_MIN_GAP` between two edits of a
post, :data:`MAX_EDITS_PER_RUN` per step and :data:`MAX_PENDING_EDITS` waiting at once; the outbox
paces every message per channel (3 s, 429). A failed edit is retried with backoff and given up after
:data:`MAX_EDIT_FAILURES` (owner alerted). A link's status in a post never goes back to active, so
only a REPORTED status (or the link's own expiry) ends a link in a post.

**A link that is merely missing is not dead.** Both providers keep an ended link listed with its
status (for days, longer than the edit window), but leave a live one out for a while when its node
restarts Xray, reports an error or its secret cannot be read. Such a link is remembered as missing
(``post_links.missing_since``) and the post is left alone for :data:`MISSING_GRACE`; after that the
post shows it as paused (label and a pause mark, no link) so the post's other changes can go out,
and it gets its link back as soon as a provider lists it again. Nothing is edited when a provider
failed, none is registered, or one returned a link we refused. Posts are never deleted.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.db.models import Outbox
from proxyhub.db.models_posts import Post, PostLink
from proxyhub.db.models_telegram import ChannelPost
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings
from proxyhub.links.contract import ProxyLink
from proxyhub.posts import content, links, plan, settings
from proxyhub.posts.messages import (
    CONTENT_RECYCLED_ALERT_FA,
    EDITS_GIVEN_UP_FA,
    FALLBACK_TITLE_FA,
    NO_CONTENT_ALERT_FA,
    NO_LINKS_ALERT_FA,
)
from proxyhub.posts.poster import BOT, POSTERS, Poster
from proxyhub.telegram import store as tg_store
from proxyhub.telegram.schedule import quiet_until
from proxyhub.telegram.templates import (
    LinkKind,
    PostTooLong,
    RenderedPost,
    ShownStatus,
    StatusTexts,
    channel_tag,
    compose_content,
    render_post,
)
from proxyhub.telegram.templates import PostLink as LinkView

log = logging.getLogger(__name__)

ACTOR: Final = "publisher"
EDIT_MIN_GAP: Final = timedelta(minutes=10)
MAX_EDITS_PER_RUN: Final = 5
MAX_PENDING_EDITS: Final = 20
MAX_EDIT_FAILURES: Final = 5
# How long a link may be missing from every provider before a post shows it as paused.
MISSING_GRACE: Final = timedelta(minutes=30)
NO_LINKS_REPEAT: Final = timedelta(days=1)
NO_CONTENT_REPEAT: Final = timedelta(days=1)
RECYCLED_REPEAT: Final = timedelta(days=7)

HarvestFn = Callable[[], Awaitable[links.Harvest]]


def channel_tag_for(channels: Mapping[str, Mapping[str, Any]]) -> str:
    """The MAIN channel's @name under every post (also in the test channel); @ipoxxo if unknown."""
    main = channels.get("main") or {}
    username = main.get("username")
    if isinstance(username, str) and username.startswith("@"):
        try:
            return channel_tag(username)
        except ValueError:
            pass
    return tg_store.DEFAULT_CHANNEL_TAG


def views_for_new_post(chosen: Sequence[ProxyLink]) -> list[LinkView]:
    return [
        LinkView(kind=x.kind, label=x.label, status="active", url=x.url, expires_at=x.expires_at)
        for x in chosen
    ]


def fit(
    content_html: str, chosen: Sequence[ProxyLink], tag: str, texts: StatusTexts
) -> tuple[RenderedPost, list[ProxyLink]]:
    """Render; if even the links do not fit one message, drop links from the end (Reality first)."""
    kept = list(chosen)
    while True:
        try:
            return render_post(content_html, views_for_new_post(kept), tag, texts), kept
        except PostTooLong:
            if not kept:
                raise
            kept.pop()


def content_for(item: content.Item | None) -> str:
    return item.content_html() if item is not None else compose_content(FALLBACK_TITLE_FA, None)


# --- small reads ---------------------------------------------------------------------------------


async def slot_taken(db: AsyncConnection, role: str, slot: plan.Slot) -> bool:
    row = (
        await db.execute(
            select(Post.id).where(Post.role == role, Post.plan_day == slot.day, Post.plan_slot == slot.index)
        )
    ).first()
    return row is not None


async def posts_on(db: AsyncConnection, role: str, day: date) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(Post).where(Post.role == role, Post.plan_day == day)
            )
        ).scalar_one()
    )


async def last_post_at(db: AsyncConnection) -> datetime | None:
    value = (await db.execute(select(func.max(Post.created_at)))).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


async def choose_links(
    db: AsyncConnection, h: links.Harvest, now: datetime, cfg: settings.PostingConfig
) -> list[ProxyLink]:
    """The links of the next post; the slots turn by one with every post made so far."""
    made = int((await db.execute(select(func.count()).select_from(Post))).scalar_one())
    return links.for_new_post(h, now, max_mtproto=cfg.max_mtproto, max_reality=cfg.max_reality, rotate=made)


async def post_waiting(db: AsyncConnection) -> bool:
    """An earlier post is still in the outbox (e.g. the bot is paused): queue nothing more."""
    row = (
        await db.execute(
            select(Post.id).join(Outbox, Outbox.id == Post.outbox_id).where(Outbox.done_at.is_(None)).limit(1)
        )
    ).first()
    return row is not None


async def _kill_switch_on(db: AsyncConnection) -> bool:
    try:
        return (await load_fleet_settings(db)).kill_switch
    except SettingsCorrupt:
        return True


async def _alert_once(
    db: AsyncConnection, now: datetime, state_field: str, repeat: timedelta, text_html: str
) -> bool:
    state = await settings.load_state(db)
    last = state.get(state_field)
    try:
        if isinstance(last, str) and now - datetime.fromisoformat(last) < repeat:
            return False
    except ValueError:
        pass
    await tg_store.enqueue_owner_alert(db, text_html)
    await settings.save_state(db, {**state, state_field: iso(now)})
    return True


# --- new posts -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    role: str
    channel: dict[str, Any]
    tag: str


async def target(db: AsyncConnection, cfg: settings.PostingConfig) -> Target | str:
    """Where the next post goes, or why there is nowhere to post."""
    if not cfg.enabled or cfg.posts_per_day == 0:
        return "disabled"
    if await tg_store.bot_info(db) is None:
        return "no_bot"
    channels = await tg_store.channels(db)
    channel = channels.get(cfg.target_role)
    if channel is None or not isinstance(channel.get("chat_id"), int):
        return "no_channel"
    return Target(role=cfg.target_role, channel=channel, tag=channel_tag_for(channels))


async def publish_due(db: AsyncConnection, now: datetime, harvest: HarvestFn, poster: Poster = BOT) -> str:
    """Queue the due post if every condition holds. Returns what happened (see the module doc)."""
    cfg = await settings.load(db)
    where = await target(db, cfg)
    if isinstance(where, str):
        return where
    posting = await tg_store.posting_settings(db)
    slot = plan.due_slot(
        now, count=cfg.posts_per_day, start=cfg.start, end=cfg.end, quiet=posting.quiet_windows
    )
    if slot is None:
        return "not_due"
    if await slot_taken(db, where.role, slot):
        return "already_posted"
    if await posts_on(db, where.role, slot.day) >= cfg.posts_per_day:
        return "day_full"
    if quiet_until(now, posting.quiet_windows) is not None:
        return "quiet"
    last = await last_post_at(db)
    if last is not None and now - last < timedelta(minutes=posting.post_interval_min):
        return "gap"
    if await post_waiting(db):
        return "waiting"
    h = await harvest()
    chosen = await choose_links(db, h, now, cfg)
    if not chosen:
        if not await _kill_switch_on(db):
            await _alert_once(db, now, "no_links_alert_at", NO_LINKS_REPEAT, NO_LINKS_ALERT_FA)
        return "no_links"
    item, recycled = await content.next_item(db, now, cfg.recycle_after_days)
    rendered, chosen = fit(content_for(item), chosen, where.tag, cfg.texts())
    if not chosen:
        return "no_links"
    job = await poster.queue_post(db, role=where.role, text_html=rendered.text)
    post_id = (
        await db.execute(
            insert(Post)
            .values(
                role=where.role,
                outbox_id=job,
                content_item_id=item.id if item else None,
                content_html=rendered.content_html,
                channel_tag=where.tag,
                poster=poster.name,
                plan_day=slot.day,
                plan_slot=slot.index,
                planned_at=slot.at,
                created_at=now,
            )
            .returning(Post.id)
        )
    ).scalar_one()
    await db.execute(
        insert(PostLink),
        [
            {
                "post_id": post_id,
                "position": i,
                "kind": x.kind,
                "key": x.key,
                "slot": x.slot,
                "label": x.label,
                "expires_at": x.expires_at,
                "status": "active",
            }
            for i, x in enumerate(chosen)
        ],
    )
    if item is not None:
        await content.mark_used(db, item.id, now)
        if recycled:
            await _alert_once(db, now, "recycled_alert_at", RECYCLED_REPEAT, CONTENT_RECYCLED_ALERT_FA)
    else:
        await _alert_once(db, now, "no_content_alert_at", NO_CONTENT_REPEAT, NO_CONTENT_ALERT_FA)
    await log_action(
        db,
        actor=ACTOR,
        action="posts.queued",
        target=f"{where.role}:post:{post_id}",
        dry_run=False,
        allowed=True,
        reasons=["slot_due"],
        context={
            "outbox_id": job,
            "plan_day": slot.day.isoformat(),
            "plan_slot": slot.index,
            "content_item_id": item.id if item else None,
            "recycled": recycled,
            "mtproto": sum(1 for x in chosen if x.kind == "mtproto"),
            "reality": sum(1 for x in chosen if x.kind == "reality"),
        },
    )
    return "queued"


# --- status edits --------------------------------------------------------------------------------


@dataclass
class _Sent:
    id: int
    role: str
    content_html: str
    channel_tag: str
    poster: str
    last_edit_outbox_id: int | None
    last_edit_at: datetime | None
    edit_failures: int
    edit_retry_at: datetime | None
    channel_post_id: int
    live_sha: str
    shown_sha: str | None

    @property
    def showing(self) -> str:
        """sha256 of the text the channel shows: the last edit that finished without an error
        (also when Telegram answered "message is not modified"), else what the outbox recorded."""
        return self.shown_sha or self.live_sha


def edit_backoff(failures: int) -> timedelta:
    """15 min, 30 min, 1 h, 2 h ... at most 6 h after a failed edit."""
    minutes: int = 15 * 2 ** min(max(0, failures - 1), 8)
    return min(timedelta(minutes=minutes), timedelta(hours=6))


async def _sent_posts(db: AsyncConnection, since: datetime) -> list[_Sent]:
    rows = (
        await db.execute(
            select(
                Post.id,
                Post.role,
                Post.content_html,
                Post.channel_tag,
                Post.poster,
                Post.last_edit_outbox_id,
                Post.last_edit_at,
                Post.edit_failures,
                Post.edit_retry_at,
                Post.shown_sha256,
                ChannelPost.id.label("cp_id"),
                ChannelPost.text_sha256,
            )
            .join(ChannelPost, ChannelPost.outbox_id == Post.outbox_id)
            .where(Post.created_at >= since)
            .order_by(Post.created_at.desc(), Post.id.desc())
            .with_for_update(of=Post)
        )
    ).all()
    return [
        _Sent(
            id=int(r.id),
            role=r.role,
            content_html=r.content_html,
            channel_tag=r.channel_tag,
            poster=r.poster,
            last_edit_outbox_id=r.last_edit_outbox_id,
            last_edit_at=r.last_edit_at,
            edit_failures=int(r.edit_failures),
            edit_retry_at=r.edit_retry_at,
            channel_post_id=int(r.cp_id),
            live_sha=r.text_sha256,
            shown_sha=r.shown_sha256,
        )
        for r in rows
    ]


async def _settle(db: AsyncConnection, post: _Sent, now: datetime) -> bool:
    """Account for the post's last edit job. Returns True while that job is still waiting."""
    if post.last_edit_outbox_id is None:
        return False
    job = (
        await db.execute(
            select(Outbox.done_at, Outbox.last_error, Outbox.payload).where(
                Outbox.id == post.last_edit_outbox_id
            )
        )
    ).first()
    if job is not None and job.done_at is None:
        return True
    failed = job is not None and job.last_error is not None
    if job is not None and not failed:  # Telegram shows this text now (sent, or "not modified")
        text = job.payload.get("text") if isinstance(job.payload, dict) else None
        if isinstance(text, str):
            post.shown_sha = tg_store.text_sha256(text)
    post.edit_failures = post.edit_failures + 1 if failed else 0
    post.edit_retry_at = now + edit_backoff(post.edit_failures) if failed else None
    post.last_edit_outbox_id = None
    await db.execute(
        update(Post)
        .where(Post.id == post.id)
        .values(
            last_edit_outbox_id=None,
            edit_failures=post.edit_failures,
            edit_retry_at=post.edit_retry_at,
            shown_sha256=post.shown_sha,
        )
    )
    if failed and post.edit_failures == MAX_EDIT_FAILURES:
        await tg_store.enqueue_owner_alert(db, EDITS_GIVEN_UP_FA.format(post_id=post.id))
        await log_action(
            db,
            actor=ACTOR,
            action="posts.edit_given_up",
            target=f"{post.role}:post:{post.id}",
            dry_run=False,
            allowed=False,
            reasons=["edit_failed_repeatedly"],
            context={"failures": post.edit_failures},
        )
    return False


@dataclass(frozen=True)
class _Carried:
    id: int
    kind: str
    key: str
    label: str
    expires_at: datetime | None
    status: str
    missing_since: datetime | None = None


async def _carried(db: AsyncConnection, post_id: int) -> list[_Carried]:
    rows = (
        await db.execute(
            select(
                PostLink.id,
                PostLink.kind,
                PostLink.key,
                PostLink.label,
                PostLink.expires_at,
                PostLink.status,
                PostLink.missing_since,
            )
            .where(PostLink.post_id == post_id)
            .order_by(PostLink.position)
        )
    ).all()
    return [
        _Carried(
            id=int(r.id),
            kind=r.kind,
            key=r.key,
            label=r.label,
            expires_at=r.expires_at,
            status=r.status,
            missing_since=r.missing_since,
        )
        for r in rows
    ]


async def _track_missing(
    db: AsyncConnection, carried: list[_Carried], h: links.Harvest, now: datetime
) -> list[_Carried]:
    """Remember since when each active link of a post is missing from a complete harvest, and
    forget it once a provider lists it again. Returns the links with ``missing_since`` updated."""
    out: list[_Carried] = []
    for c in carried:
        missing = c.status == "active" and c.key not in h.links and c.key not in h.doubtful
        since = (c.missing_since or now) if missing else None
        if since != c.missing_since:
            await db.execute(update(PostLink).where(PostLink.id == c.id).values(missing_since=since))
            c = replace(c, missing_since=since)
        out.append(c)
    return out


@dataclass(frozen=True)
class Shown:
    """How a carried link shows now. ``new_status``: the link just left ``active`` in this post
    (stored, never undone). ``unsure``: missing for less than :data:`MISSING_GRACE`, so the post is
    not edited this round."""

    view: LinkView
    new_status: str | None = None
    unsure: bool = False


def _as(carried: _Carried, status: str) -> LinkView:
    return LinkView(kind=cast(LinkKind, carried.kind), label=carried.label, status=cast(ShownStatus, status))


def current_view(carried: _Carried, h: links.Harvest, now: datetime) -> Shown:
    """How a carried link shows now. Only a status its provider reports, or the link's own expiry,
    ends it; a link missing from the harvest is unsure first, then paused (see the module doc)."""
    if carried.status != "active":  # sticky: an ended link never comes back in a post
        return Shown(_as(carried, carried.status))
    current = h.links.get(carried.key)
    if current is None:
        if carried.expires_at is not None and carried.expires_at <= now:
            return Shown(_as(carried, "expired"), new_status="expired")
        since = carried.missing_since or now
        return Shown(_as(carried, "paused"), unsure=now - since < MISSING_GRACE)
    if current.status != "active":
        return Shown(_as(carried, current.status), new_status=current.status)
    if current.expires_at is not None and current.expires_at <= now:
        return Shown(_as(carried, "expired"), new_status="expired")
    view = LinkView(
        kind=current.kind,
        label=carried.label,
        status="active",
        url=current.url,
        expires_at=current.expires_at,
    )
    return Shown(view)


async def refresh_posts(
    db: AsyncConnection, now: datetime, harvest: HarvestFn, posters: Mapping[str, Poster] = POSTERS
) -> int:
    """Queue the status/host edits recent posts need. Returns how many edit jobs were queued."""
    cfg = await settings.load(db)
    posts = await _sent_posts(db, now - timedelta(hours=cfg.edit_window_hours))
    waiting = 0
    ready: list[_Sent] = []
    for post in posts:
        if await _settle(db, post, now):
            waiting += 1
            continue
        if post.edit_failures >= MAX_EDIT_FAILURES:
            continue
        if post.edit_retry_at is not None and post.edit_retry_at > now:
            continue
        if post.last_edit_at is not None and now - post.last_edit_at < EDIT_MIN_GAP:
            continue
        ready.append(post)
    if not ready or waiting >= MAX_PENDING_EDITS:
        return 0
    h = await harvest()
    if not h.complete or h.providers == 0:  # no full picture of the links: change nothing
        return 0
    texts = cfg.texts()
    queued = 0
    for post in ready:
        if queued >= MAX_EDITS_PER_RUN or waiting + queued >= MAX_PENDING_EDITS:
            break
        poster = posters.get(post.poster)
        if poster is None:
            continue
        carried = await _track_missing(db, await _carried(db, post.id), h, now)
        if any(c.status == "active" and c.key in h.doubtful for c in carried):
            continue
        shown = [current_view(c, h, now) for c in carried]
        if any(x.unsure for x in shown):
            continue  # a link is missing for a moment only: wait before showing it as paused
        views = [x.view for x in shown]
        changes = [(c, x.new_status) for c, x in zip(carried, shown, strict=True) if x.new_status]
        paused = [c.label for c, x in zip(carried, shown, strict=True) if x.view.status == "paused"]
        try:
            text = render_post(post.content_html, views, post.channel_tag, texts).text
        except (PostTooLong, ValueError) as exc:
            log.warning("publisher: post %d could not be rendered again (%s)", post.id, type(exc).__name__)
            continue
        for c, new_status in changes:
            await db.execute(
                update(PostLink)
                .where(and_(PostLink.id == c.id, PostLink.status == "active"))
                .values(status=new_status, status_at=now)
            )
        if tg_store.text_sha256(text) == post.showing:
            continue
        job = await poster.queue_edit(db, channel_post_id=post.channel_post_id, text_html=text)
        await db.execute(
            update(Post)
            .where(Post.id == post.id)
            .values(last_edit_outbox_id=job, last_edit_at=now, edits=Post.edits + 1)
        )
        queued += 1
        await log_action(
            db,
            actor=ACTOR,
            action="posts.edit_queued",
            target=f"{post.role}:post:{post.id}",
            dry_run=False,
            allowed=True,
            reasons=[*(["link_status"] if changes else []), *(["link_paused"] if paused else [])]
            or ["link_moved"],
            context={
                "outbox_id": job,
                "changes": [{"label": c.label, "kind": c.kind, "status": s} for c, s in changes],
                "paused": paused,
            },
        )
    return queued
