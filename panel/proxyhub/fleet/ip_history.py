"""IP history of our own servers (docs/10 section 3.11, decision D14: Hetzner hands out the same
IPs again and again).

* Every public IPv4 a node of ours got is recorded once per node (:func:`record_ip`).
* When a node burns (blocked from Iran by both probe sources), its IP is marked burned, and with it
  the whole /24 (:func:`mark_burned`).
* A new server whose IP is burned, or lies in a burned /24, is refused (:func:`burned_hit`): the
  executor retires it at once and the fleet retries the creation in another location.

Burned addresses are remembered without expiry (Iran's blocks of a datacenter range tend to stay).
Only our servers' addresses live here, never a user's (CLAUDE.md rule 7).
"""

from __future__ import annotations

import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models_fleet import IpHistory

Db = AsyncConnection | AsyncSession


def public_ipv4(value: object) -> ipaddress.IPv4Address | None:
    """A global unicast IPv4 address (what Hetzner gives a server), else None."""
    try:
        ip = ipaddress.ip_interface(str(value)).ip
    except ValueError:
        return None
    if not isinstance(ip, ipaddress.IPv4Address) or not ip.is_global:
        return None
    return ip


def prefix24(ip: ipaddress.IPv4Address) -> ipaddress.IPv4Network:
    return ipaddress.ip_network(f"{ip}/24", strict=False)  # type: ignore[return-value]


@dataclass(frozen=True)
class BurnHit:
    """Why a fresh IP is refused: ``ip`` (this very address burned before) or ``prefix24``."""

    kind: str
    burned_at: datetime


async def record_ip(
    db: Db,
    *,
    node_id: uuid.UUID,
    account_id: uuid.UUID | None,
    location: str,
    ip: ipaddress.IPv4Address,
    now: datetime,
) -> bool:
    """Record that ``node_id`` got ``ip``. Returns True the first time (False if already known)."""
    result = await db.execute(
        insert(IpHistory)
        .values(
            ip=str(ip),
            prefix24=str(prefix24(ip)),
            node_id=node_id,
            account_id=account_id,
            location=location[:20],
            first_seen_at=now,
        )
        .on_conflict_do_nothing(index_elements=[IpHistory.node_id, IpHistory.ip])
        .returning(IpHistory.id)
    )
    return result.first() is not None


async def burned_hit(db: Db, ip: ipaddress.IPv4Address, *, node_id: uuid.UUID) -> BurnHit | None:
    """Is ``ip`` (or its /24) burned by ANOTHER node of ours? The exact address wins over the range."""
    rows = (
        await db.execute(
            select(IpHistory.ip, IpHistory.burned_at)
            .where(
                IpHistory.burned_at.is_not(None),
                or_(IpHistory.node_id.is_(None), IpHistory.node_id != node_id),
                or_(IpHistory.ip == str(ip), IpHistory.prefix24 == str(prefix24(ip))),
            )
            .order_by(IpHistory.burned_at.desc())
        )
    ).all()
    if not rows:
        return None
    exact = [r for r in rows if ipaddress.ip_interface(str(r.ip)).ip == ip]
    first = (exact or rows)[0]
    return BurnHit("ip" if exact else "prefix24", first.burned_at)


async def mark_burned(
    db: Db,
    *,
    node_id: uuid.UUID,
    account_id: uuid.UUID | None,
    location: str,
    ip: ipaddress.IPv4Address,
    now: datetime,
    reason: str = "probe_blocked",
) -> None:
    """The node's IP is blocked from Iran: remember it (and so its /24) as burned."""
    await record_ip(db, node_id=node_id, account_id=account_id, location=location, ip=ip, now=now)
    await db.execute(
        update(IpHistory)
        .where(IpHistory.node_id == node_id, IpHistory.ip == str(ip), IpHistory.burned_at.is_(None))
        .values(burned_at=now, burned_reason=reason)
    )


async def burned_counts(db: Db) -> tuple[int, int]:
    """(burned addresses, burned /24 ranges) for the dashboard."""
    rows = (
        await db.execute(select(IpHistory.ip, IpHistory.prefix24).where(IpHistory.burned_at.is_not(None)))
    ).all()
    return len({str(r.ip) for r in rows}), len({str(r.prefix24) for r in rows})
