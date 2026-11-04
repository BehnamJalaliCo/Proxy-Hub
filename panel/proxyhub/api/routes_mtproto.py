"""MTProto proxies on the nodes (owner only).

GET /api/mtproto            {settings, defaults, active_domain, proxies: [...]}
PUT /api/mtproto/settings   {port, tls_domain, mode_tls, mode_secure, mode_classic} -> {settings}

Each proxy: key, node (id, name, state, test flag), slot, Persian label, host (slot DNS name or the
node's IPv4) and port, link mode, the public link (the owner publishes it on purpose: the only place
a secret leaves the panel, rule 3), status, aggregate counters (current connections, total bytes;
never an end-user IP, rule 7), whether a sponsor tag is set (never the tag itself).

Errors are ``{"error", "message_fa"}``; input values are never echoed back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.cloudflare.rotation import current_active_domain
from proxyhub.fleet.actions import iso, log_action
from proxyhub.mtproto.keys import SealKeyUnavailable, load_proxy_seal_key
from proxyhub.mtproto.service import ProxyView, list_proxies
from proxyhub.mtproto.settings import (
    MtprotoSettings,
    SettingsCorrupt,
    load_settings,
    load_settings_for_update,
    save_settings,
    settings_diff,
    settings_json,
)

router = APIRouter(prefix="/api/mtproto", tags=["mtproto"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

MESSAGES_FA = {
    "invalid_settings": "تنظیمات پذیرفته نشد. پورت، دامنه‌ی TLS و دست‌کم یک حالت را بررسی کنید.",
    "mtproto_key_unavailable": "کلید اصلی پنل در دسترس نیست؛ لینک پروکسی‌ها ساخته نمیشود.",
}
FIELDS = frozenset({"port", "tls_domain", "mode_tls", "mode_secure", "mode_classic"})


def _error(code: str, status: int, extra: dict[str, Any] | None = None) -> ApiError:
    return ApiError(code, status_code=status, message_fa=MESSAGES_FA[code], extra=extra)


def _settings_out(s: MtprotoSettings) -> dict[str, Any]:
    return {**settings_json(s), "link_mode": s.link_mode}


def _proxy_out(v: ProxyView) -> dict[str, Any]:
    return {
        "key": v.key,
        "node_id": str(v.node_id),
        "node_name": v.node_name,
        "node_state": v.node_state,
        "is_test": v.is_test,
        "slot": v.slot,
        "label": v.label,
        "host": v.host,
        "host_kind": v.host_kind,
        "port": v.port,
        "mode": v.mode,
        "tls_domain": v.tls_domain,
        "link": v.url,
        "status": v.status,
        "connections": v.connections,
        "total_octets": v.total_octets,
        "stats_at": iso(v.stats_at),
        "live_at": iso(v.live_at),
        "sponsor_tag_set": v.sponsor_tag_set,
    }


def _invalid_fields(exc: ValidationError) -> list[str]:
    """Field names only (never the submitted values). A model-level error is the "no mode on" rule."""
    out: set[str] = set()
    for err in exc.errors():
        loc = err.get("loc") or ()
        if not loc:
            out.add("modes")
        else:
            out.add(str(loc[0]) if str(loc[0]) in FIELDS else "unknown")
    return sorted(out)


@router.get("")
async def get_mtproto(admin: Admin, db: Db) -> dict[str, Any]:
    try:
        seal_key = load_proxy_seal_key()
    except SealKeyUnavailable:
        raise _error("mtproto_key_unavailable", 503) from None
    settings: dict[str, Any] | None
    try:
        settings = _settings_out(await load_settings(db))
    except SettingsCorrupt:
        settings = None
    views = await list_proxies(db, seal_key, datetime.now(UTC))
    domain = await current_active_domain(db)
    await db.rollback()
    return {
        "settings": settings,
        "settings_corrupt": settings is None,
        "defaults": _settings_out(MtprotoSettings()),
        "active_domain": domain,
        "proxies": [_proxy_out(v) for v in views],
    }


@router.put("/settings")
async def put_mtproto_settings(admin: Admin, db: Db, payload: Annotated[Any, Body()]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _error("invalid_settings", 422, {"fields": ["body"]})
    try:
        new = MtprotoSettings.model_validate(payload)
    except ValidationError as exc:
        raise _error("invalid_settings", 422, {"fields": _invalid_fields(exc)}) from None
    old = await load_settings_for_update(db)
    await save_settings(db, new)
    changed = settings_diff(old, new)
    await log_action(
        db,
        actor=f"admin:{admin.username}",
        action="settings_update",
        target="mtproto",
        dry_run=False,
        allowed=True,
        reasons=sorted(changed),
        context={"changed": changed},
        result=f"{len(changed)} MTProto setting(s) changed; the mtproto loop applies them to the nodes",
    )
    await db.commit()
    return {"settings": _settings_out(new)}
