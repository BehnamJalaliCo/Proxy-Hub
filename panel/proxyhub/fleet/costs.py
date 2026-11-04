"""Month cost inputs for our nodes, priced with live Hetzner offers (proxyhub.safety.budget does the math)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from proxyhub.fleet.lifecycle import NodeState
from proxyhub.safety.budget import CostItem, Price, month_bounds, projected_month_total

PriceKey = tuple[str, str, str]  # (account_id, server_type, location)


@dataclass(frozen=True)
class NodeCostInput:
    name: str
    account_id: str
    server_type: str
    location: str
    state: NodeState
    hetzner_id: int | None
    created_at: datetime
    deleted_at: datetime | None


def node_cost_items(
    nodes: Iterable[NodeCostInput], prices: Mapping[PriceKey, Price], now: datetime
) -> tuple[list[CostItem], list[str]]:
    """Cost items for every node that can be billed this month; plus names of nodes without a live price.

    A node marked deleted without a deletion time is billed up to ``now`` (conservative). A node
    deleted before Hetzner ever saw it (no ``hetzner_id``) costs nothing.
    """
    start, _ = month_bounds(now)
    items: list[CostItem] = []
    missing: list[str] = []
    for n in nodes:
        deleted_at = n.deleted_at
        if n.state == NodeState.DELETED:
            if n.hetzner_id is None:
                continue
            if deleted_at is None:
                deleted_at = now
        if deleted_at is not None and deleted_at < start:
            continue
        price = prices.get((n.account_id, n.server_type, n.location))
        if price is None:
            missing.append(n.name)
            continue
        items.append(CostItem(price=price, created_at=n.created_at, deleted_at=deleted_at))
    return items, missing


def projected_month(
    nodes: Iterable[NodeCostInput], prices: Mapping[PriceKey, Price], now: datetime
) -> tuple[Decimal, list[str]]:
    items, missing = node_cost_items(nodes, prices, now)
    return projected_month_total(items, now), missing


def with_upper_bound_prices(
    nodes: Iterable[NodeCostInput], prices: Mapping[PriceKey, Price]
) -> dict[PriceKey, Price]:
    """Fill a missing node price with the highest known price of its type (else of any type).

    Only for DISPLAY (the overview); the planner never guesses and blocks creation instead.
    """
    out = dict(prices)
    if not prices:
        return out
    overall = Price(
        hourly=max(p.hourly for p in prices.values()), monthly=max(p.monthly for p in prices.values())
    )
    by_type: dict[str, Price] = {}
    for (_, server_type, _), p in prices.items():
        cur = by_type.get(server_type)
        by_type[server_type] = (
            p if cur is None else Price(hourly=max(cur.hourly, p.hourly), monthly=max(cur.monthly, p.monthly))
        )
    for n in nodes:
        key = (n.account_id, n.server_type, n.location)
        if key not in out:
            out[key] = by_type.get(n.server_type, overall)
    return out


def money(value: Decimal) -> str:
    """Display money: euros rounded UP to the cent (a cost shown is never below the real one)."""
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_CEILING))
