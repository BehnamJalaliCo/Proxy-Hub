"""Fleet control loop, DRY-RUN ONLY in this milestone (M1.5).

Every 60 s: build the plan for the 9 + 1 slot model from the database and the last live Hetzner
observations, evaluate every intended action through the safety gates, store the plan for the
dashboard and record the intended actions in ``actions_log`` with ``dry_run = true``.

This module never touches ``proxyhub.hetzner.mutations``. ``FLEET_LIVE_ENABLED`` is checked
before any mutation path; switching it on is a separate, later owner decision (a test fails while
it is True in this milestone).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.db.models_fleet import FleetPlan as FleetPlanRow
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.planner import FleetPlan, PlannedAction, build_plan
from proxyhub.fleet.snapshot import load_snapshot
from proxyhub.reconcilers.base import Reconciler
from proxyhub.settings_schema import FleetSettings

FLEET_LIVE_ENABLED: Final = False

# Unchanged plans are re-logged at most hourly, so the event log shows intent without repeating
# the same lines 60 times an hour.
LOG_REFRESH = timedelta(hours=1)
PLAN_RETENTION = timedelta(days=1)
# "none" actions worth an event line (the others only appear in the stored plan).
NOTABLE_NONE = frozenset({"orphan_server_not_in_db", "slot_unhealthy"})


class LiveModeDisabled(PermissionError):
    """Raised by any mutation path while FLEET_LIVE_ENABLED is False."""


def live_mode(settings: FleetSettings) -> bool:
    return FLEET_LIVE_ENABLED and not settings.dry_run


def _event_action(a: PlannedAction) -> str | None:
    if a.action == "create":
        return "node_create"
    if a.action == "delete":
        return "node_delete"
    if a.reasons and a.reasons[0] in NOTABLE_NONE:
        return a.reasons[0]
    return None


async def record_plan(conn: AsyncConnection, plan: FleetPlan) -> bool:
    """Store the plan; write its actions to actions_log when it changed (or hourly). Returns 'logged'."""
    fingerprint = plan.fingerprint()
    last = (
        await conn.execute(
            select(FleetPlanRow.fingerprint, FleetPlanRow.generated_at)
            .where(FleetPlanRow.logged.is_(True))
            .order_by(FleetPlanRow.generated_at.desc(), FleetPlanRow.id.desc())
            .limit(1)
        )
    ).first()
    should_log = (
        last is None
        or last.fingerprint != fingerprint
        or plan.generated_at - last.generated_at >= LOG_REFRESH
    )
    if should_log:
        for a in plan.actions:
            name = _event_action(a)
            if name is None:
                continue
            await log_action(
                conn,
                actor="fleet",
                action=name,
                target=a.target,
                dry_run=plan.dry_run,
                allowed=a.allowed,
                reasons=a.reasons,
                context=a.context,
                result=a.summary(plan.dry_run),
            )
    new_id = (
        await conn.execute(
            insert(FleetPlanRow)
            .values(
                generated_at=plan.generated_at,
                dry_run=plan.dry_run,
                fingerprint=fingerprint,
                logged=should_log,
                actions=[a.stored_json() for a in plan.actions],
                summary=plan.summary(),
            )
            .returning(FleetPlanRow.id)
        )
    ).scalar_one()
    await conn.execute(
        delete(FleetPlanRow).where(
            FleetPlanRow.generated_at < plan.generated_at - PLAN_RETENTION, FleetPlanRow.id != new_id
        )
    )
    return should_log


async def execute_plan(conn: AsyncConnection, plan: FleetPlan) -> None:
    """The only path that could ever lead to a Hetzner mutation. Closed in this milestone."""
    if not FLEET_LIVE_ENABLED or plan.dry_run:
        raise LiveModeDisabled("fleet live mode is disabled in this milestone (owner decision pending)")
    raise NotImplementedError("live fleet execution arrives with milestone M2.5")  # pragma: no cover


class FleetReconciler(Reconciler):
    name = "fleet"
    interval_seconds = 60.0

    async def run_once(self, conn: AsyncConnection) -> None:
        now = datetime.now(UTC)
        snap = await load_snapshot(conn, now)
        live = live_mode(snap.settings)
        plan = build_plan(snap, dry_run=not live)
        await record_plan(conn, plan)
        if live:  # never true while FLEET_LIVE_ENABLED is False
            await execute_plan(conn, plan)
