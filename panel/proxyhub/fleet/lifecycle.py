"""Node lifecycle (docs/03_NODES.md). Any transition not listed here is refused."""

from __future__ import annotations

from enum import StrEnum


class NodeState(StrEnum):
    REQUESTED = "requested"
    CREATING = "creating"
    BOOTING = "booting"
    REGISTERING = "registering"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    REPAIRING = "repairing"
    BURNED = "burned"
    DRAINING = "draining"
    RETIRING = "retiring"
    DELETED = "deleted"


S = NodeState

ALLOWED: dict[NodeState, frozenset[NodeState]] = {
    S.REQUESTED: frozenset({S.CREATING, S.DELETED}),  # DELETED: request abandoned before any API call
    S.CREATING: frozenset({S.BOOTING, S.RETIRING}),
    S.BOOTING: frozenset({S.REGISTERING, S.UNHEALTHY, S.RETIRING}),
    S.REGISTERING: frozenset({S.READY, S.UNHEALTHY, S.RETIRING}),
    S.READY: frozenset({S.UNHEALTHY, S.BURNED, S.DRAINING, S.RETIRING}),
    S.UNHEALTHY: frozenset({S.REPAIRING, S.READY, S.RETIRING}),
    S.REPAIRING: frozenset({S.READY, S.UNHEALTHY, S.RETIRING}),
    S.BURNED: frozenset({S.DRAINING, S.RETIRING}),
    S.DRAINING: frozenset({S.RETIRING}),
    S.RETIRING: frozenset({S.DELETED}),
    S.DELETED: frozenset(),
}

# States that occupy a server slot and therefore count against caps and budget.
LIVE_STATES: frozenset[NodeState] = frozenset(ALLOWED) - {S.DELETED}


class IllegalTransition(ValueError):
    pass


def check_transition(current: NodeState, target: NodeState) -> None:
    if target not in ALLOWED[current]:
        raise IllegalTransition(f"{current} -> {target} is not allowed")
