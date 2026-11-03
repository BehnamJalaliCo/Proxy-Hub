"""Property tests for the safety gates (CLAUDE.md rules 4, 5, 8). Pure data in, decision out."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from proxyhub.safety.gates import (
    AccountView,
    CreatePermit,
    CreateRequest,
    DeletePermit,
    NodeRowView,
    OperatorLimits,
    PermitError,
    ServerView,
    account_guard,
    can_create,
    can_delete,
    issue_create_permit,
    issue_delete_permit,
)

money = st.decimals(min_value=Decimal("-5"), max_value=Decimal("5000"), places=2, allow_nan=False)
label_values = st.sampled_from(["proxy-hub", "proxy-hub ", "Proxy-Hub", "other", ""])
roles = st.sampled_from(["node", "builder", "panel", "web", ""])
states = st.sampled_from(["retiring", "ready", "burned", "deleted", "requested", "draining"])

limits_st = st.builds(
    OperatorLimits,
    kill_switch=st.booleans(),
    budget_cap_eur=st.one_of(st.none(), money),
    node_cap_total=st.one_of(st.none(), st.integers(min_value=0, max_value=30)),
    creations_per_hour_cap=st.integers(min_value=0, max_value=10),
)
account_st = st.builds(
    AccountView,
    id=st.sampled_from(["a1", "a2"]),
    state=st.sampled_from(["verifying", "preparing", "active", "draining", "retired", "locked"]),
    guard_ok=st.booleans(),
    learned_server_limit=st.one_of(st.none(), st.integers(min_value=0, max_value=12)),
    servers_in_account=st.integers(min_value=0, max_value=15),
)
request_st = st.builds(
    CreateRequest,
    account=account_st,
    fleet_live_total=st.integers(min_value=0, max_value=40),
    creations_last_hour=st.integers(min_value=0, max_value=12),
    projected_month_eur=money,
    new_server_rest_of_month_eur=money,
)


@settings(max_examples=20_000)
@given(limits=limits_st, req=request_st)
def test_can_create_never_exceeds_any_cap(limits: OperatorLimits, req: CreateRequest) -> None:
    d = can_create(limits, req)
    if d.allowed:
        assert not limits.kill_switch
        assert limits.budget_cap_eur is not None and limits.node_cap_total is not None
        assert req.fleet_live_total + 1 <= limits.node_cap_total
        assert req.projected_month_eur + req.new_server_rest_of_month_eur <= limits.budget_cap_eur
        assert req.creations_last_hour < limits.creations_per_hour_cap
        assert req.account.state in {"active", "preparing"}
        assert req.account.guard_ok
        lim = req.account.learned_server_limit
        assert lim is None or req.account.servers_in_account + 1 <= lim
        assert req.projected_month_eur >= 0 and req.new_server_rest_of_month_eur >= 0
    else:
        assert d.reasons


def _ok_request() -> CreateRequest:
    return CreateRequest(
        account=AccountView("a1", "active", True, 10, 3),
        fleet_live_total=3,
        creations_last_hour=0,
        projected_month_eur=Decimal("20"),
        new_server_rest_of_month_eur=Decimal("6.49"),
    )


def test_missing_caps_block_creation() -> None:
    d = can_create(OperatorLimits(False, None, None, 3), _ok_request())
    assert not d.allowed
    assert {"budget_cap_not_set", "node_cap_not_set"} <= set(d.reasons)


def test_fresh_defaults_block_creation() -> None:
    from proxyhub.settings_schema import FleetSettings

    assert not can_create(FleetSettings().limits(), _ok_request()).allowed


def test_happy_path_allows() -> None:
    assert can_create(OperatorLimits(False, Decimal("100"), 10, 3), _ok_request()).allowed


server_st = st.builds(
    ServerView,
    hetzner_id=st.integers(min_value=1, max_value=5),
    name=st.just("s"),
    labels=st.fixed_dictionaries({"managed-by": label_values, "role": roles}),
    delete_protected=st.booleans(),
)
row_st = st.one_of(
    st.none(),
    st.builds(
        NodeRowView,
        account_id=st.sampled_from(["a1", "a2"]),
        hetzner_id=st.one_of(st.none(), st.integers(min_value=1, max_value=5)),
        state=states,
    ),
)


@settings(max_examples=20_000)
@given(server=server_st, row=row_st, account_id=st.sampled_from(["a1", "a2"]))
def test_can_delete_requires_both_locks(server: ServerView, row: NodeRowView | None, account_id: str) -> None:
    d = can_delete(server, row, account_id)
    both_locks = (
        server.labels.get("managed-by") == "proxy-hub"
        and server.labels.get("role") in {"node", "builder"}
        and row is not None
        and row.hetzner_id == server.hetzner_id
        and row.account_id == account_id
        and row.state == "retiring"
        and not server.delete_protected
    )
    assert d.allowed == both_locks


def test_panel_role_is_never_deletable() -> None:
    server = ServerView(1, "panel", {"managed-by": "proxy-hub", "role": "panel"})
    assert not can_delete(server, NodeRowView("a1", 1, "retiring"), "a1").allowed


@given(servers=st.lists(server_st, max_size=6))
def test_account_guard_flags_every_foreign_server(servers: list[ServerView]) -> None:
    d = account_guard(servers)
    foreign = [s for s in servers if s.labels.get("managed-by") != "proxy-hub"]
    assert d.allowed == (not foreign)
    assert len(d.reasons) == len(foreign)


def test_permits_cannot_be_forged_or_issued_on_denial() -> None:
    from proxyhub.safety.gates import Decision

    with pytest.raises(PermitError):
        CreatePermit(object(), Decision(True))
    with pytest.raises(PermitError):
        DeletePermit(object(), Decision(True), 1)
    with pytest.raises(PermitError):
        issue_create_permit(OperatorLimits(True, Decimal("1"), 1, 1), _ok_request())
    with pytest.raises(PermitError):
        issue_delete_permit(ServerView(1, "x", {}), None, "a1")
    permit = issue_create_permit(OperatorLimits(False, Decimal("100"), 10, 3), _ok_request())
    assert permit.decision.allowed
