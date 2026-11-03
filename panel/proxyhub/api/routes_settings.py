"""Owner settings for the fleet, and the kill switch."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, db_session, require_admin
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.http import ApiError, ContractRoute, validation_fields
from proxyhub.fleet.settings_store import (
    FleetSettingsInput,
    SettingsCorrupt,
    load_fleet_settings,
    load_fleet_settings_for_update,
    save_fleet_settings,
    settings_diff,
    settings_json,
)
from proxyhub.reconcilers.fleet import FLEET_LIVE_ENABLED
from proxyhub.settings_schema import FleetSettings

router = APIRouter(prefix="/api", tags=["settings"], route_class=ContractRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool


@router.get("/settings/fleet")
async def get_fleet_settings(admin: Admin, db: Db) -> dict[str, Any]:
    try:
        return settings_json(await load_fleet_settings(db))
    except SettingsCorrupt:
        raise ApiError(500, "settings_corrupt") from None


@router.put("/settings/fleet")
async def put_fleet_settings(admin: Admin, db: Db, payload: Annotated[Any, Body()]) -> dict[str, Any]:
    try:
        new = FleetSettingsInput.model_validate(payload).to_settings()
    except ValidationError as exc:
        raise ApiError(422, "invalid_settings", fields=validation_fields(exc.errors())) from None
    if not new.dry_run and not FLEET_LIVE_ENABLED:
        raise ApiError(422, "live_mode_locked")
    try:
        old = await load_fleet_settings_for_update(db)
    except SettingsCorrupt:
        old = None  # a full, valid replacement repairs corrupt settings
    await save_fleet_settings(db, new)
    changed = settings_diff(old, new) if old is not None else {"*": ["corrupt", "replaced"]}
    await log_action(
        db,
        actor=f"admin:{admin.username}",
        action="settings_update",
        target="fleet",
        dry_run=False,
        allowed=True,
        reasons=sorted(changed),
        context={"changed": changed},
        result=f"{len(changed)} fleet setting(s) changed",
    )
    await db.commit()
    return settings_json(new)


@router.post("/kill-switch")
async def set_kill_switch(admin: Admin, db: Db, body: KillSwitchIn) -> dict[str, bool]:
    try:
        current = await load_fleet_settings_for_update(db)
    except SettingsCorrupt:
        if not body.on:
            raise ApiError(500, "settings_corrupt") from None
        current = FleetSettings()  # stopping must always work: safe defaults (no caps, switch on)
    previous = current.kill_switch
    await save_fleet_settings(db, current.model_copy(update={"kill_switch": body.on}))
    await log_action(
        db,
        actor=f"admin:{admin.username}",
        action="kill_switch_on" if body.on else "kill_switch_off",
        target="fleet",
        dry_run=False,
        allowed=True,
        reasons=["owner_request"],
        context={"previous": previous, "now": body.on},
        result=f"kill switch {'ON' if body.on else 'OFF'}",
    )
    await db.commit()
    return {"kill_switch": body.on}
