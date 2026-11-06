"""Content engine API (decision D51, M3.1 phase A): the owner's sources and the review queue.

Owner only (``require_admin``; CSRF on writes). Errors are ``{"error", "message_fa"}``:

=========================  ======  ===========================================================
``invalid_request``        422     body is not JSON or not an object of the expected keys
``kind_invalid``           422     the kind is neither ``telegram`` nor ``rss``
``telegram_name_invalid``  422     not a public channel's name or t.me link
``feed_url_invalid``       422     not an https feed URL to a public DNS name
``label_invalid``          422
``source_exists``          409     the same channel or feed is already a source
``too_many_sources``       409     at most 50
``source_not_found``       404
``caption_invalid``        422     title up to 120, body up to 1000 characters, one of them set
``status_invalid``         422
``not_editable``           409     the candidate was posted, expired or is a duplicate
``candidate_not_found``    404
``media_not_found``        404     the file was removed (its post went out or the item ended)
=========================  ======  ===========================================================

* ``GET  /api/content/sources`` / ``POST`` (``kind``, ``ref``, ``label``) / ``PUT /{id}``
  (``enabled``, ``label``) / ``POST /{id}/fetch`` (read at the next round) / ``DELETE /{id}``.
* ``GET  /api/content/queue?status=pending&sort=new|views|hot``: the candidates, the counts per
  status and ``caption_budget`` (characters a media caption's content may have now).
* ``PUT  /api/content/candidates/{id}`` (``title``, ``body``) and ``POST /{id}/status``
  (``status``: pending, approved, rejected; with ``title`` and ``body`` an approval saves the
  edited caption at the same time).
* ``GET  /api/content/media/{id}``: a candidate's photo, thumbnail or video, the panel's own copy
  (never a redirect to the source), with its sniffed type, ``nosniff`` and a sandboxing policy.

No proxy link, secret or source media URL is returned. Nothing here touches a channel: an approved
candidate is posted by the publisher (proxyhub/posts/publisher.py).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.content import candidates, sources
from proxyhub.content.candidates import CandidateInvalid
from proxyhub.content.items import SourceInvalid
from proxyhub.content.messages import API_ERRORS_FA
from proxyhub.fleet.actions import log_action
from proxyhub.posts import links, views
from proxyhub.telegram import store as tg_store

api = APIRouter(prefix="/api/content", tags=["content"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

STATUS: dict[str, int] = {
    "invalid_request": 422,
    "kind_invalid": 422,
    "telegram_name_invalid": 422,
    "feed_url_invalid": 422,
    "label_invalid": 422,
    "source_exists": 409,
    "too_many_sources": 409,
    "source_not_found": 404,
    "caption_invalid": 422,
    "status_invalid": 422,
    "not_editable": 409,
    "candidate_not_found": 404,
    "media_not_found": 404,
}
MEDIA_HEADERS: dict[str, str] = {
    "Cache-Control": "private, max-age=3600",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "Content-Disposition": "inline",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _err(code: str) -> ApiError:
    return ApiError(
        code,
        status_code=STATUS.get(code, 400),
        message_fa=API_ERRORS_FA.get(code, API_ERRORS_FA["invalid_request"]),
    )


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


async def _body(request: Request, keys: set[str], required: set[str]) -> dict[str, Any]:
    try:
        value = json.loads(await request.body())
    except (ValueError, RecursionError):
        raise _err("invalid_request") from None
    if not isinstance(value, dict) or not set(value) <= keys or not required <= set(value):
        raise _err("invalid_request")
    return value


# --- sources -------------------------------------------------------------------------------------------


@api.get("/sources")
async def get_sources(admin: Admin, db: Db) -> dict[str, Any]:
    found = await sources.list_all(db)
    pending = await sources.pending_counts(db)
    await db.rollback()
    return {
        "sources": [s.as_json(pending.get(s.id, 0)) for s in found],
        "max_sources": sources.MAX_SOURCES,
        "fetch_every_min": int(sources.FETCH_EVERY.total_seconds() // 60),
    }


@api.post("/sources", status_code=201)
async def add_source(admin: Admin, db: Db, request: Request) -> dict[str, Any]:
    value = await _body(request, {"kind", "ref", "label"}, {"kind", "ref"})
    try:
        source = await sources.create(
            db, kind=value["kind"], ref=value["ref"], label=value.get("label"), now=tg_store.now_utc()
        )
    except SourceInvalid as exc:
        raise _err(exc.code) from None
    except sources.SourceExists:
        raise _err("source_exists") from None
    except sources.TooManySources:
        raise _err("too_many_sources") from None
    await log_action(
        db,
        actor=_actor(admin),
        action="content.source_added",
        target=f"source:{source.id}",
        dry_run=False,
        allowed=True,
        context={"kind": source.kind, "ref": source.ref, "label": source.label},
    )
    await db.commit()
    return source.as_json()


@api.put("/sources/{source_id}")
async def put_source(admin: Admin, db: Db, request: Request, source_id: int) -> dict[str, Any]:
    value = await _body(request, {"enabled", "label"}, set())
    enabled = value.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise _err("invalid_request")
    try:
        source = await sources.update_source(
            db, source_id, enabled=enabled, label=value.get("label"), now=tg_store.now_utc()
        )
    except SourceInvalid as exc:
        raise _err(exc.code) from None
    if source is None:
        raise _err("source_not_found")
    await log_action(
        db,
        actor=_actor(admin),
        action="content.source_updated",
        target=f"source:{source.id}",
        dry_run=False,
        allowed=True,
        context={k: v for k, v in value.items() if k in ("enabled", "label")},
    )
    await db.commit()
    return source.as_json((await sources.pending_counts(db)).get(source.id, 0))


@api.post("/sources/{source_id}/fetch")
async def fetch_source(admin: Admin, db: Db, source_id: int) -> dict[str, Any]:
    source = await sources.fetch_now(db, source_id, tg_store.now_utc())
    if source is None:
        raise _err("source_not_found")
    await db.commit()
    return source.as_json()


@api.delete("/sources/{source_id}")
async def delete_source(admin: Admin, db: Db, source_id: int) -> dict[str, Any]:
    found = await sources.get(db, source_id)
    dropped = await sources.remove(db, source_id)
    if found is None or dropped is None:
        raise _err("source_not_found")
    await log_action(
        db,
        actor=_actor(admin),
        action="content.source_removed",
        target=f"source:{source_id}",
        dry_run=False,
        allowed=True,
        context={"kind": found.kind, "ref": found.ref, "dropped_pending": dropped},
    )
    await db.commit()
    return {"removed": True, "dropped_pending": dropped}


# --- the review queue --------------------------------------------------------------------------------


@api.get("/queue")
async def get_queue(
    admin: Admin,
    db: Db,
    status: Annotated[str, Query(pattern="^(pending|approved|rejected|used|expired)$")] = "pending",
    sort: Annotated[str, Query(pattern="^(new|views|hot)$")] = "new",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    conn = await db.connection()
    now = tg_store.now_utc()
    found = await candidates.queue(conn, status, sort, now, limit)
    ids = [c.id for c in found]
    media = await candidates.media_rows(conn, ids)
    also = await candidates.also_in(conn, ids)
    budget = await views.caption_budget(conn, now, await links.harvest(conn))
    out = {
        "items": [candidates.as_json(c, media.get(c.id), also.get(c.id)) for c in found],
        "counts": await candidates.counts(conn),
        "caption_budget": budget,
    }
    await db.rollback()
    return out


async def _log_candidate(
    db: AsyncSession, admin: AdminIdentity, action: str, item: Any, **context: Any
) -> None:
    await log_action(
        db,
        actor=_actor(admin),
        action=action,
        target=f"candidate:{item.id}",
        dry_run=False,
        allowed=True,
        context={"source": item.source_label, "title": item.title[:60], **context},
    )


@api.put("/candidates/{candidate_id}")
async def edit_candidate(admin: Admin, db: Db, request: Request, candidate_id: int) -> dict[str, Any]:
    value = await _body(request, {"title", "body"}, {"title", "body"})
    try:
        item = await candidates.edit(db, candidate_id, value["title"], value["body"], tg_store.now_utc())
    except CandidateInvalid as exc:
        raise _err(exc.code) from None
    if item is None:
        raise _err("candidate_not_found")
    await _log_candidate(db, admin, "content.candidate_edited", item)
    await db.commit()
    return await _one(db, item)


@api.post("/candidates/{candidate_id}/status")
async def set_candidate_status(admin: Admin, db: Db, request: Request, candidate_id: int) -> dict[str, Any]:
    value = await _body(request, {"status", "title", "body"}, {"status"})
    status = value["status"]
    if not isinstance(status, str) or status not in candidates.OWNER_STATUSES:
        raise _err("status_invalid")
    if ("title" in value) != ("body" in value):
        raise _err("invalid_request")
    now = tg_store.now_utc()
    try:
        if "title" in value:
            if await candidates.edit(db, candidate_id, value["title"], value["body"], now) is None:
                raise _err("candidate_not_found")
        item, before = await candidates.set_status(db, candidate_id, status, now)
    except CandidateInvalid as exc:
        raise _err(exc.code) from None
    if item is None:
        raise _err("candidate_not_found")
    if before != item.status:
        await _log_candidate(
            db, admin, "content.candidate_status", item, **{"from": before, "to": item.status}
        )
    await db.commit()
    return await _one(db, item)


async def _one(db: AsyncSession, item: candidates.Candidate) -> dict[str, Any]:
    media = await candidates.media_rows(db, [item.id])
    also = await candidates.also_in(db, [item.id])
    out = candidates.as_json(item, media.get(item.id), also.get(item.id))
    await db.rollback()
    return out


@api.get("/media/{media_id}")
async def get_media(admin: Admin, db: Db, media_id: int) -> Response:
    found = await candidates.media_data(db, media_id)
    await db.rollback()
    if found is None:
        raise _err("media_not_found")
    mime, data = found
    return Response(content=data, media_type=mime, headers=MEDIA_HEADERS)


router = APIRouter()
router.include_router(api)
