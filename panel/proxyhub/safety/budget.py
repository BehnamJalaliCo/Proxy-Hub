"""Monthly cost projection from live Hetzner prices.

Hetzner bills cloud servers per started hour, and a server's bill for a month never exceeds
its monthly price (verified on hetzner.com, 2026-11-03). Prices are passed in from the live
API; nothing here hard-codes a price.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Price:
    hourly: Decimal
    monthly: Decimal


@dataclass(frozen=True)
class CostItem:
    price: Price
    created_at: datetime
    deleted_at: datetime | None = None


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


def _billed_hours(start: datetime, end: datetime) -> int:
    seconds = (end - start).total_seconds()
    return max(0, math.ceil(seconds / 3600))


def _cost(price: Price, hours: int) -> Decimal:
    return min(price.hourly * hours, price.monthly)


def projected_month_total(items: Iterable[CostItem], now: datetime) -> Decimal:
    """Expected bill for the current month if live servers keep running to month end."""
    start, end = month_bounds(now)
    total = Decimal(0)
    for item in items:
        if item.deleted_at is not None and item.deleted_at < start:
            continue
        begin = max(item.created_at, start)
        finish = min(item.deleted_at, end) if item.deleted_at is not None else end
        total += _cost(item.price, _billed_hours(begin, finish))
    return total


def rest_of_month_cost(price: Price, now: datetime) -> Decimal:
    """Cost of a server created now and kept until month end."""
    _, end = month_bounds(now)
    return _cost(price, _billed_hours(now.astimezone(UTC), end))
