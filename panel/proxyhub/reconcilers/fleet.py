"""Fleet control loop (decision D14: 9 slots + 1 spare).

Every 60 s: build the plan for the 9 + 1 slot model from the database and the last live Hetzner
observations, evaluate every intended action through the safety gates, store the plan for the
dashboard and record the intended actions in ``actions_log``.

**Dry-run** (the default, and whenever the kill switch is on): that is all; every row says
``dry_run = true`` and nothing changes anywhere.

**Live** (the owner turned dry-run off in the dashboard, the kill switch is off, the stored
settings are valid): the loop also (proxyhub/fleet/live.py)

1. marks slot nodes that Iran blocks (fresh probe verdict ``blocked``: TLS failed from MCI and
   Rightel in two rounds while the control worked) ``burned``
   and remembers their IP and /24 (``ip_history``),
2. applies the plan's ALLOWED actions as rows only: a ``requested`` slot node for each allowed
   creation, ``retiring`` for each allowed deletion. The node executor (proxyhub/nodes/executor.py)
   then creates or deletes the server, through the gates AGAIN on a live look and with permits,
3. tells the owner (Persian) when a cap blocks a needed action (proxyhub/fleet/alerts.py).

This module never touches ``proxyhub.hetzner.mutations``. ``FLEET_LIVE_ENABLED`` stays as a
build-time emergency lock: set to False, every plan is dry-run whatever the settings say.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_fleet import FleetPlan as FleetPlanRow
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.alerts import findings, raise_alerts
from proxyhub.fleet.live import Applied, apply_plan, mark_burned_nodes
from proxyhub.fleet.planner import FleetPlan, PlannedAction, build_plan
from proxyhub.fleet.snapshot import load_snapshot
from proxyhub.reconcilers.base import Reconciler
from proxyhub.settings_schema import FleetSettings

# The live fleet exists in this build; the owner's ``dry_run`` setting decides at run time.
FLEET_LIVE_ENABLED: Final = True

# Unchanged plans are re-logged at most hourly, so the event log shows intent without repeating
# the same lines 60 times an hour.
LOG_REFRESH = timedelta(hours=1)
PLAN_RETENTION = timedelta(days=1)
# "none" actions worth an event line (the others only appear in the stored plan).
NOTABLE_NONE = frozenset({"orphan_server_not_in_db", "slot_unhealthy", "request_backoff"})


class LiveModeDisabled(PermissionError):
    """Raised by the apply path for a dry-run plan or while FLEET_LIVE_ENABLED is False."""


def live_mode(settings: FleetSettings, settings_problem: str | None = None) -> bool:
    """Live only when the build allows it, the owner turned dry-run off, the kill switch is off
    and the stored settings are valid."""
    return FLEET_LIVE_ENABLED and not settings.dry_run and not settings.kill_switch and not settings_problem


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
            select(FleetPlanRow.fingerprint, FleetPlanRow.generated_at, FleetPlanRow.dry_run)
            .where(FleetPlanRow.logged.is_(True))
            .order_by(FleetPlanRow.generated_at.desc(), FleetPlanRow.id.desc())
            .limit(1)
        )
    ).first()
    should_log = (
        last is None
        or last.fingerprint != fingerprint
        or last.dry_run != plan.dry_run
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


async def execute_plan(conn: AsyncConnection, plan: FleetPlan) -> Applied:
    """Apply a LIVE plan's allowed actions as rows (never a Hetzner call; the executor does those)."""
    if not FLEET_LIVE_ENABLED or plan.dry_run:
        raise LiveModeDisabled("a dry-run plan is never applied")
    async with AsyncSession(bind=conn, expire_on_commit=False) as s:
        return await apply_plan(s, plan, plan.generated_at)


class FleetReconciler(Reconciler):
    name = "fleet"
    interval_seconds = 60.0

    async def run_once(self, conn: AsyncConnection) -> None:
        now = datetime.now(UTC)
        snap = await load_snapshot(conn, now)
        live = live_mode(snap.settings, snap.settings_problem)
        if live:
            async with AsyncSession(bind=conn, expire_on_commit=False) as s:
                burned = await mark_burned_nodes(s, snap, now)
            if burned:
                snap = await load_snapshot(conn, now)
        plan = build_plan(snap, dry_run=not live)
        await record_plan(conn, plan)
        if live:
            await execute_plan(conn, plan)
            await raise_alerts(conn, findings(plan, snap), now)
