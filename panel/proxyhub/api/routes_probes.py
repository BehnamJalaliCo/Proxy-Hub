"""Reachability from Iran by TLS handshakes (docs/10 M4.1, P15): GET /api/probes/summary (per node
and network, plus the location x network table the fleet plans with), POST /api/probes/run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.fleet.locations import locations_summary
from proxyhub.probes import service

router = APIRouter(prefix="/api/probes", tags=["probes"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

MESSAGES_FA: dict[str, str] = {
    "not_found": "این node پیدا نشد یا پیش‌تر حذف شده است.",
    "node_has_no_ip": "این node هنوز IP ندارد؛ سنجش از ایران پس از ساخته شدن سرورش ممکن است.",
}
STATUS: dict[str, int] = {"not_found": 404, "node_has_no_ip": 409}


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: uuid.UUID


@router.get("/summary")
async def probes_summary(admin: Admin, db: Db) -> dict[str, Any]:
    body = await service.summary(db)
    body["locations"] = await locations_summary(db, datetime.now(UTC))
    return body


@router.post("/run", status_code=202)
async def probes_run(admin: Admin, body: RunIn, db: Db) -> dict[str, Any]:
    try:
        await service.request_run(db, body.node_id, actor=f"admin:{admin.username}", now=datetime.now(UTC))
    except service.ProbeServiceError as exc:
        await db.rollback()
        raise ApiError(exc.code, status_code=STATUS[exc.code], message_fa=MESSAGES_FA[exc.code]) from None
    return {}
