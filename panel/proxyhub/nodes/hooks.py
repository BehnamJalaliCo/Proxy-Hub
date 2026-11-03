"""Lifecycle hooks called by the agent API (inside its transaction; the caller commits)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.fleet.lifecycle import NodeState
from proxyhub.nodes.transitions import lock_node, transition


async def on_agent_registered(session: AsyncSession, node_id: uuid.UUID, now: datetime) -> None:
    """A node's agent registered with its one-time token: ``booting`` -> ``registering``.

    Any other state is left alone (a re-registration of a ready node, or a node the executor has
    not adopted yet; the executor reconciles those from ``agent_nodes`` on its next pass).
    """
    node = await lock_node(session, node_id)
    if node is None or node.state != NodeState.BOOTING.value:
        return
    await transition(session, node, NodeState.REGISTERING, now, reason="agent_registered")
