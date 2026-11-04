"""``usage_agg``: the proxy-usage counters a node agent may add to its report (decision D39).

What the agent does ON THE NODE, in memory (the panel never sees an IP):

* every connection's IP is looked up in a local offline database (country, province/region, ASN;
  e.g. DB-IP Lite, CC-BY) and turned into counters at once; no online lookup service is used,
* unique users per UTC day are counted with HMAC-SHA256(IP) under a random salt that exists only in
  the agent's memory and is replaced at UTC midnight (never written, never sent),
* MB per anonymous user per day gives the p50/p90/p99 distribution,
* Xray and Telemt access logs stay OFF; Telemt's stats endpoint and Xray's StatsService give the
  per-proxy (Telemt user / Reality config) bytes,
* every country/region/ASN bucket with fewer than :data:`MIN_USERS` users is folded into "other".

What the panel does here: a strict schema (unknown fields refused, strict types, narrow patterns;
the whole report was already scanned for anything IP-shaped by ``proxyhub.agent_api.reports``), the
fold rule enforced again, percentiles dropped below :data:`MIN_USERS` users, and an upsert keyed by
(node, agent run, day[, hour, proxy] / [dimension, key]). Counters are cumulative within one agent
run (``boot_id``), so a later report of the same run replaces the earlier one, and two runs on one
day add up.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_analytics import USAGE_MIN_USERS, UsageDay, UsageDim, UsageHour

MIN_USERS: Final = USAGE_MIN_USERS
INT64_MAX: Final = 2**63 - 1
MAX_HOUR_ROWS: Final = 2400
MAX_BUCKETS: Final = 400
# A report about a day further away than this is not stored (clock trouble on the node).
DAY_WINDOW: Final = timedelta(days=3)
OTHER: Final = "other"

Counter = Annotated[int, Field(ge=0, le=INT64_MAX)]
Users = Annotated[int, Field(ge=0, le=100_000_000)]
Mb = Annotated[float, Field(ge=0, le=100_000_000)]
BootId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
DayText = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
# Telemt user names and Reality config names: the same narrow, IP-free shape.
Ref = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
COUNTRY_KEY: Final = r"^(?:[A-Z]{2}|other)$"
# Region names as an offline DB spells them ("Tehran", "Razavi Khorasan"): letters only, no digits.
REGION_KEY: Final = r"^(?:[A-Za-z][A-Za-z .'()\-]{0,63}|other)$"
ASN_KEY: Final = r"^(?:AS[0-9]{1,10}|other)$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class HourCounters(_Strict):
    hour: Annotated[int, Field(ge=0, le=23)]
    kind: Literal["mtproto", "reality"]
    ref: Ref
    bytes_up: Counter
    bytes_down: Counter
    conns_max: Annotated[int, Field(ge=0, le=10_000_000)]


class Bucket(_Strict):
    key: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    users: Users
    bytes: Counter


class PerUser(_Strict):
    """MB per anonymous user (from the per-day HMAC keys): how many users, and three percentiles."""

    users: Users
    p50_mb: Mb
    p90_mb: Mb
    p99_mb: Mb

    @model_validator(mode="after")
    def _ordered(self) -> PerUser:
        if not self.p50_mb <= self.p90_mb <= self.p99_mb:
            raise ValueError("percentiles out of order")
        return self


def _keys(buckets: Sequence[Bucket], pattern: str) -> None:
    rx = re.compile(pattern)
    keys = [b.key for b in buckets]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate bucket")
    if any(not rx.fullmatch(k) for k in keys):
        raise ValueError("bad bucket key")


class UsageAgg(_Strict):
    boot_id: BootId
    day: DayText
    unique_users: Users
    hours: Annotated[list[HourCounters], Field(max_length=MAX_HOUR_ROWS)]
    by_country: Annotated[list[Bucket], Field(max_length=MAX_BUCKETS)]
    by_region: Annotated[list[Bucket], Field(max_length=MAX_BUCKETS)]
    by_asn: Annotated[list[Bucket], Field(max_length=MAX_BUCKETS)]
    per_user: PerUser | None

    @field_validator("day")
    @classmethod
    def _date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> UsageAgg:
        _keys(self.by_country, COUNTRY_KEY)
        _keys(self.by_region, REGION_KEY)
        _keys(self.by_asn, ASN_KEY)
        hours = [(h.hour, h.kind, h.ref) for h in self.hours]
        if len(hours) != len(set(hours)):
            raise ValueError("duplicate hour row")
        return self

    @property
    def day_date(self) -> date:
        return date.fromisoformat(self.day)


def fold_small(buckets: Sequence[Bucket], min_users: int = MIN_USERS) -> list[tuple[str, int, int]]:
    """(key, users, bytes) with every bucket under ``min_users`` users folded into "other"."""
    kept: list[tuple[str, int, int]] = []
    other_users = other_bytes = 0
    seen_other = False
    for b in buckets:
        if b.key == OTHER or b.users < min_users:
            other_users += b.users
            other_bytes += b.bytes
            seen_other = True
        else:
            kept.append((b.key, b.users, b.bytes))
    kept.sort(key=lambda t: (-t[1], t[0]))
    if seen_other:
        kept.append((OTHER, other_users, min(other_bytes, INT64_MAX)))
    return kept


Db = AsyncConnection | AsyncSession


async def store_usage_agg(db: Db, node_id: uuid.UUID, agg: UsageAgg, now: datetime) -> bool:
    """Upsert one ``usage_agg`` block. Returns False (stores nothing) for a day outside the window."""
    day = agg.day_date
    if abs(day - now.date()) > DAY_WINDOW:
        return False
    key = {"node_id": node_id, "boot_id": agg.boot_id, "day": day}
    if agg.hours:
        stmt = pg_insert(UsageHour).values(
            [
                {
                    **key,
                    "hour": h.hour,
                    "kind": h.kind,
                    "ref": h.ref,
                    "bytes_up": h.bytes_up,
                    "bytes_down": h.bytes_down,
                    "conns_max": h.conns_max,
                    "received_at": now,
                }
                for h in agg.hours
            ]
        )
        await db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_analytics_usage_hourly_key",
                set_={
                    "bytes_up": stmt.excluded.bytes_up,
                    "bytes_down": stmt.excluded.bytes_down,
                    "conns_max": stmt.excluded.conns_max,
                    "received_at": stmt.excluded.received_at,
                },
            )
        )
    per_user = agg.per_user
    keep_percentiles = per_user is not None and per_user.users >= MIN_USERS
    day_stmt = pg_insert(UsageDay).values(
        **key,
        unique_users=agg.unique_users,
        p50_mb=per_user.p50_mb if keep_percentiles and per_user else None,
        p90_mb=per_user.p90_mb if keep_percentiles and per_user else None,
        p99_mb=per_user.p99_mb if keep_percentiles and per_user else None,
        received_at=now,
    )
    await db.execute(
        day_stmt.on_conflict_do_update(
            constraint="uq_analytics_usage_daily_key",
            set_={
                "unique_users": day_stmt.excluded.unique_users,
                "p50_mb": day_stmt.excluded.p50_mb,
                "p90_mb": day_stmt.excluded.p90_mb,
                "p99_mb": day_stmt.excluded.p99_mb,
                "received_at": day_stmt.excluded.received_at,
            },
        )
    )
    dims: dict[Literal["country", "region", "asn"], list[Bucket]] = {
        "country": agg.by_country,
        "region": agg.by_region,
        "asn": agg.by_asn,
    }
    for dim, buckets in dims.items():
        rows = fold_small(buckets)
        # The newest report of this run is the whole picture of the day: older keys go.
        stale = delete(UsageDim).where(
            UsageDim.node_id == node_id,
            UsageDim.boot_id == agg.boot_id,
            UsageDim.day == day,
            UsageDim.dim == dim,
        )
        if rows:
            stale = stale.where(UsageDim.key.not_in([k for k, _, _ in rows]))
        await db.execute(stale)
        if not rows:
            continue
        stmt = pg_insert(UsageDim).values(
            [{**key, "dim": dim, "key": k, "users": u, "bytes": b, "received_at": now} for k, u, b in rows]
        )
        await db.execute(
            stmt.on_conflict_do_update(
                constraint="uq_analytics_usage_dims_key",
                set_={
                    "users": stmt.excluded.users,
                    "bytes": stmt.excluded.bytes,
                    "received_at": stmt.excluded.received_at,
                },
            )
        )
    return True
