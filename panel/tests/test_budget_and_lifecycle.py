from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from proxyhub.fleet.lifecycle import ALLOWED, IllegalTransition, NodeState, check_transition
from proxyhub.safety.budget import CostItem, Price, month_bounds, projected_month_total, rest_of_month_cost

CPX02 = Price(hourly=Decimal("0.0104"), monthly=Decimal("6.49"))  # hetzner.com, 2026-11-03


def test_month_bounds_december_rolls_over() -> None:
    start, end = month_bounds(datetime(2026, 12, 15, 10, tzinfo=UTC))
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_full_month_is_capped_at_monthly_price() -> None:
    now = datetime(2026, 11, 3, 12, tzinfo=UTC)
    item = CostItem(CPX02, created_at=datetime(2026, 10, 1, tzinfo=UTC))
    assert projected_month_total([item], now) == Decimal("6.49")


def test_started_hour_is_billed() -> None:
    now = datetime(2026, 11, 30, 23, 30, tzinfo=UTC)
    assert rest_of_month_cost(CPX02, now) == Decimal("0.0104")


def test_short_lived_server_costs_hours_only() -> None:
    created = datetime(2026, 11, 3, 10, tzinfo=UTC)
    item = CostItem(CPX02, created_at=created, deleted_at=created + timedelta(minutes=61))
    assert projected_month_total([item], created) == Decimal("0.0208")


def test_server_deleted_last_month_costs_nothing_now() -> None:
    item = CostItem(CPX02, datetime(2026, 9, 1, tzinfo=UTC), deleted_at=datetime(2026, 10, 20, tzinfo=UTC))
    assert projected_month_total([item], datetime(2026, 11, 3, tzinfo=UTC)) == 0


@given(day=st.integers(min_value=1, max_value=28), hour=st.integers(min_value=0, max_value=23))
def test_rest_of_month_never_exceeds_monthly(day: int, hour: int) -> None:
    cost = rest_of_month_cost(CPX02, datetime(2026, 11, day, hour, tzinfo=UTC))
    assert Decimal(0) <= cost <= CPX02.monthly


def test_deleted_is_terminal_and_every_state_can_reach_deleted() -> None:
    assert ALLOWED[NodeState.DELETED] == frozenset()
    for state in NodeState:
        seen, frontier = {state}, [state]
        while frontier:
            for nxt in ALLOWED[frontier.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert NodeState.DELETED in seen


def test_illegal_transition_rejected() -> None:
    with pytest.raises(IllegalTransition):
        check_transition(NodeState.READY, NodeState.DELETED)  # must pass through retiring
    check_transition(NodeState.RETIRING, NodeState.DELETED)
