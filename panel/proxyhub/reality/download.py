"""The download check (decision D61): does a node's Reality traffic come back to its users?

The owner (2026-11-07, P29): the Finland server's configs worked from outside Iran at 200+ Mbit
while MCI users saw "upload but no download"; the probes from Iran (a TLS handshake) called it open.
The only witnesses of that are the users' own connections, so the agent (0.5.0 on) reports the
node's totals of each direction over its Reality users: two numbers for the whole node, never per
person (rule 7). :func:`record_traffic` keeps them per node and hour (``reality_traffic_hours``,
:data:`KEEP` long).

:func:`judge` (pure) names the SUSPECTS: a node whose users sent at least :data:`MIN_UP` in each of
the last :data:`HOURS` complete hours while getting back far less than the other nodes' users do,
relative to what they sent: below :data:`PEER_FACTOR` of the other nodes' median down/up, and never
above :data:`MAX_SUSPECT_RATIO` (a node whose users get back twice what they send is not blocked,
whatever the others do). With no other node to compare, only a node whose users got back less than
they sent (:data:`LONE_RATIO`) is a suspect.

**Observe first** (like the @MTProxybot removal, D37): a suspect is told to the owner with the
numbers (proxyhub/fleet/alerts.py, ``download_suspect``), who can remove its location (D60); nothing
is replaced by itself until real data from a blocked node and healthy ones confirm the thresholds.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models import Node
from proxyhub.db.models_reality import RealityNodeStatus, RealityTrafficHour

Db = AsyncConnection | AsyncSession

MB: Final = 1_000_000
MIN_UP: Final = 5 * MB  # per hour: less is too little to judge
HOURS: Final = 2  # complete hours in a row
PEER_FACTOR: Final = 0.25
MAX_SUSPECT_RATIO: Final = 2.0
LONE_RATIO: Final = 1.0
KEEP: Final = timedelta(days=8)


def hour_of(at: datetime) -> datetime:
    return at.replace(minute=0, second=0, microsecond=0)


def deltas(previous: tuple[int | None, int | None], up: int, down: int) -> tuple[int, int]:
    """Bytes of each direction since the last report; with no last totals nothing is known yet.
    Either total below the last one means Xray started again, and then the counters of BOTH
    directions began at 0: the other one may already have passed its old value. (Removing a user
    live keeps Xray's counters, so nothing else lowers a total.)"""
    prev_up, prev_down = previous
    if prev_up is None or prev_down is None:
        return 0, 0
    if up < prev_up or down < prev_down:
        return up, down
    return up - prev_up, down - prev_down


async def record_traffic(db: Db, node_id: uuid.UUID, up_total: int, down_total: int, now: datetime) -> None:
    """Fold one report's cumulative totals into the node's hour (the caller commits)."""
    row = (
        await db.execute(
            select(RealityNodeStatus.traffic_up, RealityNodeStatus.traffic_down).where(
                RealityNodeStatus.node_id == node_id
            )
        )
    ).first()
    d_up, d_down = deltas((row.traffic_up, row.traffic_down) if row else (None, None), up_total, down_total)
    await db.execute(
        update(RealityNodeStatus)
        .where(RealityNodeStatus.node_id == node_id)
        .values(traffic_up=up_total, traffic_down=down_total)
    )
    if d_up or d_down:
        stmt = insert(RealityTrafficHour).values(node_id=node_id, hour=hour_of(now), up=d_up, down=d_down)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[RealityTrafficHour.node_id, RealityTrafficHour.hour],
                set_={"up": RealityTrafficHour.up + d_up, "down": RealityTrafficHour.down + d_down},
            )
        )
    await db.execute(
        delete(RealityTrafficHour).where(
            RealityTrafficHour.node_id == node_id, RealityTrafficHour.hour < now - KEEP
        )
    )


@dataclass(frozen=True)
class Suspect:
    node_id: str
    up: int  # over the judged hours
    down: int
    peers_ratio: float | None  # the other nodes' median down/up (None: no other node to compare)


def judge(
    traffic: Mapping[str, Mapping[datetime, tuple[int, int]]], hours: Sequence[datetime]
) -> list[Suspect]:
    """The suspects among ``traffic`` (node -> hour -> (up, down)) over ``hours`` (see the module
    doc). Pure."""
    out: list[Suspect] = []
    for node, rows in sorted(traffic.items()):
        total_up = total_down = 0
        ref: float | None = None
        for hour in hours:
            up, down = rows.get(hour, (0, 0))
            if up < MIN_UP:
                break
            peers = [
                d / u
                for other, other_rows in traffic.items()
                if other != node
                for u, d in [other_rows.get(hour, (0, 0))]
                if u >= MIN_UP
            ]
            ref = statistics.median(peers) if peers else None
            limit = min(PEER_FACTOR * ref, MAX_SUSPECT_RATIO) if ref is not None else LONE_RATIO
            if down >= limit * up:
                break
            total_up, total_down = total_up + up, total_down + down
        else:
            if hours:
                out.append(Suspect(node, total_up, total_down, ref))
    return out


def judged_hours(now: datetime) -> list[datetime]:
    """The last :data:`HOURS` complete hours, oldest first."""
    current = hour_of(now)
    return [current - timedelta(hours=k) for k in range(HOURS, 0, -1)]


async def suspects(db: Db, now: datetime) -> list[Suspect]:
    """The live nodes whose download looks blocked now (see the module doc)."""
    hours = judged_hours(now)
    rows = (
        await db.execute(
            select(
                RealityTrafficHour.node_id,
                RealityTrafficHour.hour,
                RealityTrafficHour.up,
                RealityTrafficHour.down,
            )
            .join(Node, Node.id == RealityTrafficHour.node_id)
            .where(RealityTrafficHour.hour.in_(hours), Node.state != "deleted")
        )
    ).all()
    traffic: dict[str, dict[datetime, tuple[int, int]]] = {}
    for r in rows:
        traffic.setdefault(str(r.node_id), {})[r.hour] = (int(r.up), int(r.down))
    return judge(traffic, hours)


def suspect_detail(s: Suspect) -> dict[str, Any]:
    return {
        "node_id": s.node_id,
        "up_mb": round(s.up / MB, 1),
        "down_mb": round(s.down / MB, 1),
        "peers_ratio": None if s.peers_ratio is None else round(s.peers_ratio, 1),
    }
