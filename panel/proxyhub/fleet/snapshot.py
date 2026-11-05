"""Load the planner's snapshot from the database (rows + the last live Hetzner observations)."""

from __future__ import annotations

import ipaddress
from datetime import datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.accounts.hetzner_io import LiveOffer
from proxyhub.db.models import HetznerAccount, Node
from proxyhub.db.models_fleet import AccountObservation, AccountServer, HetznerOffer
from proxyhub.db.models_live import NodeOperation
from proxyhub.db.models_net import ProbeNodeState
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.fleet.locations import load_location_reach, location_classes
from proxyhub.fleet.planner import CREATION_WINDOW, AccountOffer, AccountSnapshot, FleetSnapshot, NodeSnapshot
from proxyhub.fleet.settings_store import SettingsCorrupt, load_fleet_settings
from proxyhub.nodes.settings import get_live_settings
from proxyhub.probes.settings import load_probe_settings
from proxyhub.safety.budget import Price, month_bounds
from proxyhub.safety.gates import ServerView
from proxyhub.settings_schema import FleetSettings

Db = AsyncConnection | AsyncSession


async def load_accounts(db: Db) -> tuple[AccountSnapshot, ...]:
    rows = (
        await db.execute(
            select(
                HetznerAccount.id,
                HetznerAccount.name,
                HetznerAccount.state,
                HetznerAccount.created_at,
                HetznerAccount.learned_server_limit,
                AccountObservation.last_ok_at,
                AccountObservation.guard_ok,
                AccountObservation.servers_total,
            ).outerjoin(AccountObservation, AccountObservation.account_id == HetznerAccount.id)
        )
    ).all()
    return tuple(
        AccountSnapshot(
            id=str(r.id),
            name=r.name,
            state=r.state,
            created_at=r.created_at,
            learned_server_limit=r.learned_server_limit,
            observed_at=r.last_ok_at,
            guard_ok=r.guard_ok,
            servers_total=r.servers_total,
        )
        for r in rows
    )


def _ip_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(ipaddress.ip_interface(str(value)).ip)
    except ValueError:
        return None


async def load_nodes(db: Db, now: datetime) -> tuple[NodeSnapshot, ...]:
    """Live nodes, plus deleted ones that still matter (billed this month, or created within the hour),
    with the executor's bookkeeping (node_operations) and the latest probe verdict from Iran."""
    month_start, _ = month_bounds(now)
    nodes = Node.__table__
    ops = NodeOperation.__table__
    verdicts = ProbeNodeState.__table__
    # Table-level selects return plain rows on both AsyncConnection (loops) and AsyncSession (API).
    rows = (
        await db.execute(
            select(
                nodes,
                ops.c.state_since,
                ops.c.retire_reason,
                ops.c.create_attempted_at,
                ops.c.node_id.label("op_node_id"),
                verdicts.c.verdict,
                verdicts.c.verdict_at,
            )
            .outerjoin(ops, ops.c.node_id == nodes.c.id)
            .outerjoin(verdicts, verdicts.c.node_id == nodes.c.id)
            .where(
                or_(
                    nodes.c.state != NodeState.DELETED.value,
                    nodes.c.deleted_at.is_(None),
                    nodes.c.deleted_at >= month_start,
                    nodes.c.created_at >= now - CREATION_WINDOW,
                )
            )
        )
    ).all()
    return tuple(
        NodeSnapshot(
            id=str(n.id),
            name=n.name,
            account_id=str(n.account_id),
            server_type=n.server_type,
            location=n.location,
            state=NodeState(n.state),
            hetzner_id=n.hetzner_id,
            created_at=n.created_at,
            deleted_at=n.deleted_at,
            slot=n.slot,
            is_test=n.is_test,
            ready_at=n.ready_at,
            verdict=n.verdict,
            verdict_at=n.verdict_at,
            state_since=n.state_since,
            retire_reason=n.retire_reason,
            ipv4=_ip_text(n.ipv4),
            # A node without executor bookkeeping is counted as a creation (conservative).
            create_attempted=(
                n.op_node_id is None or n.create_attempted_at is not None or n.hetzner_id is not None
            ),
        )
        for n in rows
    )


async def load_offers(db: Db) -> tuple[AccountOffer, ...]:
    rows = (await db.execute(select(HetznerOffer.__table__))).all()
    return tuple(
        AccountOffer(
            account_id=str(o.account_id),
            offer=LiveOffer(
                server_type=o.server_type,
                location=o.location,
                price=Price(hourly=Decimal(o.price_hourly_net), monthly=Decimal(o.price_monthly_net)),
                included_traffic_bytes=o.included_traffic_bytes,
                deprecated=o.deprecated,
                available=o.available,
            ),
            fetched_at=o.fetched_at,
        )
        for o in rows
    )


async def load_servers(db: Db) -> dict[tuple[str, int], ServerView]:
    rows = (await db.execute(select(AccountServer.__table__))).all()
    return {
        (str(s.account_id), int(s.hetzner_id)): ServerView(
            hetzner_id=int(s.hetzner_id),
            name=s.name,
            labels={str(k): str(v) for k, v in dict(s.labels).items()},
            delete_protected=s.delete_protected,
        )
        for s in rows
    }


async def load_snapshot(db: Db, now: datetime) -> FleetSnapshot:
    problem: str | None = None
    try:
        settings = await load_fleet_settings(db)
    except SettingsCorrupt:
        settings, problem = FleetSettings(), "settings_invalid"  # defaults block every creation
    probes = await load_probe_settings(db)
    reach = await load_location_reach(db, now)
    classes = location_classes(reach, settings.locations, probes.key_networks)
    return FleetSnapshot(
        now=now,
        settings=settings,
        accounts=await load_accounts(db),
        nodes=await load_nodes(db, now),
        offers=await load_offers(db),
        servers=await load_servers(db),
        settings_problem=problem,
        blockers=() if get_live_settings().checked_agent_domain() else ("agent_domain_not_set",),
        location_classes=classes,
    )
