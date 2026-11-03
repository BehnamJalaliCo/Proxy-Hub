"""Domain pool state machine (docs/10 section 3.10, decision D13).

* ``active``: the domain the slot names live on (new posts use it). At most one, enforced by the
  database (partial unique index) as well as here.
* ``standby``: ready to take over when the active domain is filtered.
* ``filtered``: blocked in Iran; kept for the record, never used for new names.
* ``retired``: the owner no longer wants it used.
* ``none``: seen in Cloudflare, not part of the pool.

The owner sets states from the dashboard. Making a zone active while another one is active moves
the old one to ``standby`` in the same transaction. Any transition not listed is refused.
"""

from __future__ import annotations

from enum import StrEnum


class PoolState(StrEnum):
    NONE = "none"
    ACTIVE = "active"
    STANDBY = "standby"
    FILTERED = "filtered"
    RETIRED = "retired"


P = PoolState

ALLOWED: dict[PoolState, frozenset[PoolState]] = {
    P.NONE: frozenset({P.ACTIVE, P.STANDBY, P.RETIRED}),
    P.STANDBY: frozenset({P.ACTIVE, P.NONE, P.FILTERED, P.RETIRED}),
    P.ACTIVE: frozenset({P.STANDBY, P.FILTERED, P.RETIRED, P.NONE}),
    P.FILTERED: frozenset({P.STANDBY, P.NONE, P.RETIRED}),
    P.RETIRED: frozenset({P.NONE, P.STANDBY}),
}

# Cloudflare zone statuses in which records actually resolve on the public internet.
SERVING_ZONE_STATUSES = frozenset({"active"})


class PoolTransitionError(ValueError):
    """``code`` is the API error code: pool_transition_invalid or zone_not_ready."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_pool_transition(
    current: PoolState, target: PoolState, *, zone_status: str, dns_edit: bool | None
) -> bool:
    """True if something changes, False for a no-op (same state). Raises if refused."""
    if current == target:
        return False
    if target not in ALLOWED[current]:
        raise PoolTransitionError("pool_transition_invalid")
    if target == P.ACTIVE and (zone_status not in SERVING_ZONE_STATUSES or dns_edit is False):
        # Names on a zone whose nameservers are not on Cloudflare yet (or without DNS:Edit) would
        # never resolve; refusing is clearer than an active domain that silently does nothing.
        raise PoolTransitionError("zone_not_ready")
    return True


def displaced_state(previous_active: PoolState) -> PoolState:
    """What the old active zone becomes when the owner activates another one."""
    assert previous_active == P.ACTIVE
    return P.STANDBY
