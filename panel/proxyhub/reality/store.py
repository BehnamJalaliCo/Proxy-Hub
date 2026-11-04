"""Reality database access shared by the agent API, the policy loop, the links provider and the
dashboard API.

Key scopes: a node with a slot uses its slot's key (and the slot's configs), a node without one
(the owner's test node) its own. Every node of a scope serves all of the scope's active configs, so
a config posted as ``slot<N>.<domain>`` keeps working when slot N moves to a new server.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models import Node
from proxyhub.db.models_agent import TelemtNodeConfig
from proxyhub.db.models_reality import RealityConfig, RealityKey, RealityNodeStatus, RealityUsage
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.reality.settings import PORT
from proxyhub.vault import Vault

Db = AsyncConnection | AsyncSession
S = NodeState

# Nodes that keep serving their scope's configs (a burned or draining node until it is gone: its
# clients may still reach it, and the slot's replacement serves the same configs).
SERVE_STATES: Final = frozenset({S.READY, S.UNHEALTHY, S.REPAIRING, S.BURNED, S.DRAINING})
XRAY_API_PORT: Final = 10085  # node-image/files/agent-xray.json; Telemt may not use it
EMAIL_DOMAIN: Final = "proxyhub"
_EMAIL = re.compile(r"cfg-([1-9][0-9]{0,17})@proxyhub")


def config_email(config_id: int) -> str:
    """The Xray user label of a config: an id, never anything about a person."""
    return f"cfg-{config_id}@{EMAIL_DOMAIN}"


def config_id_from_email(email: str) -> int | None:
    m = _EMAIL.fullmatch(email)
    return int(m.group(1)) if m else None


def node_ipv4(value: object) -> str | None:
    """A node's IPv4 address as text (the column is INET), or None."""
    if value is None:
        return None
    try:
        ip = ipaddress.ip_interface(str(value)).ip
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


def is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class NodeView:
    id: uuid.UUID
    name: str
    slot: int | None
    state: str
    ipv4: str | None
    location: str
    ready_at: datetime | None


_NODE_COLS = (Node.id, Node.name, Node.slot, Node.state, Node.ipv4, Node.location, Node.ready_at)


def _node(r: Any) -> NodeView:
    return NodeView(
        id=r.id,
        name=r.name,
        slot=r.slot,
        state=r.state,
        ipv4=node_ipv4(r.ipv4),
        location=r.location,
        ready_at=r.ready_at,
    )


async def get_node(db: Db, node_id: uuid.UUID) -> NodeView | None:
    r = (await db.execute(select(*_NODE_COLS).where(Node.id == node_id))).first()
    return _node(r) if r is not None else None


async def live_nodes(db: Db) -> list[NodeView]:
    rows = (await db.execute(select(*_NODE_COLS).where(Node.state != S.DELETED.value))).all()
    return [_node(r) for r in rows]


@dataclass(frozen=True)
class KeyView:
    id: int
    slot: int | None
    node_id: uuid.UUID | None
    public_key: str
    short_id: str
    target: str
    server_name: str
    secret_name: str


_KEY_COLS = (
    RealityKey.id,
    RealityKey.slot,
    RealityKey.node_id,
    RealityKey.public_key,
    RealityKey.short_id,
    RealityKey.target,
    RealityKey.server_name,
    RealityKey.secret_name,
)


def scope_filter(slot: int | None, node_id: uuid.UUID) -> Any:
    return RealityKey.slot == slot if slot is not None else RealityKey.node_id == node_id


async def scope_key(db: Db, node: NodeView) -> KeyView | None:
    r = (await db.execute(select(*_KEY_COLS).where(scope_filter(node.slot, node.id)))).first()
    return KeyView(*r) if r is not None else None


async def telemt_clash(db: Db, node_id: uuid.UUID) -> bool:
    """True if the node's Telemt config uses Xray's port or API port (the agent would refuse it)."""
    ports = (
        await db.execute(select(TelemtNodeConfig.ports).where(TelemtNodeConfig.node_id == node_id))
    ).scalar_one_or_none()
    return bool(ports) and bool({PORT, XRAY_API_PORT} & set(ports or []))


@dataclass(frozen=True)
class StatusView:
    node_id: uuid.UUID
    enabled: bool
    up: bool
    version: str | None
    uptime_s: float | None
    target_ok: bool | None
    apply_error: str | None
    users_reported: int
    reported_at: datetime


_STATUS_COLS = (
    RealityNodeStatus.node_id,
    RealityNodeStatus.enabled,
    RealityNodeStatus.up,
    RealityNodeStatus.version,
    RealityNodeStatus.uptime_s,
    RealityNodeStatus.target_ok,
    RealityNodeStatus.apply_error,
    RealityNodeStatus.users_reported,
    RealityNodeStatus.reported_at,
)


async def node_status(db: Db, node_id: uuid.UUID) -> StatusView | None:
    r = (await db.execute(select(*_STATUS_COLS).where(RealityNodeStatus.node_id == node_id))).first()
    return StatusView(*r) if r is not None else None


async def all_statuses(db: Db) -> dict[uuid.UUID, StatusView]:
    return {r.node_id: StatusView(*r) for r in (await db.execute(select(*_STATUS_COLS))).all()}


def _utc_text(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


async def xray_section(db: Db, node_id: uuid.UUID, vault: Vault | None = None) -> dict[str, Any] | None:
    """The desired state's ``xray`` section for one node, or None (then the key is left out, and an
    agent that runs Xray stops it). Present only when the node's agent reported that it manages
    Xray (an older agent refuses the key), the node still serves, its Telemt does not clash, and
    its scope has a key. Carries the scope's private key: only for that node's own agent."""
    node = await get_node(db, node_id)
    if node is None or S(node.state) not in SERVE_STATES:
        return None
    status = await node_status(db, node_id)
    if status is None or not status.enabled or await telemt_clash(db, node_id):
        return None
    key = await scope_key(db, node)
    if key is None:
        return None
    configs = (
        await db.execute(
            select(
                RealityConfig.id, RealityConfig.uuid, RealityConfig.expires_at, RealityConfig.data_cap_bytes
            )
            .where(RealityConfig.key_id == key.id, RealityConfig.status == "active")
            .order_by(RealityConfig.id)
        )
    ).all()
    secret = await (vault or Vault.from_config()).get(db, key.secret_name)
    if secret is None:
        raise LookupError("reality key missing from the vault")
    return {
        "port": PORT,
        "private_key": secret.reveal(),
        "target": key.target,
        "server_names": [key.server_name],
        "short_ids": [key.short_id],
        "users": [
            {
                "id": str(c.uuid),
                "email": config_email(c.id),
                "expires_at": _utc_text(c.expires_at),
                "data_cap_bytes": c.data_cap_bytes,
            }
            for c in configs
        ],
    }


@dataclass(frozen=True)
class XrayUserTotal:
    email: str
    total_octets: int


async def store_xray_report(
    db: Db,
    node_id: uuid.UUID,
    *,
    enabled: bool,
    up: bool,
    version: str | None,
    uptime_s: float | None,
    target_ok: bool | None,
    apply_error: str | None,
    users: Sequence[XrayUserTotal],
    now: datetime,
) -> int:
    """Store one report's Xray section: node status, and per config the bytes this node counted.

    The agent sends cumulative totals; a total below the last one means the agent started counting
    again (its state was reset), so the new total is added rather than lost. Only configs of the
    node's own scope are accepted. Returns how many configs got usage. The caller commits.
    """
    status_values: dict[str, Any] = {
        "enabled": enabled,
        "up": up,
        "version": version,
        "uptime_s": uptime_s,
        "target_ok": target_ok,
        "apply_error": apply_error,
        "users_reported": len(users),
        "reported_at": now,
    }
    await db.execute(
        insert(RealityNodeStatus)
        .values(node_id=node_id, **status_values)
        .on_conflict_do_update(index_elements=[RealityNodeStatus.node_id], set_=status_values)
    )
    node = await get_node(db, node_id)
    if node is None or not users:
        return 0
    key = await scope_key(db, node)
    if key is None:
        return 0
    reported = {cid: u.total_octets for u in users if (cid := config_id_from_email(u.email)) is not None}
    if not reported:
        return 0
    mine = set(
        (
            await db.execute(
                select(RealityConfig.id).where(
                    RealityConfig.key_id == key.id, RealityConfig.id.in_(list(reported))
                )
            )
        )
        .scalars()
        .all()
    )
    previous = {
        r.config_id: (r.total_bytes, r.last_reported)
        for r in (
            await db.execute(
                select(RealityUsage.config_id, RealityUsage.total_bytes, RealityUsage.last_reported).where(
                    RealityUsage.node_id == node_id, RealityUsage.config_id.in_(list(mine))
                )
            )
        ).all()
    }
    for cid in sorted(mine):
        value = reported[cid]
        total, last = previous.get(cid, (0, 0))
        total += value - last if value >= last else value
        await db.execute(
            insert(RealityUsage)
            .values(config_id=cid, node_id=node_id, total_bytes=total, last_reported=value, updated_at=now)
            .on_conflict_do_update(
                index_elements=[RealityUsage.config_id, RealityUsage.node_id],
                set_={"total_bytes": total, "last_reported": value, "updated_at": now},
            )
        )
    if mine:
        # A config's usage is the sum over every node of its scope (old and new server of a slot).
        total = (
            select(func.coalesce(func.sum(RealityUsage.total_bytes), 0))
            .where(RealityUsage.config_id == RealityConfig.id)
            .scalar_subquery()
        )
        await db.execute(
            update(RealityConfig).where(RealityConfig.id.in_(list(mine))).values(used_bytes=total)
        )
    return len(mine)


async def recent_and_active_configs(db: Db, since: datetime) -> list[Any]:
    """Active configs and those that ended after ``since``, with their key and node."""
    return list(
        (
            await db.execute(
                select(
                    RealityConfig.id,
                    RealityConfig.node_id,
                    RealityConfig.slot,
                    RealityConfig.uuid,
                    RealityConfig.host,
                    RealityConfig.created_at,
                    RealityConfig.expires_at,
                    RealityConfig.data_cap_bytes,
                    RealityConfig.used_bytes,
                    RealityConfig.status,
                    RealityConfig.ended_at,
                    RealityConfig.key_id,
                    RealityKey.public_key,
                    RealityKey.short_id,
                    RealityKey.server_name,
                    Node.name.label("node_name"),
                    Node.location,
                )
                .join(RealityKey, RealityKey.id == RealityConfig.key_id)
                .join(Node, Node.id == RealityConfig.node_id)
                .where(
                    or_(
                        RealityConfig.status == "active",
                        and_(RealityConfig.ended_at.is_not(None), RealityConfig.ended_at >= since),
                    )
                )
                .order_by(RealityConfig.id.desc())
            )
        ).all()
    )
