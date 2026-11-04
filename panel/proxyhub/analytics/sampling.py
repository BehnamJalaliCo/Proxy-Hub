"""What the ``analytics`` loop stores, the load signal of each node, and the owner's threshold alerts.

Traffic is counted from Hetzner's own per-server counters (``outgoing_traffic`` is what users
received). Each sample stores the counter AND the bytes since the previous sample of the same node,
so "TB per day" is a plain sum. A counter that went down was reset by Hetzner (new billing period):
the new value is the traffic since the reset. A node's first sample counts its whole counter only
if the node is new (its counter started at creation); an older node's first sample is a baseline,
because the traffic before it cannot be put on a day.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.accounts.state import OBSERVED_STATES
from proxyhub.analytics.hetzner_usage import LoadWindow, ServerUsage
from proxyhub.analytics.messages import cpu_alert_fa, traffic_alert_fa
from proxyhub.analytics.settings import AnalyticsSettings, load_settings
from proxyhub.db.models import HetznerAccount, Node
from proxyhub.db.models_analytics import ChannelSample, NodeSample, ThresholdAlert
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.lifecycle import LIVE_STATES
from proxyhub.telegram.schedule import TEHRAN
from proxyhub.telegram.store import enqueue_owner_alert

Db = AsyncConnection | AsyncSession
Signal = Literal["ok", "hot", "near_traffic_limit"]

# Hetzner's "TB" is 1024^4 bytes: a 20 TB server reports included_traffic = 20 * 1024^4.
TB: Final = 1024**4
NODE_INTERVAL: Final = timedelta(minutes=15)
CHANNEL_INTERVAL: Final = timedelta(hours=1)
# A node created this recently started its traffic counter at zero when it was created.
NEW_NODE_WINDOW: Final = 2 * NODE_INTERVAL
# A sample older than this says nothing about the node's load now.
FRESH_FOR: Final = timedelta(minutes=45)
ACTOR: Final = "analytics"


def tehran_day(at: datetime) -> date:
    return at.astimezone(TEHRAN).date()


def minute(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(second=0, microsecond=0)


# --- which nodes are sampled ----------------------------------------------------------------------


@dataclass(frozen=True)
class SampledNode:
    """A node row the loop may sample: ours, live, with a Hetzner id, in an account we still read."""

    node_id: uuid.UUID
    account_id: uuid.UUID
    account_name: str
    hetzner_id: int
    name: str
    server_type: str
    location: str
    created_at: datetime


async def sampleable_nodes(db: Db) -> list[SampledNode]:
    rows = (
        await db.execute(
            select(
                Node.id,
                Node.account_id,
                HetznerAccount.name.label("account_name"),
                Node.hetzner_id,
                Node.name,
                Node.server_type,
                Node.location,
                Node.created_at,
            )
            .join(HetznerAccount, HetznerAccount.id == Node.account_id)
            .where(
                Node.state.in_([s.value for s in LIVE_STATES]),
                Node.hetzner_id.is_not(None),
                HetznerAccount.state.in_([s.value for s in OBSERVED_STATES]),
            )
            .order_by(Node.account_id, Node.name)
        )
    ).all()
    return [
        SampledNode(
            node_id=r.id,
            account_id=r.account_id,
            account_name=r.account_name,
            hetzner_id=int(r.hetzner_id),
            name=r.name,
            server_type=r.server_type,
            location=r.location,
            created_at=r.created_at,
        )
        for r in rows
    ]


async def last_node_sample_at(db: Db) -> datetime | None:
    value = (await db.execute(select(func.max(NodeSample.at)))).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


async def last_channel_sample_at(db: Db) -> datetime | None:
    value = (await db.execute(select(func.max(ChannelSample.at)))).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


# --- storing samples ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Counters:
    out_bytes: int | None
    in_bytes: int | None


async def last_counters(db: Db, node_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Counters]:
    ids = list(node_ids)
    if not ids:
        return {}
    latest = (
        select(NodeSample.node_id, func.max(NodeSample.at).label("at"))
        .where(NodeSample.node_id.in_(ids))
        .group_by(NodeSample.node_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(NodeSample.node_id, NodeSample.out_bytes, NodeSample.in_bytes).join(
                latest, and_(latest.c.node_id == NodeSample.node_id, latest.c.at == NodeSample.at)
            )
        )
    ).all()
    return {r.node_id: Counters(r.out_bytes, r.in_bytes) for r in rows}


def counter_delta(current: int | None, previous: int | None, *, first_counts: bool) -> int | None:
    """Bytes since the previous reading of a period counter (a lower value means it was reset)."""
    if current is None:
        return None
    if previous is None:
        return current if first_counts else 0
    return current - previous if current >= previous else current


async def store_node_sample(
    db: Db,
    node: SampledNode,
    usage: ServerUsage,
    load: LoadWindow | None,
    at: datetime,
    previous: Counters | None,
) -> bool:
    """Insert one sample (idempotent per node and minute). Returns False if it was already there."""
    first_counts = previous is None and at - node.created_at <= NEW_NODE_WINDOW
    prev_out = previous.out_bytes if previous else None
    prev_in = previous.in_bytes if previous else None
    values = {
        "node_id": node.node_id,
        "account_id": node.account_id,
        "hetzner_id": node.hetzner_id,
        "node_name": node.name,
        "server_type": node.server_type,
        "location": node.location,
        "at": at,
        "out_bytes": usage.out_bytes,
        "in_bytes": usage.in_bytes,
        "included_bytes": usage.included_bytes,
        "out_delta_bytes": counter_delta(usage.out_bytes, prev_out, first_counts=first_counts),
        "in_delta_bytes": counter_delta(usage.in_bytes, prev_in, first_counts=first_counts),
        "traffic_price_per_tb_eur": usage.traffic_price_per_tb_eur,
        "cpu_cores": usage.cores,
        "cpu_avg": load.cpu_avg if load else None,
        "cpu_max": load.cpu_max if load else None,
        "net_out_bps_avg": load.net_out_bps_avg if load else None,
        "net_in_bps_avg": load.net_in_bps_avg if load else None,
        "window_s": int((load.end - load.start).total_seconds()) if load else None,
    }
    stmt = pg_insert(NodeSample).values(**values).on_conflict_do_nothing().returning(NodeSample.id)
    return (await db.execute(stmt)).first() is not None


async def store_channel_sample(db: Db, role: str, chat_id: int, at: datetime, count: int) -> bool:
    stmt = (
        pg_insert(ChannelSample)
        .values(role=role, chat_id=chat_id, at=at, member_count=max(0, count))
        .on_conflict_do_nothing()
        .returning(ChannelSample.id)
    )
    return (await db.execute(stmt)).first() is not None


# --- load signals (for the dashboard and, later, the fleet) ---------------------------------------


@dataclass(frozen=True)
class CapacitySignal:
    node_id: str
    name: str
    signal: Signal
    # Percent of the included traffic used this billing period (Hetzner's counters), if known.
    traffic_pct: float | None
    # Average CPU of the whole server over the settings window, if enough samples cover it.
    cpu_window_avg: float | None
    cpu_window_max: float | None
    sampled_at: datetime | None
    # False when the newest sample is older than 45 minutes: then ``signal`` says nothing about now.
    fresh: bool


def classify(traffic_pct: float | None, cpu_window_avg: float | None, s: AnalyticsSettings) -> Signal:
    """Pure: the traffic limit wins (it costs money); then a hot CPU; else ok."""
    if traffic_pct is not None and traffic_pct >= s.traffic_warn_pct:
        return "near_traffic_limit"
    if cpu_window_avg is not None and cpu_window_avg >= s.cpu_warn_pct:
        return "hot"
    return "ok"


def samples_needed(window_min: int) -> int:
    """How many 15-minute samples must lie in the window before its CPU average counts (3 of 4 for 1 h)."""
    return max(1, window_min // 15 - 1)


@dataclass(frozen=True)
class _Live:
    node_id: uuid.UUID
    name: str


async def _live_nodes(db: Db) -> list[_Live]:
    rows = (
        await db.execute(
            select(Node.id, Node.name)
            .where(Node.state.in_([s.value for s in LIVE_STATES]))
            .order_by(Node.name)
        )
    ).all()
    return [_Live(r.id, r.name) for r in rows]


async def capacity_signals(
    db: Db, *, now: datetime | None = None, settings: AnalyticsSettings | None = None
) -> list[CapacitySignal]:
    """Per live node: ``ok`` / ``hot`` / ``near_traffic_limit`` from the stored samples. Reads only.

    For the fleet (a hot node may want a neighbour; a node near its traffic limit costs extra).
    A consumer must ignore a signal whose ``fresh`` is False.
    """
    now = now or datetime.now(UTC)
    s = settings or await load_settings(db)
    nodes = await _live_nodes(db)
    if not nodes:
        return []
    ids = [n.node_id for n in nodes]
    latest = (
        select(NodeSample.node_id, func.max(NodeSample.at).label("at"))
        .where(NodeSample.node_id.in_(ids))
        .group_by(NodeSample.node_id)
        .subquery()
    )
    last_rows = (
        await db.execute(
            select(NodeSample.node_id, NodeSample.at, NodeSample.out_bytes, NodeSample.included_bytes).join(
                latest, and_(latest.c.node_id == NodeSample.node_id, latest.c.at == NodeSample.at)
            )
        )
    ).all()
    last = {r.node_id: r for r in last_rows}
    since = now - timedelta(minutes=s.cpu_window_min)
    cpu_rows = (
        await db.execute(
            select(
                NodeSample.node_id,
                func.avg(NodeSample.cpu_avg).label("avg"),
                func.max(NodeSample.cpu_max).label("max"),
                func.count(NodeSample.cpu_avg).label("n"),
            )
            .where(NodeSample.node_id.in_(ids), NodeSample.at > since, NodeSample.at <= now)
            .group_by(NodeSample.node_id)
        )
    ).all()
    cpu = {r.node_id: r for r in cpu_rows}
    needed = samples_needed(s.cpu_window_min)
    out: list[CapacitySignal] = []
    for n in nodes:
        row = last.get(n.node_id)
        sampled_at = row.at if row is not None else None
        fresh = sampled_at is not None and now - sampled_at <= FRESH_FOR
        pct = traffic_pct(row.out_bytes, row.included_bytes) if row is not None and fresh else None
        c = cpu.get(n.node_id)
        cpu_avg = float(c.avg) if c is not None and c.avg is not None and int(c.n) >= needed else None
        cpu_max = float(c.max) if c is not None and c.max is not None and cpu_avg is not None else None
        out.append(
            CapacitySignal(
                node_id=str(n.node_id),
                name=n.name,
                signal=classify(pct, cpu_avg, s) if fresh else "ok",
                traffic_pct=pct,
                cpu_window_avg=cpu_avg,
                cpu_window_max=cpu_max,
                sampled_at=sampled_at,
                fresh=fresh,
            )
        )
    return out


def traffic_pct(out_bytes: int | None, included_bytes: int | None) -> float | None:
    if out_bytes is None or not included_bytes:
        return None
    return out_bytes * 100.0 / included_bytes


# --- owner alerts ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RaisedAlert:
    node_id: str
    name: str
    kind: Literal["traffic", "cpu"]
    value: float
    threshold: int
    outbox_id: int | None


async def _claim(
    db: Db, node_id: uuid.UUID, kind: str, day: date, now: datetime, value: float, threshold: int
) -> int | None:
    """The day's alert row for (node, kind), or None if one already exists (once per node per day)."""
    stmt = (
        pg_insert(ThresholdAlert)
        .values(node_id=node_id, kind=kind, day=day, at=now, value=value, threshold=float(threshold))
        .on_conflict_do_nothing()
        .returning(ThresholdAlert.id)
    )
    row = (await db.execute(stmt)).first()
    return None if row is None else int(row[0])


async def _included_tb(db: Db, node_id: uuid.UUID) -> tuple[float, float]:
    row = (
        await db.execute(
            select(NodeSample.out_bytes, NodeSample.included_bytes)
            .where(NodeSample.node_id == node_id)
            .order_by(NodeSample.at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return 0.0, 0.0
    return (row.out_bytes or 0) / TB, (row.included_bytes or 0) / TB


async def raise_threshold_alerts(
    db: Db, now: datetime, *, settings: AnalyticsSettings | None = None
) -> list[RaisedAlert]:
    """Persian owner alerts for nodes past a threshold: at most one per node, limit and Tehran day.

    Each one is also written to actions_log. Nothing is queued while alerts are off in the settings.
    """
    s = settings or await load_settings(db)
    if not s.alerts_enabled:
        return []
    day = tehran_day(now)
    raised: list[RaisedAlert] = []
    for sig in await capacity_signals(db, now=now, settings=s):
        if not sig.fresh:
            continue
        node_id = uuid.UUID(sig.node_id)
        checks: Sequence[tuple[Literal["traffic", "cpu"], float | None, int]] = (
            ("traffic", sig.traffic_pct, s.traffic_warn_pct),
            ("cpu", sig.cpu_window_avg, s.cpu_warn_pct),
        )
        for kind, value, threshold in checks:
            if value is None or value < threshold:
                continue
            alert_id = await _claim(db, node_id, kind, day, now, value, threshold)
            if alert_id is None:
                continue
            if kind == "traffic":
                used, included = await _included_tb(db, node_id)
                text = traffic_alert_fa(sig.name, value, threshold, used, included)
            else:
                text = cpu_alert_fa(sig.name, value, threshold, s.cpu_window_min)
            outbox_id = await enqueue_owner_alert(db, text)
            if outbox_id is not None:
                await db.execute(
                    update(ThresholdAlert).where(ThresholdAlert.id == alert_id).values(outbox_id=outbox_id)
                )
            await log_action(
                db,
                actor=ACTOR,
                action=f"analytics.{kind}_warning",
                target=sig.name,
                dry_run=False,
                allowed=True,
                reasons=[f"{kind}_over_threshold"],
                context={
                    "node_id": sig.node_id,
                    "value_pct": round(value, 1),
                    "threshold_pct": threshold,
                    "alert_queued": outbox_id is not None,
                },
                result=f"{kind} {value:.0f}% >= {threshold}%",
            )
            raised.append(RaisedAlert(sig.node_id, sig.name, kind, value, threshold, outbox_id))
    return raised


def money(value: Decimal) -> str:
    """Euros as a decimal string, two places (display only)."""
    return str(value.quantize(Decimal("0.01")))
