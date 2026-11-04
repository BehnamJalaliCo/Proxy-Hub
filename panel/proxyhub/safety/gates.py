"""Safety gates: the only place that decides whether a real server may be created or deleted.

CLAUDE.md rules enforced here:
  4: a Hetzner token that sees servers we do not manage locks that account.
  5: delete only servers with label managed-by=proxy-hub AND a matching row in our DB.
  8: budget cap and node cap are never exceeded, whatever a control loop asks for.

Inputs are plain data, so these functions are tested exhaustively (property tests) without
any imitation of Hetzner. Hetzner mutations require a permit object issued only here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

MANAGED_LABEL = "managed-by"
MANAGED_VALUE = "proxy-hub"
DELETABLE_ROLES = frozenset({"node", "builder"})
ACTIVE_ACCOUNT_STATE = "active"
# Accounts in these states may still host build/test servers created by the panel itself.
CREATE_ACCOUNT_STATES = frozenset({"active", "preparing"})


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...] = ()

    @staticmethod
    def of(reasons: list[str]) -> Decision:
        return Decision(allowed=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class OperatorLimits:
    """Owner-set limits. None means "not set" and always blocks creation (decision O3)."""

    kill_switch: bool
    budget_cap_eur: Decimal | None
    node_cap_total: int | None
    creations_per_hour_cap: int


@dataclass(frozen=True)
class AccountView:
    id: str
    state: str
    guard_ok: bool
    learned_server_limit: int | None
    servers_in_account: int  # every server in the project, ours or not


@dataclass(frozen=True)
class CreateRequest:
    account: AccountView
    fleet_live_total: int  # live + in-progress nodes across all accounts
    creations_last_hour: int
    projected_month_eur: Decimal  # all accounts, current month, before this creation
    new_server_rest_of_month_eur: Decimal


def can_create(limits: OperatorLimits, req: CreateRequest) -> Decision:
    reasons: list[str] = []
    if limits.kill_switch:
        reasons.append("kill_switch_on")
    if limits.budget_cap_eur is None:
        reasons.append("budget_cap_not_set")
    if limits.node_cap_total is None:
        reasons.append("node_cap_not_set")
    if limits.node_cap_total is not None and req.fleet_live_total + 1 > limits.node_cap_total:
        reasons.append("node_cap_reached")
    if limits.budget_cap_eur is not None and (
        req.projected_month_eur + req.new_server_rest_of_month_eur > limits.budget_cap_eur
    ):
        reasons.append("budget_cap_reached")
    if limits.creations_per_hour_cap < 1 or req.creations_last_hour >= limits.creations_per_hour_cap:
        reasons.append("creation_rate_cap_reached")
    if req.account.state not in CREATE_ACCOUNT_STATES:
        reasons.append("account_not_usable")
    if not req.account.guard_ok:
        reasons.append("account_guard_failed")
    limit = req.account.learned_server_limit
    if limit is not None and req.account.servers_in_account + 1 > limit:
        reasons.append("account_server_limit_reached")
    if req.projected_month_eur < 0 or req.new_server_rest_of_month_eur < 0:
        reasons.append("invalid_cost_input")
    return Decision.of(reasons)


@dataclass(frozen=True)
class ServerView:
    """What Hetzner says about a server (live API data, not our DB)."""

    hetzner_id: int
    name: str
    labels: dict[str, str] = field(default_factory=dict)
    delete_protected: bool = False


@dataclass(frozen=True)
class NodeRowView:
    """What our database says about that server."""

    account_id: str
    hetzner_id: int | None
    state: str


def can_delete(server: ServerView, row: NodeRowView | None, account_id: str) -> Decision:
    reasons: list[str] = []
    if server.labels.get(MANAGED_LABEL) != MANAGED_VALUE:
        reasons.append("label_managed_by_missing")
    if server.labels.get("role") not in DELETABLE_ROLES:
        reasons.append("role_not_deletable")
    if row is None:
        reasons.append("no_db_row")
    else:
        if row.hetzner_id != server.hetzner_id:
            reasons.append("db_row_hetzner_id_mismatch")
        if row.account_id != account_id:
            reasons.append("db_row_account_mismatch")
        if row.state != "retiring":
            reasons.append("db_row_not_retiring")
    if server.delete_protected:
        reasons.append("delete_protection_on")
    return Decision.of(reasons)


def account_guard(servers: Sequence[ServerView]) -> Decision:
    """A node account must contain only servers we manage (CLAUDE.md rule 4)."""
    foreign = [s for s in servers if s.labels.get(MANAGED_LABEL) != MANAGED_VALUE]
    return Decision.of([f"foreign_server:{s.hetzner_id}" for s in foreign])


# --- permits -----------------------------------------------------------------------------------

_ISSUER = object()


class PermitError(PermissionError):
    pass


class _Permit:
    def __init__(self, issuer: object, decision: Decision) -> None:
        if issuer is not _ISSUER:
            raise PermitError("permits are issued only by proxyhub.safety.gates")
        if not decision.allowed:
            raise PermitError(f"decision denies the action: {decision.reasons}")
        self.decision = decision


class CreatePermit(_Permit):
    pass


class DeletePermit(_Permit):
    def __init__(self, issuer: object, decision: Decision, hetzner_id: int) -> None:
        super().__init__(issuer, decision)
        self.hetzner_id = hetzner_id


def issue_create_permit(limits: OperatorLimits, req: CreateRequest) -> CreatePermit:
    return CreatePermit(_ISSUER, can_create(limits, req))


def issue_delete_permit(server: ServerView, row: NodeRowView | None, account_id: str) -> DeletePermit:
    return DeletePermit(_ISSUER, can_delete(server, row, account_id), server.hetzner_id)
