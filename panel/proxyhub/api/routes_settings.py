"""Owner settings for the fleet, and the kill switch.

Lost updates: GET answers the settings with ``updated_at`` (ISO 8601, ``null`` before the first
save). A PUT that carries ``expected_updated_at`` (that value, exactly as read) is refused with 409
``settings_changed`` when the stored settings changed since, so a stale tab cannot silently turn
dry-run off or raise a cap again. Without the field the PUT is saved as before. An ``updated_at``
in the PUT body (a form that sends back what it read) is ignored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, db_session, require_admin
from proxyhub.db.models import Setting
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.http import ApiError, ContractRoute, validation_fields
from proxyhub.fleet.settings_store import (
    FLEET_KEY,
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

SETTINGS_CHANGED_FA = (
    "تنظیمات در این فاصله جای دیگری (دستگاه یا زبانه‌ی دیگر) عوض شده است و ذخیره نشد. "
    "صفحه را تازه کنید و تغییر خود را دوباره بزنید."
)


class KillSwitchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on: StrictBool


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


async def _stamp(db: AsyncSession, lock: Literal["share", "update"] | None = None) -> datetime | None:
    """``updated_at`` of the fleet settings row (``None`` while there is none). A lock keeps the row
    as read until the transaction ends, so the stamp and the values read next belong together."""
    stmt = select(Setting.updated_at).where(Setting.key == FLEET_KEY)
    if lock is not None:
        stmt = stmt.with_for_update(read=lock == "share")
    return (await db.execute(stmt)).scalar_one_or_none()


def _split_stamps(payload: Any) -> tuple[Any, bool, datetime | None]:
    """The settings body without the stamp fields; whether ``expected_updated_at`` was sent, and
    its value."""
    if not isinstance(payload, dict):
        return payload, False, None
    body = {k: v for k, v in payload.items() if k not in ("updated_at", "expected_updated_at")}
    if "expected_updated_at" not in payload:
        return body, False, None
    raw = payload["expected_updated_at"]
    if raw is None:
        return body, True, None
    expected = None
    if isinstance(raw, str):
        try:
            expected = datetime.fromisoformat(raw)
        except ValueError:
            expected = None
    if expected is None or expected.tzinfo is None:
        raise ApiError(
            422, "invalid_settings", fields=[{"loc": ["expected_updated_at"], "type": "datetime_parsing"}]
        )
    return body, True, expected


@router.get("/settings/fleet")
async def get_fleet_settings(admin: Admin, db: Db) -> dict[str, Any]:
    stamp = await _stamp(db, lock="share")
    try:
        settings = await load_fleet_settings(db)
    except SettingsCorrupt:
        raise ApiError(500, "settings_corrupt") from None
    finally:
        await db.commit()  # releases the share lock
    return {**settings_json(settings), "updated_at": _iso(stamp)}


@router.put("/settings/fleet")
async def put_fleet_settings(admin: Admin, db: Db, payload: Annotated[Any, Body()]) -> dict[str, Any]:
    body, check_stamp, expected = _split_stamps(payload)
    try:
        new = FleetSettingsInput.model_validate(body).to_settings()
    except ValidationError as exc:
        raise ApiError(422, "invalid_settings", fields=validation_fields(exc.errors())) from None
    if not new.dry_run and not FLEET_LIVE_ENABLED:
        raise ApiError(422, "live_mode_locked")
    stamp = await _stamp(db, lock="update")
    if check_stamp and stamp != expected:
        # Saving would put back the values the form read (dry-run off, an older cap) over a newer
        # change made on another device or tab. Only the kill switch was protected before.
        raise ApiError(409, "settings_changed", message_fa=SETTINGS_CHANGED_FA)
    try:
        old = await load_fleet_settings_for_update(db)
    except SettingsCorrupt:
        old = None  # a full, valid replacement repairs corrupt settings
    # The kill switch is changed ONLY by POST /api/kill-switch: a stale settings form (another
    # device, an old tab) must never turn it back off. Corrupt settings keep it on (fail closed).
    new = new.model_copy(update={"kill_switch": old.kill_switch if old is not None else True})
    if old is not None and isinstance(body, dict) and "location_max" not in body:
        # A form that does not know the per-location caps keeps them (it must not lift the owner's
        # "one server in the USA" by saving another field).
        new = new.model_copy(update={"location_max": old.location_max})
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
    saved_at = await _stamp(db)
    await db.commit()
    return {**settings_json(new), "updated_at": _iso(saved_at)}


@router.post("/kill-switch")
async def set_kill_switch(admin: Admin, db: Db, body: KillSwitchIn) -> dict[str, Any]:
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
    saved_at = await _stamp(db)
    await db.commit()
    # The settings row changed, so a form holding the old updated_at would get 409 on its next
    # save: the new stamp lets the dashboard keep its copy current.
    return {"kill_switch": body.on, "updated_at": _iso(saved_at)}
