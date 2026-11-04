"""One place that changes ``nodes.state``: checked against the lifecycle table, recorded in
``node_events`` (kind ``state``) and in ``node_operations.state_since`` (timeouts count from it).

The caller holds the node row lock (``SELECT ... FOR UPDATE``) and commits.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.db.models import Node, NodeEvent
from proxyhub.db.models_agent import AgentCertificate, AgentRegistrationToken
from proxyhub.db.models_live import NodeOperation
from proxyhub.fleet.lifecycle import NodeState, check_transition


async def lock_node(session: AsyncSession, node_id: uuid.UUID) -> Node | None:
    return await session.get(Node, node_id, with_for_update=True, populate_existing=True)


async def ensure_operation(
    session: AsyncSession, node: Node, now: datetime, *, requested_by: str = "system"
) -> NodeOperation:
    """The node's executor row (created for nodes that were not requested through this module)."""
    await session.execute(
        insert(NodeOperation)
        .values(
            node_id=node.id,
            requested_by=requested_by,
            requested_at=node.created_at,
            state_since=now,
            create_attempts=0,
            delete_attempts=0,
        )
        .on_conflict_do_nothing(index_elements=[NodeOperation.node_id])
    )
    op = await session.get(NodeOperation, node.id, with_for_update=True, populate_existing=True)
    assert op is not None
    return op


async def transition(
    session: AsyncSession,
    node: Node,
    target: NodeState,
    now: datetime,
    *,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> None:
    current = NodeState(node.state)
    check_transition(current, target)
    op = await ensure_operation(session, node, now)
    node.state = target.value
    node.updated_at = now
    if target == NodeState.READY and node.ready_at is None:
        node.ready_at = now
    if target == NodeState.RETIRING and node.retired_at is None:
        node.retired_at = now
    if target == NodeState.DELETED:
        node.deleted_at = now
        await close_identity(session, node.id, now)
    op.state_since = now
    op.updated_at = now
    session.add(
        NodeEvent(
            node_id=node.id,
            at=now,
            kind="state",
            detail={"from": current.value, "to": target.value, "reason": reason, **(detail or {})},
        )
    )


async def close_identity(session: AsyncSession, node_id: uuid.UUID, now: datetime) -> None:
    """A deleted node keeps no way back in: unused registration tokens expire, certificates are
    revoked (``node_deleted``). Authentication refuses deleted nodes anyway; this makes it explicit."""
    await session.execute(
        update(AgentRegistrationToken)
        .where(
            AgentRegistrationToken.node_id == node_id,
            AgentRegistrationToken.used_at.is_(None),
            AgentRegistrationToken.expires_at > now,
        )
        # never before created_at (the table's own check)
        .values(expires_at=func.greatest(AgentRegistrationToken.created_at, now))
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentCertificate)
        .where(AgentCertificate.node_id == node_id, AgentCertificate.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason="node_deleted")
        .execution_options(synchronize_session=False)
    )


def event(session: AsyncSession, node_id: uuid.UUID, now: datetime, kind: str, **detail: Any) -> None:
    session.add(NodeEvent(node_id=node_id, at=now, kind=kind, detail=detail))
