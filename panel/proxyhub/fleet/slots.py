"""The 9 + 1 slots as the dashboard shows them (GET /api/fleet/slots): per slot the node its DNS
name points at, a replacement on its way, the nodes leaving, and the fleet loop's latest decision.

Read-only. Node addresses here are our own servers' (rule 7 is about users: nothing of theirs here).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.cloudflare.rotation import current_active_domain
from proxyhub.db.models_fleet import FleetAlert
from proxyhub.db.models_fleet import FleetPlan as FleetPlanRow
from proxyhub.fleet.actions import iso
from proxyhub.fleet.ip_history import burned_counts
from proxyhub.fleet.lifecycle import LIVE_STATES, NodeState
from proxyhub.fleet.planner import SICK, SPARE_SLOTS, STARTING, NodeSnapshot, verdict_blocked
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings
from proxyhub.fleet.snapshot import load_nodes
from proxyhub.settings_schema import FleetSettings

Db = AsyncConnection | AsyncSession
S = NodeState
Mode = Literal["live", "dry_run", "stopped"]
SlotStatus = Literal["healthy", "settling", "starting", "replacing", "unhealthy", "burned", "empty"]


def fleet_mode(settings: FleetSettings, problem: bool) -> Mode:
    from proxyhub.reconcilers.fleet import live_mode

    if problem or settings.kill_switch:
        return "stopped"
    return "live" if live_mode(settings) else "dry_run"


def brief(n: NodeSnapshot, now: datetime) -> dict[str, Any]:
    return {
        "id": n.id,
        "name": n.name,
        "state": n.state.value,
        "location": n.location,
        "server_type": n.server_type,
        "ipv4": n.ipv4,  # our server's address
        "verdict": n.verdict,
        "verdict_at": iso(n.verdict_at),
        "blocked": verdict_blocked(n, now) or n.state == S.BURNED,
        "created_at": iso(n.created_at),
        "ready_at": iso(n.ready_at),
        "state_since": iso(n.state_since),
        "retire_reason": n.retire_reason,
    }


def slot_status(
    nodes: list[NodeSnapshot], now: datetime
) -> tuple[SlotStatus, NodeSnapshot | None, NodeSnapshot | None]:
    """(status, the node the slot's DNS name follows, a replacement on its way)."""
    blocked = [n for n in nodes if n.state == S.BURNED or verdict_blocked(n, now)]
    serving = [n for n in nodes if n.state == S.READY and n not in blocked]
    starting = [n for n in nodes if n.state in STARTING]
    sick = [n for n in nodes if n.state in SICK]
    leaving = [n for n in nodes if n.state in (S.DRAINING, S.RETIRING)]
    if serving:
        keep = max(serving, key=lambda n: (n.ready_at or n.created_at, n.created_at, n.id))
        others = [n for n in nodes if n is not keep and n not in leaving]
        return ("settling" if others else "healthy"), keep, None
    holding = blocked + sick
    current = max(holding, key=lambda n: (n.ready_at or n.created_at, n.created_at)) if holding else None
    if starting:
        newest = max(starting, key=lambda n: (n.created_at, n.id))
        return ("replacing" if holding or leaving else "starting"), current, newest
    if sick:
        return "unhealthy", current, None
    if blocked:
        return "burned", current, None
    return "empty", None, None


async def slots_view(db: Db, now: datetime) -> dict[str, Any]:
    try:
        settings, problem = await load_fleet_settings(db), False
    except SettingsCorrupt:
        settings, problem = FleetSettings(), True
    nodes = [n for n in await load_nodes(db, now) if not n.is_test]
    live = [n for n in nodes if n.state in LIVE_STATES]
    zone = await current_active_domain(db)
    plan_row = (
        await db.execute(
            select(FleetPlanRow.generated_at, FleetPlanRow.dry_run, FleetPlanRow.actions)
            .order_by(FleetPlanRow.generated_at.desc(), FleetPlanRow.id.desc())
            .limit(1)
        )
    ).first()
    plan_by_target: dict[str, dict[str, Any]] = {}
    if plan_row is not None:
        for a in plan_row.actions:
            if isinstance(a, dict) and isinstance(a.get("target"), str):
                plan_by_target.setdefault(a["target"], a)

    slots: list[dict[str, Any]] = []
    for slot in range(1, settings.target_slots + 1):
        in_slot = [n for n in live if n.slot == slot]
        status, current, replacement = slot_status(in_slot, now)
        leaving = [n for n in in_slot if n.state in (S.DRAINING, S.RETIRING)]
        others = [n for n in in_slot if n not in (current, replacement) and n not in leaving]
        planned = plan_by_target.get(f"slot-{slot}")
        slots.append(
            {
                "slot": slot,
                "dns_name": f"slot{slot}.{zone}" if zone else None,
                "status": status,
                "node": brief(current, now) if current else None,
                "replacement": brief(replacement, now) if replacement else None,
                "others": [brief(n, now) for n in others],
                "leaving": [brief(n, now) for n in leaving],
                "plan": (
                    {
                        "action": planned.get("action"),
                        "allowed": bool(planned.get("allowed")),
                        "reasons": [str(r) for r in planned.get("reasons") or []],
                    }
                    if planned
                    else None
                ),
            }
        )
    in_slots = {n.id for n in live if n.slot is not None and 1 <= n.slot <= settings.target_slots}
    crowded = [s["slot"] for s in slots if sum(1 for n in live if n.slot == s["slot"]) > 1]
    burned_ips, burned_prefixes = await burned_counts(db)
    alerts = (
        await db.execute(
            select(FleetAlert.code, FleetAlert.since, FleetAlert.last_sent_at).where(
                FleetAlert.active.is_(True)
            )
        )
    ).all()
    return {
        "mode": fleet_mode(settings, problem),
        "target_slots": settings.target_slots,
        "spare_slots": SPARE_SLOTS,
        "node_cap_total": settings.node_cap_total,
        "live_fleet_nodes": len(live),
        "active_domain": zone,
        "plan_at": iso(plan_row.generated_at) if plan_row else None,
        "slots": slots,
        "spare": {"in_use": len(live) > settings.target_slots, "slots": crowded},
        "excess": [brief(n, now) for n in live if n.id not in in_slots],
        "burned_ips": burned_ips,
        "burned_prefixes": burned_prefixes,
        "alerts": [
            {"code": a.code, "since": iso(a.since), "last_sent_at": iso(a.last_sent_at)} for a in alerts
        ],
    }
