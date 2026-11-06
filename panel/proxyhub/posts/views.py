"""What the dashboard shows about posting: the state of the publisher, recent posts and a preview.

Only :func:`preview` returns proxy links: they are exactly the links the next post will publish in
the channel on purpose (CLAUDE.md rule 3 allows those). Nothing else here returns a URL, a
secret or a VLESS id: a recent post lists its links by label and status only.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.content import candidates
from proxyhub.db.models import Outbox
from proxyhub.db.models_content import ContentCandidate
from proxyhub.db.models_posts import ContentItem, Post, PostLink
from proxyhub.db.models_telegram import ChannelPost
from proxyhub.fleet.actions import iso
from proxyhub.posts import content, entities, links, operator, operators, plan, publisher, settings
from proxyhub.telegram import store as tg_store
from proxyhub.telegram.schedule import TEHRAN, quiet_until
from proxyhub.telegram.templates import (
    CAPTION_LIMIT,
    PostTooLong,
    later_states,
    post_tail,
    utf16_len,
    visible_text,
)

MAX_RECENT = 50


def _slot_json(slot: plan.Slot) -> dict[str, Any]:
    return {"at": iso(slot.at), "day": slot.day.isoformat(), "index": slot.index}


async def status(db: AsyncConnection, now: datetime, h: links.Harvest) -> dict[str, Any]:
    cfg = await settings.load(db)
    posting = await tg_store.posting_settings(db)
    channels = await tg_store.channels(db)
    where = await publisher.target(db, cfg)
    role = cfg.target_role
    today = now.astimezone(TEHRAN).date()
    todays = plan.day_plan(
        today, count=cfg.posts_per_day, start=cfg.start, end=cfg.end, quiet=posting.quiet_windows
    )
    nxt = plan.upcoming(
        now, count=cfg.posts_per_day, start=cfg.start, end=cfg.end, quiet=posting.quiet_windows, limit=1
    )
    last = await publisher.last_post_at(db)
    return {
        "target": role,
        "blocked": where if isinstance(where, str) else None,
        "channels": {r: (channels[r].get("username") if r in channels else None) for r in tg_store.ROLES},
        "channel_tag": publisher.channel_tag_for(channels),
        "quiet_now": quiet_until(now, posting.quiet_windows) is not None,
        "post_interval_min": posting.post_interval_min,
        "today": {
            "planned": [_slot_json(s) for s in todays],
            "posted": await publisher.posts_on(db, role, today),
        },
        "next_slot": _slot_json(nxt[0]) if nxt and cfg.enabled else None,
        "last_post_at": iso(last) if last else None,
        "links": {
            "providers": h.providers,
            "complete": h.complete,
            "mtproto": len(h.active("mtproto")),
            "reality": len(h.active("reality")),
        },
        "content": await content.counts(db),
        "candidates": await candidates.counts(db),
    }


async def preview(db: AsyncConnection, now: datetime, h: links.Harvest) -> dict[str, Any]:
    """The next post as it would go now (nothing is consumed or queued)."""
    cfg = await settings.load(db)
    channels = await tg_store.channels(db)
    tag = publisher.channel_tag_for(channels)
    chosen = await publisher.choose_links(db, h, now, cfg, cfg.target_role)
    candidate = await candidates.next_for_post(db, now)
    item, recycled = (
        (None, False) if candidate is not None else await content.next_item(db, now, cfg.recycle_after_days)
    )
    content_html = candidate.content_html() if candidate is not None else publisher.content_for(item)
    names = await operators.operator_labels(db, chosen, now)
    media_row = await candidates.post_media(db, candidate) if candidate is not None else None
    rendered = None
    if media_row is not None and chosen:
        try:
            rendered, chosen = publisher.fit_caption(content_html, chosen, tag, cfg.texts(), names)
        except PostTooLong:
            media_row = None
    if rendered is None:
        media_row = None
        rendered, chosen = publisher.fit(content_html, chosen, tag, cfg.texts(), names)
    where = await publisher.target(db, cfg)
    pack = await operator.emoji_map(db)
    return {
        "target": cfg.target_role,
        "blocked": where if isinstance(where, str) else ("no_links" if not chosen else None),
        "text": rendered.text,
        "chars": utf16_len(visible_text(rendered.text)),
        # Which emoji the operator account's post would show animated (decision D38); the bot's
        # post always keeps plain emoji.
        "emoji": [{"emoji": e, "animated": a} for e, a in entities.emoji_report(rendered.text, pack)],
        "content_item": {"id": item.id, "title": item.title, "recycled": recycled} if item else None,
        # The owner's approved candidate (decision D51) goes before the content bank.
        "candidate": {"id": candidate.id, "title": candidate.title, "source": candidate.source_label}
        if candidate is not None
        else None,
        "media": {
            "kind": "video" if media_row.mime == "video/mp4" else "photo",
            "preview_id": (await _preview_id(db, candidate.id)) if candidate is not None else None,
            "caption_limit": CAPTION_LIMIT,
        }
        if media_row is not None
        else None,
        "links": {
            "mtproto": sum(1 for x in chosen if x.kind == "mtproto"),
            "reality": sum(1 for x in chosen if x.kind == "reality"),
        },
    }


async def _preview_id(db: AsyncConnection, candidate_id: int) -> int | None:
    rows = (await candidates.media_rows(db, [candidate_id])).get(candidate_id, {})
    main, thumb = rows.get("main"), rows.get("thumb")
    shown = main if main is not None and main.mime != "video/mp4" else thumb
    return shown.id if shown is not None else None


async def caption_budget(db: AsyncConnection, now: datetime, h: links.Harvest) -> int | None:
    """How many characters a media post's content (title line + text) may have with the links the
    next post would carry, in their longest later state (docs/04): the review queue shows it while
    the owner edits a caption. None while there is no live link at all."""
    cfg = await settings.load(db)
    tag = publisher.channel_tag_for(await tg_store.channels(db))
    chosen = await publisher.choose_links(db, h, now, cfg, cfg.target_role)
    if not chosen:
        return None
    names = await operators.operator_labels(db, chosen, now)
    try:
        _, kept = publisher.fit_caption("", chosen, tag, cfg.texts(), names)
    except PostTooLong:
        return 0
    views = publisher.views_for_new_post(kept, names)
    longest = max(utf16_len(visible_text(post_tail(v, tag, cfg.texts()))) for v in later_states(views))
    return max(0, CAPTION_LIMIT - longest - 2)


def _state(r: Any) -> str:
    if r.cp_id is not None:
        return "sent"
    if r.outbox_id is None:
        return "unknown"
    if r.done_at is not None:
        return "failed" if r.last_error is not None else "sent"
    return "queued"


async def recent(db: AsyncConnection, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(limit, MAX_RECENT))
    rows = (
        await db.execute(
            select(
                Post.id,
                Post.role,
                Post.created_at,
                Post.planned_at,
                Post.outbox_id,
                Post.edits,
                Post.last_edit_at,
                Post.edit_failures,
                Post.poster,
                Post.gone_at,
                Post.media_kind,
                func.coalesce(ContentItem.title, ContentCandidate.title).label("title"),
                Outbox.done_at,
                Outbox.last_error,
                ChannelPost.id.label("cp_id"),
                ChannelPost.message_id,
            )
            .outerjoin(ContentItem, ContentItem.id == Post.content_item_id)
            .outerjoin(ContentCandidate, ContentCandidate.id == Post.content_candidate_id)
            .outerjoin(Outbox, Outbox.id == Post.outbox_id)
            .outerjoin(ChannelPost, ChannelPost.outbox_id == Post.outbox_id)
            .order_by(Post.created_at.desc(), Post.id.desc())
            .limit(limit)
        )
    ).all()
    ids = [int(r.id) for r in rows]
    carried: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    if ids:
        for lr in (
            await db.execute(
                select(PostLink.post_id, PostLink.kind, PostLink.label, PostLink.status, PostLink.status_at)
                .where(PostLink.post_id.in_(ids))
                .order_by(PostLink.post_id, PostLink.position)
            )
        ).all():
            carried[int(lr.post_id)].append(
                {
                    "kind": lr.kind,
                    "label": lr.label,
                    "status": lr.status,
                    "status_at": iso(lr.status_at) if lr.status_at else None,
                }
            )
    channels = await tg_store.channels(db)
    cfg = await settings.load(db)
    window = timedelta(hours=cfg.edit_window_hours)
    out: list[dict[str, Any]] = []
    for r in rows:
        username = (channels.get(r.role) or {}).get("username")
        message_url = None
        if (
            r.gone_at is None
            and r.message_id is not None
            and isinstance(username, str)
            and username.startswith("@")
        ):
            message_url = f"https://t.me/{username[1:]}/{int(r.message_id)}"
        out.append(
            {
                "id": int(r.id),
                "role": r.role,
                # "gone": its message was deleted in the channel; it is no longer edited.
                "state": "gone" if r.gone_at is not None else _state(r),
                "created_at": iso(r.created_at),
                "planned_at": iso(r.planned_at),
                "title": r.title,
                "links": carried[int(r.id)],
                "edits": int(r.edits),
                "last_edit_at": iso(r.last_edit_at) if r.last_edit_at else None,
                "edits_stopped": int(r.edit_failures) >= publisher.MAX_EDIT_FAILURES,
                "watching": r.gone_at is None and now - r.created_at < window,
                "message_url": message_url,
                "poster": r.poster,
                "media": r.media_kind,
            }
        )
    return out
