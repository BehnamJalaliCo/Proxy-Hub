"""VLESS Reality (decision D18): GET /api/reality (configs, nodes, settings) and
PUT /api/reality/settings (cap hours/GB, configs per node, target, link name, on/off).

A new ``target_host`` whose name Xray warns about (proxyhub/targets/risky.py) is refused with its own
message_fa. One already stored is accepted unchanged, so the owner can still save other fields: in
``auto`` mode the targets loop replaces it, and no config is ever made on it
(proxyhub/reality/policy.py)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.fleet.actions import log_action
from proxyhub.reality import service
from proxyhub.reality.settings import RealitySettings, load_reality_settings, save_reality_settings
from proxyhub.targets.risky import RISKY_RULE_FA, risky_sni

router = APIRouter(prefix="/api/reality", tags=["reality"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

INVALID_FA = "تنظیمات Reality درست نیست؛ عددها و نام سایت هدف را بررسی کنید."
RISKY_FA = f"سایت هدف Reality پذیرفته نشد. {RISKY_RULE_FA} سایت دیگری بگذارید یا «خودکار» را بزنید."


@router.get("")
async def reality_summary(admin: Admin, db: Db) -> dict[str, Any]:
    return await service.summary(db)


@router.put("/settings")
async def put_reality_settings(admin: Admin, db: Db, payload: Annotated[Any, Body()]) -> dict[str, Any]:
    try:
        new = RealitySettings.model_validate(payload)
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        raise ApiError(
            "invalid_settings", status_code=422, message_fa=INVALID_FA, extra={"fields": fields}
        ) from None
    old = await load_reality_settings(db)
    if new.target_host != old.target_host and risky_sni(new.target_host):
        raise ApiError(
            "invalid_settings", status_code=422, message_fa=RISKY_FA, extra={"fields": ["target_host"]}
        )
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    changed = {k: [a[k], b[k]] for k in b if a.get(k) != b[k]}
    await save_reality_settings(db, new)
    await log_action(
        db,
        actor=f"admin:{admin.username}",
        action="settings_update",
        target="reality",
        dry_run=False,
        allowed=True,
        reasons=sorted(changed),
        context={"changed": changed},
        result=f"{len(changed)} reality setting(s) changed",
    )
    await db.commit()
    return service.settings_json(new)
