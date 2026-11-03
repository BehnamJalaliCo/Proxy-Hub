"""Overview, the fleet's dry-run plan, and the event log (actions_log)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, db_session, require_admin
from proxyhub.db.models import ActionLog, HetznerAccount
from proxyhub.db.models_fleet import FleetPlan
from proxyhub.fleet.actions import iso, reason_list
from proxyhub.fleet.costs import money, node_cost_items, with_upper_bound_prices
from proxyhub.fleet.http import ApiError, ContractRoute
from proxyhub.fleet.lifecycle import LIVE_STATES
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings, settings_json
from proxyhub.fleet.snapshot import load_nodes, load_offers
from proxyhub.reconcilers.fleet import FLEET_LIVE_ENABLED
from proxyhub.safety.budget import projected_month_total

router = APIRouter(prefix="/api", tags=["overview"], route_class=ContractRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

PLAN_ACTION_KEYS = ("action", "target", "allowed", "reasons")


@router.get("/overview")
async def overview(admin: Admin, db: Db) -> dict[str, Any]:
    try:
        s = await load_fleet_settings(db)
    except SettingsCorrupt:
        raise ApiError(500, "settings_corrupt") from None
    now = datetime.now(UTC)
    nodes = await load_nodes(db, now)
    prices = {
        (ao.account_id, ao.offer.server_type, ao.offer.location): ao.offer.price
        for ao in await load_offers(db)
    }
    items, _ = node_cost_items(nodes, with_upper_bound_prices(nodes, prices), now)

    rows = (await db.execute(select(HetznerAccount.state, func.count()).group_by(HetznerAccount.state))).all()
    counts = {str(state): int(n) for state, n in rows}
    last_run = (await db.execute(select(func.max(FleetPlan.generated_at)))).scalar_one_or_none()
    heartbeat_age = (
        await db.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM (now() - updated_at)) FROM settings WHERE key = 'worker.heartbeat'"
            )
        )
    ).scalar_one_or_none()
    return {
        "kill_switch": s.kill_switch,
        "dry_run": s.dry_run or not FLEET_LIVE_ENABLED,
        "budget_cap_eur": settings_json(s)["budget_cap_eur"],
        "node_cap_total": s.node_cap_total,
        "target_slots": s.target_slots,
        "nodes_live": sum(1 for n in nodes if n.state in LIVE_STATES),
        "month_projected_eur": money(projected_month_total(items, now)),
        "accounts": {
            "active": int(counts.get("active", 0)),
            "locked": int(counts.get("locked", 0)),
            "total": int(sum(counts.values())),
        },
        "last_fleet_run_at": iso(last_run),
        "worker_heartbeat_age_s": round(float(heartbeat_age), 1) if heartbeat_age is not None else None,
    }


@router.get("/fleet/plan")
async def fleet_plan(admin: Admin, db: Db) -> dict[str, Any]:
    row = (
        await db.execute(
            select(FleetPlan.generated_at, FleetPlan.dry_run, FleetPlan.actions)
            .order_by(FleetPlan.generated_at.desc(), FleetPlan.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return {"generated_at": None, "dry_run": True, "actions": []}
    actions = [
        {
            "action": a.get("action"),
            "target": a.get("target"),
            "allowed": bool(a.get("allowed")),
            "reasons": reason_list(a.get("reasons")),
        }
        for a in row.actions
    ]
    return {"generated_at": iso(row.generated_at), "dry_run": bool(row.dry_run), "actions": actions}


@router.get("/events")
async def events(
    admin: Admin,
    db: Db,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[dict[str, Any]]:
    stmt = select(ActionLog).order_by(ActionLog.id.desc()).limit(limit)
    if before_id is not None:
        stmt = stmt.where(ActionLog.id < before_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "at": iso(r.at),
            "actor": r.actor,
            "action": r.action,
            "target": r.target,
            "dry_run": r.dry_run,
            "allowed": r.allowed,
            "reasons": reason_list(r.reasons),
            "result": r.result,
        }
        for r in rows
    ]
