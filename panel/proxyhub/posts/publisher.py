"""The publisher's two steps, each run in the caller's transaction (proxyhub/reconcilers/publisher.py).

**New posts** (:func:`publish_due`): when a slot of today's plan is due (proxyhub/posts/plan.py),
the post is the next content + the live links of every provider + the channel tag, queued for
the TEST channel, or for the MAIN channel only when the owner turned ``main_enabled`` on. Checks,
in order: publisher on, a bot, the target channel set, a due slot not yet posted, the day not full,
not inside a no-post window (D21), at least ``post_interval_min`` since the last post (both
channels), and no earlier post still waiting in the outbox. **A post never goes without at least
one live link** (docs/04): then nothing is queued, the slot stays due until its grace ends, and the
owner hears it at most once a day (not while the fleet's kill switch is on: nothing can run then).
Its MTProto links take an operator's name only from the panel's own fresh probes (decision D50,
proxyhub/posts/operators.py); the names are stored with the links (``post_links.operator``) and every
later render of the post uses them, so a status edit never renames a link.
The post goes through the owner's poster (proxyhub/posts/poster.py); when that is the operator
account and it cannot take the post now, the post goes to the bot and the reason is logged
(``posts.poster_fallback``, shown on the Content page).

**The content** (decision D51): the oldest candidate the owner approved in the review queue
(proxyhub/content/candidates.py), else the old content bank (proxyhub/posts/content.py). A candidate
with a downloaded photo or video makes a MEDIA post: the file with the content and the links as its
caption, within :data:`~proxyhub.telegram.templates.CAPTION_LIMIT` (1024) for both posters
(:func:`fit_caption`). The links must fit now and in every later state; links that cannot are
dropped from the end (the Reality configs first, ``posts.caption_links_dropped``); when not one fits,
or the candidate's file failed, the post goes as text (``posts.media_skipped``). The content is what
gets shortened, never a link.

**Status edits** (:func:`refresh_posts`): every sent post younger than ``edit_window_hours`` is
rendered again from what it carries, in the order it was posted (a post shows no label: an MTProto
link is numbered by its place, a config's TCP/XHTTP marker follows from the keys): a link the
provider now REPORTS expired/full/burned (or whose own expiry passed) gets its status mark and
loses its link; a live link takes its provider's current
URL, so a new active domain (proxyhub.cloudflare.rotation) or a node's slot name replaces the old
host. When the text differs from the one the channel shows (the last edit that finished without an
error, else ``channel_posts.text_sha256``) an ``edit_post`` job is queued, through the poster that
sent the post (for a photo or video post, an edit of its CAPTION, within the caption limit).
Bounds: one edit job per post at a time, :data:`EDIT_MIN_GAP` between two edits of a
post, :data:`MAX_EDITS_PER_RUN` per step and :data:`MAX_PENDING_EDITS` waiting at once; the outbox
paces every message per channel (3 s, 429). A failed edit is retried with backoff and given up after
:data:`MAX_EDIT_FAILURES` (owner alerted). A link's status in a post never goes back to active, so
only a REPORTED status (or the link's own expiry) ends a link in a post.

**A post whose message is gone** (someone deleted it in the channel by hand; the edit failed with
:data:`~proxyhub.telegram.outbox.MESSAGE_GONE`, bot or operator account) is final at once: it is
marked ``posts.gone_at``, logged once (``posts.message_gone``) and never edited again; no failure is
counted and the owner gets no alert (the dashboard shows the post as gone). The panel itself never
deletes a post.

**A new layout reaches recent posts the same way** (the compact layout of decision D48, and the
layouts before it): there is no layout version to keep. A post is rendered from what it carries,
never from the text it was sent with, so after a layout change the render of every post younger
than ``edit_window_hours`` differs ONCE from the text the channel shows, and that post gets one edit,
within the same bounds as any edit (at most :data:`MAX_EDITS_PER_RUN` a round,
:data:`MAX_PENDING_EDITS` waiting, the outbox's pacing); after it the render equals the shown text
and nothing more is queued. Posts older than the window keep the layout they were sent with. A
post's content is kept as it was posted (``posts.content_html``): only the links part changes.

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

from proxyhub.content import candidates
from proxyhub.db.models import Outbox
from proxyhub.db.models_posts import Post, PostLink
from proxyhub.db.models_telegram import ChannelPost
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings
from proxyhub.links.contract import ProxyLink
from proxyhub.posts import content, links, operator, operators, plan, settings
from proxyhub.posts.messages import (
    CONTENT_RECYCLED_ALERT_FA,
    EDITS_GIVEN_UP_FA,
    FALLBACK_TITLE_FA,
    NO_CONTENT_ALERT_FA,
    NO_LINKS_ALERT_FA,
)
from proxyhub.posts.poster import BOT, POSTERS, MediaPayload, Poster, for_new_post
from proxyhub.telegram import outbox as tg_outbox
from proxyhub.telegram import store as tg_store
from proxyhub.telegram.schedule import quiet_until
from proxyhub.telegram.templates import (
    CAPTION_LIMIT,
    LinkKind,
    PostTooLong,
    RenderedPost,
    ShownStatus,
    StatusTexts,
    channel_tag,
    compose_content,
    render_post,
    tail_always_fits,
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


def with_marks(views: Sequence[LinkView], keys: Sequence[tuple[str, str]]) -> list[LinkView]:
    """The views with the transport markers their keys give (:func:`links.transport_marks`)."""
    marks = links.transport_marks(keys)
    return [
        replace(v, variant=variant, companion=companion)
        for v, (variant, companion) in zip(views, marks, strict=True)
    ]


def views_for_new_post(chosen: Sequence[ProxyLink], names: Mapping[str, str] | None = None) -> list[LinkView]:
    """``names``: key -> the operator a link is named after (:func:`operators.operator_labels`)."""
    views = [
        LinkView(
            kind=x.kind,
            status="active",
            url=x.url,
            expires_at=x.expires_at,
            operator=(names or {}).get(x.key),
        )
        for x in chosen
    ]
    return with_marks(views, [(x.kind, x.key) for x in chosen])


def fit(
    content_html: str,
    chosen: Sequence[ProxyLink],
    tag: str,
    texts: StatusTexts,
    names: Mapping[str, str] | None = None,
) -> tuple[RenderedPost, list[ProxyLink]]:
    """Render; if even the links do not fit one message, drop links from the end (Reality first)."""
    kept = list(chosen)
    while True:
        try:
            return render_post(content_html, views_for_new_post(kept, names), tag, texts), kept
        except PostTooLong:
            if not kept:
                raise
            kept.pop()


def fit_caption(
    content_html: str,
    chosen: Sequence[ProxyLink],
    tag: str,
    texts: StatusTexts,
    names: Mapping[str, str] | None = None,
) -> tuple[RenderedPost, list[ProxyLink]]:
    """A media post's caption (decision D51): the links must fit the caption limit now AND in every
    later state (a status edit only ever shortens the content); otherwise links are dropped from the
    end (the Reality configs first). PostTooLong when not even one link fits."""
    kept = list(chosen)
    while kept:
        views = views_for_new_post(kept, names)
        if tail_always_fits(views, tag, texts, caption=True):
            return render_post(content_html, views, tag, texts, caption=True), kept
        kept.pop()
    raise PostTooLong("not one link fits a media caption")


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
    db: AsyncConnection, h: links.Harvest, now: datetime, cfg: settings.PostingConfig, role: str
) -> list[ProxyLink]:
    """The links of the next post; the slots turn by one with every post made so far. The MAIN
    channel never gets a test node's link (see :func:`links.for_new_post`)."""
    made = int((await db.execute(select(func.count()).select_from(Post))).scalar_one())
    return links.for_new_post(
        h,
        now,
        max_mtproto=cfg.max_mtproto,
        max_reality=cfg.max_reality,
        rotate=made,
        slots_only=role == "main",
    )


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
    chosen = await choose_links(db, h, now, cfg, where.role)
    if not chosen:
        if not await _kill_switch_on(db):
            await _alert_once(db, now, "no_links_alert_at", NO_LINKS_REPEAT, NO_LINKS_ALERT_FA)
        return "no_links"
    # The owner's approved candidate first (decision D51), else the content bank.
    candidate = await candidates.next_for_post(db, now)
    item, recycled = (
        (None, False) if candidate is not None else await content.next_item(db, now, cfg.recycle_after_days)
    )
    content_html = candidate.content_html() if candidate is not None else content_for(item)
    names = await operators.operator_labels(db, chosen, now)  # D50: decided once, stored below
    media_row = await candidates.post_media(db, candidate) if candidate is not None else None
    offered = list(chosen)
    media_skipped: str | None = None
    if media_row is not None:
        try:
            rendered, chosen = fit_caption(content_html, chosen, where.tag, cfg.texts(), names)
        except PostTooLong:  # not one link fits a caption: the post goes as text, with its links
            media_row, media_skipped = None, "links_do_not_fit_caption"
    elif candidate is not None and candidate.media_kind is not None:
        media_skipped = f"media_{candidate.media_state}"  # approved although its file failed
    if media_row is None:
        rendered, chosen = fit(content_html, chosen, where.tag, cfg.texts(), names)
    if not chosen:
        return "no_links"
    media = (
        MediaPayload(
            kind="video" if media_row.mime == "video/mp4" else "photo",
            media_id=media_row.id,
            width=media_row.width,
            height=media_row.height,
            duration=media_row.duration,
        )
        if media_row is not None
        else None
    )
    poster, fallback = await for_new_post(db, poster, role=where.role, now=now)
    job = await poster.queue_post(db, role=where.role, text_html=rendered.text, media=media)
    post_id = (
        await db.execute(
            insert(Post)
            .values(
                role=where.role,
                outbox_id=job,
                content_item_id=item.id if item else None,
                content_candidate_id=candidate.id if candidate is not None else None,
                media_kind=media.kind if media is not None else None,
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
                "operator": names.get(x.key),
            }
            for i, x in enumerate(chosen)
        ],
    )
    if fallback is not None:
        await operator.log_fallback(
            db, actor=ACTOR, role=where.role, post_id=post_id, reason=fallback, stage="queue", outbox_id=job
        )
    dropped = offered[len(chosen) :]
    if media is not None and dropped:
        await log_action(
            db,
            actor=ACTOR,
            action="posts.caption_links_dropped",
            target=f"{where.role}:post:{post_id}",
            dry_run=False,
            allowed=True,
            reasons=["caption_limit"],
            context={
                "caption_limit": CAPTION_LIMIT,
                "dropped": [{"label": x.label, "kind": x.kind} for x in dropped],
            },
        )
    if media_skipped is not None:
        await log_action(
            db,
            actor=ACTOR,
            action="posts.media_skipped",
            target=f"{where.role}:post:{post_id}",
            dry_run=False,
            allowed=True,
            reasons=[media_skipped],
            context={"candidate_id": candidate.id if candidate is not None else None},
        )
    if candidate is not None:
        await candidates.mark_used(db, candidate.id, now)
    elif item is not None:
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
            "content_candidate_id": candidate.id if candidate is not None else None,
            "media": media.kind if media is not None else None,
            "recycled": recycled,
            "mtproto": sum(1 for x in chosen if x.kind == "mtproto"),
            "mtproto_extras": sum(1 for x in chosen if x.extra_of is not None),
            "operator_names": sorted(names[x.key] for x in chosen if x.key in names),
            "reality": sum(1 for x in chosen if x.kind == "reality"),
            "poster": poster.name,
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
    gone: bool = False  # this round found its message gone from the channel
    media_kind: str | None = None  # a photo or video post: its text is the media's caption (D51)

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
                Post.media_kind,
                ChannelPost.id.label("cp_id"),
                ChannelPost.text_sha256,
            )
            .join(ChannelPost, ChannelPost.outbox_id == Post.outbox_id)
            .where(Post.created_at >= since, Post.gone_at.is_(None))
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
            media_kind=r.media_kind,
        )
        for r in rows
    ]


async def _gone(db: AsyncConnection, post: _Sent, job_id: int, now: datetime) -> None:
    """The post's message no longer exists in the channel: never edit it again (see the module doc)."""
    post.gone = True
    post.last_edit_outbox_id = None
    await db.execute(
        update(Post)
        .where(Post.id == post.id)
        .values(last_edit_outbox_id=None, edit_retry_at=None, gone_at=now)
    )
    await log_action(
        db,
        actor=ACTOR,
        action="posts.message_gone",
        target=f"{post.role}:post:{post.id}",
        dry_run=False,
        allowed=False,
        reasons=["message_gone"],
        context={"outbox_id": job_id, "poster": post.poster, "channel_post_id": post.channel_post_id},
    )


async def _settle(db: AsyncConnection, post: _Sent, now: datetime) -> bool:
    """Account for the post's last edit job. Returns True while that job is still waiting. A job
    that found the message gone marks the post gone (``post.gone``) instead of counting a failure."""
    job_id = post.last_edit_outbox_id
    if job_id is None:
        return False
    job = (
        await db.execute(select(Outbox.done_at, Outbox.last_error, Outbox.payload).where(Outbox.id == job_id))
    ).first()
    if job is not None and job.done_at is None:
        return True
    if job is not None and tg_outbox.message_gone(job.last_error):
        await _gone(db, post, job_id, now)
        return False
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
    operator: str | None = None  # the operator the post names this link after (D50)


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
                PostLink.operator,
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
            operator=r.operator,
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
    return LinkView(kind=cast(LinkKind, carried.kind), status=cast(ShownStatus, status))


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
    view = LinkView(kind=current.kind, status="active", url=current.url, expires_at=current.expires_at)
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
        if post.gone or post.edit_failures >= MAX_EDIT_FAILURES:
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
        views = with_marks(
            [replace(x.view, operator=c.operator) for c, x in zip(carried, shown, strict=True)],
            [(c.kind, c.key) for c in carried],
        )
        changes = [(c, x.new_status) for c, x in zip(carried, shown, strict=True) if x.new_status]
        paused = [c.label for c, x in zip(carried, shown, strict=True) if x.view.status == "paused"]
        caption = post.media_kind is not None
        try:
            text = render_post(post.content_html, views, post.channel_tag, texts, caption=caption).text
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
        job = await poster.queue_edit(
            db, channel_post_id=post.channel_post_id, text_html=text, caption=caption
        )
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
