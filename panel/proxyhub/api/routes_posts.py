"""Posts API: posting settings, the publisher's state, recent posts, preview, content library.

Owner only (``require_admin``; CSRF on writes). Errors are ``{"error", "message_fa"}``:

=======================  ======  ==============================================================
``invalid_request``      422     body is not JSON or not an object of the expected keys
``invalid_settings``     422     a setting is out of range (``fields`` lists which; no value echoed)
``main_channel_not_set`` 409     main posting turned on while no main channel is set
``title_invalid``        422     ``body_invalid``, ``tags_invalid``, ``status_invalid`` likewise
``content_not_found``    404
=======================  ======  ==============================================================

Only ``GET /preview`` returns proxy links: the very links the next post publishes in the channel
(CLAUDE.md rule 3 allows those). Status, recent posts and actions_log name links by label only.
Nothing here deletes anything: a content item is retired, a post is never removed (rule 6).
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.fleet.actions import log_action
from proxyhub.posts import content, links, settings, views
from proxyhub.posts.content import ContentInvalid
from proxyhub.posts.messages import ERRORS_FA
from proxyhub.telegram import store as tg_store

api = APIRouter(prefix="/api/posts", tags=["posts"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

STATUS: dict[str, int] = {
    "invalid_request": 422,
    "invalid_settings": 422,
    "main_channel_not_set": 409,
    "title_invalid": 422,
    "body_invalid": 422,
    "tags_invalid": 422,
    "status_invalid": 422,
    "content_not_found": 404,
}


def _err(code: str, **extra: Any) -> ApiError:
    return ApiError(
        code,
        status_code=STATUS.get(code, 400),
        message_fa=ERRORS_FA.get(code, ERRORS_FA["invalid_request"]),
        extra=extra or None,
    )


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


async def _body(request: Request, keys: set[str], required: set[str]) -> dict[str, Any]:
    """The JSON object of the request, with only ``keys`` and at least ``required``."""
    try:
        value = json.loads(await request.body())
    except (ValueError, RecursionError):
        raise _err("invalid_request") from None
    if not isinstance(value, dict) or not set(value) <= keys or not required <= set(value):
        raise _err("invalid_request")
    return value


# --- publisher state, preview, recent posts ------------------------------------------------------


@api.get("/status")
async def get_status(admin: Admin, db: Db) -> dict[str, Any]:
    conn = await db.connection()
    now = tg_store.now_utc()
    out = await views.status(conn, now, await links.harvest(conn))
    await db.rollback()
    return out


@api.get("/preview")
async def get_preview(admin: Admin, db: Db) -> dict[str, Any]:
    conn = await db.connection()
    now = tg_store.now_utc()
    out = await views.preview(conn, now, await links.harvest(conn))
    await db.rollback()
    return out


@api.get("/recent")
async def get_recent(admin: Admin, db: Db, limit: Annotated[int, Query(ge=1, le=50)] = 20) -> dict[str, Any]:
    conn = await db.connection()
    out = await views.recent(conn, tg_store.now_utc(), limit)
    await db.rollback()
    return {"posts": out}


# --- settings ------------------------------------------------------------------------------------


@api.get("/settings")
async def get_settings(admin: Admin, db: Db) -> dict[str, Any]:
    cfg = await settings.load(db)
    await db.rollback()
    return cfg.as_json()


@api.put("/settings")
async def put_settings(admin: Admin, db: Db, request: Request) -> dict[str, Any]:
    try:
        raw = json.loads(await request.body())
    except (ValueError, RecursionError):
        raise _err("invalid_request") from None
    try:
        new = settings.parse(raw)
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        raise _err("invalid_settings", fields=fields) from None
    old = await settings.load(db)
    if new.main_enabled and not old.main_enabled:
        main = (await tg_store.channels(db)).get("main")
        if main is None or not isinstance(main.get("chat_id"), int):
            raise _err("main_channel_not_set")
    await settings.save(db, new)
    changed = {k: [v, new.as_json()[k]] for k, v in old.as_json().items() if new.as_json()[k] != v}
    await log_action(
        db,
        actor=_actor(admin),
        action="posts.settings_set",
        target="posting",
        dry_run=False,
        allowed=True,
        reasons=["main_enabled"] if new.main_enabled and not old.main_enabled else [],
        context={"changed": changed},
    )
    await db.commit()
    return new.as_json()


# --- content library -----------------------------------------------------------------------------


@api.get("/content")
async def get_content(
    admin: Admin,
    db: Db,
    status: Annotated[str | None, Query(pattern="^(draft|approved|used|retired)$")] = None,
) -> dict[str, Any]:
    items = await content.list_items(db, status)
    counts = await content.counts(db)
    await db.rollback()
    return {"items": [i.as_json() for i in items], "counts": counts}


def _checked(value: dict[str, Any]) -> content.ContentIn:
    try:
        return content.check_input(value.get("title"), value.get("body"), value.get("tags"))
    except ContentInvalid as exc:
        raise _err(exc.code) from None


@api.post("/content", status_code=201)
async def create_content(admin: Admin, db: Db, request: Request) -> dict[str, Any]:
    value = await _body(request, {"title", "body", "tags", "status"}, {"title", "body"})
    data = _checked(value)
    status = value.get("status", "draft")
    if status not in ("draft", "approved"):
        raise _err("status_invalid")
    item = await content.create_item(db, data, status, tg_store.now_utc())
    await log_action(
        db,
        actor=_actor(admin),
        action="posts.content_created",
        target=f"content:{item.id}",
        dry_run=False,
        allowed=True,
        context={"status": item.status, "title": item.title[:60]},
    )
    await db.commit()
    return item.as_json()


@api.put("/content/{item_id}")
async def update_content(admin: Admin, db: Db, request: Request, item_id: int) -> dict[str, Any]:
    value = await _body(request, {"title", "body", "tags"}, {"title", "body"})
    data = _checked(value)
    item = await content.update_item(db, item_id, data, tg_store.now_utc())
    if item is None:
        raise _err("content_not_found")
    await log_action(
        db,
        actor=_actor(admin),
        action="posts.content_updated",
        target=f"content:{item.id}",
        dry_run=False,
        allowed=True,
        context={"title": item.title[:60]},
    )
    await db.commit()
    return item.as_json()


@api.post("/content/{item_id}/status")
async def set_content_status(admin: Admin, db: Db, request: Request, item_id: int) -> dict[str, Any]:
    value = await _body(request, {"status"}, {"status"})
    target = value["status"]
    if not isinstance(target, str) or target not in content.OWNER_STATUSES:
        raise _err("status_invalid")
    item, before = await content.set_status(db, item_id, target, tg_store.now_utc())
    if item is None:
        raise _err("content_not_found")
    if before != item.status:
        await log_action(
            db,
            actor=_actor(admin),
            action="posts.content_status",
            target=f"content:{item.id}",
            dry_run=False,
            allowed=True,
            context={"from": before, "to": item.status},
        )
    await db.commit()
    return item.as_json()


router = APIRouter()
router.include_router(api)
