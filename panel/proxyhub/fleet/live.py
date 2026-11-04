"""The live fleet: what the fleet loop DOES once the owner turned dry-run off (decision D14).

The fleet loop never talks to Hetzner. In live mode it only changes rows, in its own transaction:

* :func:`mark_burned_nodes`: a slot node whose fresh probe verdict says *blocked* (both sources,
  at least two rounds; proxyhub/probes/verdict.py) becomes ``burned``; its IP and /24 go into
  ``ip_history`` (proxyhub/fleet/ip_history.py).
* :func:`apply_plan`: every ALLOWED action of the plan (proxyhub/fleet/planner.py, which evaluated
  it through ``proxyhub.safety.gates``):
  - ``create`` -> a ``requested`` slot node. The node executor (proxyhub/nodes/executor.py) creates
    the server only after the gates allow it AGAIN, on a live look at the account, with a
    ``CreatePermit``; caps (budget, node count, creations per hour) are never bypassed.
  - ``delete`` -> the node becomes ``retiring``; the executor deletes the server only through
    ``can_delete`` on a LIVE read of it and a ``DeletePermit`` (label managed-by=proxy-hub, role,
    our row with the same id and account, delete protection off; CLAUDE.md rule 5).

Every change is locked (``SELECT ... FOR UPDATE``), re-checked against the state the plan saw,
recorded in ``node_events`` and ``actions_log``. Nothing secret is involved anywhere here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.db.models import Node
from proxyhub.db.models_live import NodeOperation
from proxyhub.db.models_net import ProbeNodeState
from proxyhub.fleet.actions import iso, log_action
from proxyhub.fleet.ip_history import mark_burned, public_ipv4
from proxyhub.fleet.lifecycle import ALLOWED, NodeState
from proxyhub.fleet.planner import VERDICT_MAX_AGE, FleetPlan, FleetSnapshot, PlannedAction, verdict_blocked
from proxyhub.nodes.transitions import ensure_operation, event, lock_node, transition

ACTOR = "fleet"
S = NodeState
FLEET_NODE_PREFIX = "ph-s"


def fleet_node_name(slot: int, node_id: uuid.UUID) -> str:
    """Unique in its Hetzner project (the executor finds a server again by name and label)."""
    return f"{FLEET_NODE_PREFIX}{slot}-{node_id.hex[:8]}"


@dataclass
class Applied:
    requested: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# --- burned nodes ---------------------------------------------------------------------------


async def mark_burned_nodes(s: AsyncSession, snap: FleetSnapshot, now: datetime) -> list[str]:
    """READY slot nodes with a fresh ``blocked`` verdict -> ``burned`` (+ ip_history). Returns names."""
    burned: list[str] = []
    for n in snap.nodes:
        if n.is_test or n.slot is None or not verdict_blocked(n, now):
            continue
        node_id = uuid.UUID(n.id)
        row = await lock_node(s, node_id)
        if row is None or row.state != S.READY.value or row.is_test:
            continue
        # The probes loop may have written a newer verdict since the snapshot: read it again.
        verdict = (
            await s.execute(
                select(ProbeNodeState.verdict, ProbeNodeState.verdict_at).where(
                    ProbeNodeState.node_id == node_id
                )
            )
        ).first()
        if (
            verdict is None
            or verdict.verdict != "blocked"
            or verdict.verdict_at is None
            or now - verdict.verdict_at > VERDICT_MAX_AGE
        ):
            continue
        await transition(
            s, row, S.BURNED, now, reason="verdict_blocked", detail={"verdict_at": iso(verdict.verdict_at)}
        )
        row.burned_at = now
        ip = public_ipv4(row.ipv4)
        if ip is not None:
            await mark_burned(
                s, node_id=row.id, account_id=row.account_id, location=row.location, ip=ip, now=now
            )
        await log_action(
            s,
            actor=ACTOR,
            action="node_burned",
            target=row.name,
            dry_run=False,
            allowed=True,
            reasons=["verdict_blocked"],
            context={
                "node_id": str(row.id),
                "slot": row.slot,
                "location": row.location,
                "ipv4": str(ip) if ip else None,  # our server's address, never a user's
                "verdict_at": iso(verdict.verdict_at),
            },
            result=(
                "blocked from Iran by both probe sources: a replacement is built in another location first"
            ),
        )
        burned.append(row.name)
    if burned:
        await s.flush()
    return burned


# --- the plan -------------------------------------------------------------------------------


async def apply_plan(s: AsyncSession, plan: FleetPlan, now: datetime) -> Applied:
    """Turn the plan's allowed actions into rows (the executor does the Hetzner part)."""
    if plan.dry_run:
        raise PermissionError("a dry-run plan is never applied")
    done = Applied()
    for a in plan.actions:
        if not a.allowed:
            continue
        if a.action == "create":
            done.requested.append(await _request(s, a, now))
        elif a.action == "delete":
            await _retire(s, a, now, done)
    await s.flush()
    return done


async def _request(s: AsyncSession, a: PlannedAction, now: datetime) -> str:
    c = a.context
    slot = int(c["slot"])
    node_id = uuid.uuid4()
    name = fleet_node_name(slot, node_id)
    s.add(
        Node(
            id=node_id,
            account_id=uuid.UUID(str(c["account_id"])),
            name=name,
            slot=slot,
            location=str(c["location"]),
            server_type=str(c["server_type"]),
            state=S.REQUESTED.value,
            is_test=False,
            created_at=now,
            updated_at=now,
        )
    )
    await s.flush()
    s.add(NodeOperation(node_id=node_id, requested_by=ACTOR, requested_at=now, state_since=now))
    detail = {k: c.get(k) for k in ("account_name", "location", "server_type", "avoid")}
    event(s, node_id, now, "requested", by=ACTOR, slot=slot, need=c.get("kind"), **detail)
    await log_action(
        s,
        actor=ACTOR,
        action="node_request",
        target=name,
        dry_run=False,
        allowed=True,
        reasons=[str(c.get("kind") or "fill")],
        context={"node_id": str(node_id), **_public(c)},
        result=(
            f"slot {slot}: requested a server in {c['location']}; the node executor creates it only "
            "if the safety gates allow it again on a live look at the account"
        ),
    )
    return name


async def _retire(s: AsyncSession, a: PlannedAction, now: datetime, done: Applied) -> None:
    c = a.context
    why = str(c.get("why") or "")
    if why == "retiring":  # already on its way out: the executor deletes it
        return
    node_id = uuid.UUID(str(c["node_id"]))
    row = await lock_node(s, node_id)
    if row is None or row.is_test or row.state != c.get("state"):
        done.skipped.append(a.target)  # changed since the plan was made: the next pass decides again
        return
    state = S(row.state)
    if state == S.REQUESTED:  # nothing exists on Hetzner yet: withdraw the request
        await transition(s, row, S.DELETED, now, reason=f"fleet_{why}")
        result = "request withdrawn before any Hetzner call"
        done.withdrawn.append(row.name)
    elif S.RETIRING in ALLOWED[state]:
        await transition(s, row, S.RETIRING, now, reason=f"fleet_{why}")
        op = await ensure_operation(s, row, now)
        op.retire_requested_at, op.retire_reason = now, why
        result = "retiring; the node executor deletes the server only through the delete gate (live read)"
        done.retired.append(row.name)
    else:  # pragma: no cover - every live state may retire (lifecycle table)
        done.skipped.append(a.target)
        return
    await log_action(
        s,
        actor=ACTOR,
        action="node_retire",
        target=row.name,
        dry_run=False,
        allowed=True,
        reasons=[why],
        context={"node_id": str(row.id), "slot": row.slot, "from": state.value, "hetzner_id": row.hetzner_id},
        result=result,
    )


_PUBLIC_KEYS = (
    *("slot", "kind", "account_id", "account_name", "location", "server_type", "avoid"),
    *("price_monthly_eur", "rest_of_month_eur", "projected_month_eur"),
)


def _public(c: Mapping[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in _PUBLIC_KEYS}
