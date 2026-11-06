"""Candidate REALITY / fake-TLS sites measured from Iran (P15, owner only).

GET /api/targets            {settings, defaults, key_networks, reality: {mode, target, transport},
                             mtproto: {mode, tls_domain} | null, last_full_at, next_full_at,
                             candidates: [{host, listed, current_for, measured_at, networks,
                             prev_networks, open, blocked_on, ms, node_check, ...}]}
PUT /api/targets/settings   {candidates: [...], interval_h, enabled} -> the GET body

The ``auto`` / ``manual`` switch itself is part of the Reality and MTProto settings
(PUT /api/reality/settings, PUT /api/mtproto/settings: ``target_mode``). Errors are
``{"error", "message_fa"}``; input values are never echoed back. A candidate whose name Xray warns
about (proxyhub/targets/risky.py) is refused with its own message_fa that states the rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.fleet.actions import log_action
from proxyhub.probes.settings import load_probe_settings
from proxyhub.targets import service
from proxyhub.targets.risky import RISKY_RULE_FA
from proxyhub.targets.settings import (
    RISKY_ERROR,
    TargetSettings,
    load_target_settings,
    save_target_settings,
)

router = APIRouter(prefix="/api/targets", tags=["targets"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

INVALID_FA = "فهرست سایت‌های نامزد پذیرفته نشد: ۱ تا ۱۲ نام دامنه (نه IP)، و فاصله‌ی سنجش ۱ تا ۴۸ ساعت."
RISKY_FA = f"فهرست سایت‌های نامزد پذیرفته نشد. {RISKY_RULE_FA} آن نام را بردارید و دوباره ذخیره کنید."


async def _body(db: AsyncSession, settings: TargetSettings) -> dict[str, Any]:
    keys = (await load_probe_settings(db)).key_networks
    return await service.summary(db, settings, keys, datetime.now(UTC))


@router.get("")
async def get_targets(admin: Admin, db: Db) -> dict[str, Any]:
    body = await _body(db, await load_target_settings(db))
    await db.rollback()
    return body


@router.put("/settings")
async def put_target_settings(admin: Admin, db: Db, payload: Annotated[Any, Body()]) -> dict[str, Any]:
    try:
        new = TargetSettings.model_validate(payload)
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        risky = any(e["type"] == RISKY_ERROR for e in exc.errors())
        raise ApiError(
            "invalid_settings",
            status_code=422,
            message_fa=RISKY_FA if risky else INVALID_FA,
            extra={"fields": fields},
        ) from None
    old = await load_target_settings(db)
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    changed = {k: [a[k], b[k]] for k in b if a.get(k) != b[k]}
    await save_target_settings(db, new)
    await log_action(
        db,
        actor=f"admin:{admin.username}",
        action="settings_update",
        target="targets",
        dry_run=False,
        allowed=True,
        reasons=sorted(changed),
        context={"changed": changed},
        result=f"{len(changed)} target setting(s) changed; new sites are measured from Iran within minutes",
    )
    await db.commit()
    return await _body(db, new)
