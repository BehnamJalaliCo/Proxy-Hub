"""The live fleet (team fleet, round 3): the 9 slots + 1 spare for the dashboard's nodes page.

GET /api/fleet/slots is read-only: per slot the node its DNS name follows, a replacement on its
way, the nodes leaving, the fleet loop's latest decision; the fleet's mode (live, dry-run,
stopped), the burned-IP counts and the active owner alerts. Only our own servers' addresses
appear (rule 7 is about users); nothing secret.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiRoute, db_session, require_admin
from proxyhub.fleet.slots import slots_view

router = APIRouter(prefix="/api", tags=["fleet"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]


@router.get("/fleet/slots")
async def fleet_slots(admin: Admin, db: Db) -> dict[str, Any]:
    return await slots_view(db, datetime.now(UTC))
