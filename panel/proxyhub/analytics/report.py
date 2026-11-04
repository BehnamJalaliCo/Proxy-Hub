"""The numbers of the dashboard's «آمار» page, from the analytics tables only (aggregates; rule 7).

Units: bytes as integers (the page formats them; Hetzner's TB is 1024^4 bytes), money as decimal
strings in EUR (net, as Hetzner prices are stored), times ISO-8601 UTC. "Today" and day buckets are
Tehran days; "this month" is the UTC calendar month, the same month Hetzner bills and the budget uses.

Every series that has no data yet comes back empty (never invented, decision D10).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.analytics.sampling import TB, capacity_signals, tehran_day
from proxyhub.analytics.settings import AnalyticsSettings, load_settings
from proxyhub.db.models_analytics import ChannelSample, ChannelStatsSnapshot, MemberEvent, NodeSample
from proxyhub.fleet.actions import iso
from proxyhub.fleet.costs import NodeCostInput, with_upper_bound_prices
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.fleet.snapshot import load_nodes, load_offers
from proxyhub.safety.budget import CostItem, Price, month_bounds, projected_month_total
from proxyhub.telegram import store
from proxyhub.telegram.schedule import TEHRAN

Db = AsyncConnection | AsyncSession

STATUS_KEY: Final = "analytics.status"
MAX_DAYS: Final = 90
MAX_POINTS: Final = 500
TOP_SOURCES: Final = 8


def _f(value: Any) -> float | None:
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _int(value: Any) -> int:
    return int(value or 0)


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def tehran_midnight(now: datetime) -> datetime:
    local = now.astimezone(TEHRAN)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def _billed_hours(start: datetime, end: datetime) -> int:
    return max(0, math.ceil((end - start).total_seconds() / 3600))


def month_to_date_cost(items: list[CostItem], now: datetime) -> Decimal:
    """What the month cost so far: started hours of each server up to now, at most its monthly price."""
    start, _ = month_bounds(now)
    total = Decimal(0)
    for item in items:
        if item.deleted_at is not None and item.deleted_at < start:
            continue
        begin = max(item.created_at, start)
        finish = min(item.deleted_at, now) if item.deleted_at is not None else now
        if finish <= begin:
            continue
        total += min(item.price.hourly * _billed_hours(begin, finish), item.price.monthly)
    return total


# --- traffic ------------------------------------------------------------------------------------------


async def _sum_out(db: Db, since: datetime, until: datetime | None = None) -> tuple[int, int]:
    stmt = select(
        func.coalesce(func.sum(NodeSample.out_delta_bytes), 0),
        func.coalesce(func.sum(NodeSample.in_delta_bytes), 0),
    ).where(NodeSample.at >= since)
    if until is not None:
        stmt = stmt.where(NodeSample.at < until)
    out, inn = (await db.execute(stmt)).one()
    return int(out or 0), int(inn or 0)


async def _node_rows(db: Db, month_start: datetime) -> list[dict[str, Any]]:
    """One row per node that is live or has samples this month (deleted nodes keep their history)."""
    rows = (
        await db.execute(
            text(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (node_id) node_id, at, out_bytes, in_bytes, included_bytes,
                         traffic_price_per_tb_eur, net_out_bps_avg, node_name, server_type, location
                  FROM analytics_node_samples ORDER BY node_id, at DESC
                ), month AS (
                  SELECT node_id, sum(out_delta_bytes) AS out_month, sum(in_delta_bytes) AS in_month
                  FROM analytics_node_samples WHERE at >= :start GROUP BY node_id
                )
                SELECT COALESCE(n.id, l.node_id) AS node_id,
                       COALESCE(n.name, l.node_name) AS name,
                       COALESCE(n.location, l.location) AS location,
                       COALESCE(n.server_type, l.server_type) AS server_type,
                       n.state AS state, n.is_test AS is_test,
                       l.at AS sampled_at, l.out_bytes, l.included_bytes, l.traffic_price_per_tb_eur,
                       l.net_out_bps_avg, m.out_month, m.in_month
                FROM latest l
                FULL OUTER JOIN nodes n ON n.id = l.node_id
                LEFT JOIN month m ON m.node_id = COALESCE(n.id, l.node_id)
                WHERE (n.id IS NOT NULL AND n.state <> 'deleted') OR m.node_id IS NOT NULL
                ORDER BY 2
                """
            ),
            {"start": month_start},
        )
    ).mappings()
    return [dict(r) for r in rows]


def overage_cost(rows: list[dict[str, Any]], month_start: datetime) -> Decimal:
    total = Decimal(0)
    for r in rows:
        at, out, inc, price = (
            r["sampled_at"],
            r["out_bytes"],
            r["included_bytes"],
            r["traffic_price_per_tb_eur"],
        )
        if at is None or at < month_start or out is None or inc is None or price is None:
            continue
        if out > inc:
            total += Decimal(out - inc) / Decimal(TB) * Decimal(price)
    return total


async def _cost(db: Db, now: datetime) -> tuple[Decimal, Decimal, dict[str, Decimal], list[str]]:
    """(month to date, projected month, per node id, nodes priced by an upper bound)."""
    nodes = await load_nodes(db, now)
    prices = {
        (ao.account_id, ao.offer.server_type, ao.offer.location): ao.offer.price
        for ao in await load_offers(db)
    }
    filled = with_upper_bound_prices(nodes, prices)
    so_far = Decimal(0)
    items: list[CostItem] = []
    per_node: dict[str, Decimal] = {}
    guessed: list[str] = []
    for n in nodes:
        item = _cost_item(n, filled, now)
        if item is None:
            continue
        if (n.account_id, n.server_type, n.location) not in prices:
            guessed.append(n.name)
        items.append(item)
        cost = month_to_date_cost([item], now)
        per_node[n.id] = cost
        so_far += cost
    return so_far, projected_month_total(items, now), per_node, guessed


def _cost_item(
    node: NodeCostInput, prices: dict[tuple[str, str, str], Price], now: datetime
) -> CostItem | None:
    deleted_at = node.deleted_at
    if node.state == NodeState.DELETED:
        if node.hetzner_id is None:
            return None
        deleted_at = deleted_at or now
    price = prices.get((node.account_id, node.server_type, node.location))
    if price is None:
        return None
    return CostItem(price=price, created_at=node.created_at, deleted_at=deleted_at)


# --- members ------------------------------------------------------------------------------------------


async def _main_chat(db: Db) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    channels = await store.channels(db)
    return channels.get("main"), channels.get("test")


async def _latest_count(db: Db, chat_id: int) -> tuple[int | None, datetime | None]:
    row = (
        await db.execute(
            select(ChannelSample.member_count, ChannelSample.at)
            .where(ChannelSample.chat_id == chat_id)
            .order_by(ChannelSample.at.desc())
            .limit(1)
        )
    ).first()
    return (None, None) if row is None else (int(row.member_count), row.at)


async def _moves(db: Db, chat_id: int, since: datetime) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(MemberEvent.kind, func.count())
            .where(MemberEvent.chat_id == chat_id, MemberEvent.at >= since)
            .group_by(MemberEvent.kind)
        )
    ).all()
    counts = {str(k): int(n) for k, n in rows}
    return counts.get("join", 0), counts.get("leave", 0)


async def sources(db: Db, chat_id: int, since: datetime, limit: int = TOP_SOURCES) -> list[dict[str, Any]]:
    """Joins, leaves and net per source since ``since``, most joins first (leaves follow the join)."""
    rows = (
        await db.execute(
            text(
                """
                SELECT e.source, e.tracking_link_id, COALESCE(t.name, e.link_name) AS name,
                       count(*) FILTER (WHERE e.kind = 'join') AS joins,
                       count(*) FILTER (WHERE e.kind = 'leave') AS leaves
                FROM analytics_member_events e
                LEFT JOIN analytics_tracking_links t ON t.id = e.tracking_link_id
                WHERE e.chat_id = :chat AND e.at >= :since
                GROUP BY e.source, e.tracking_link_id, COALESCE(t.name, e.link_name)
                ORDER BY 4 DESC, 5 ASC, 3
                LIMIT :limit
                """
            ),
            {"chat": chat_id, "since": since, "limit": limit},
        )
    ).mappings()
    out: list[dict[str, Any]] = []
    for r in rows:
        kind = "tracking" if r["tracking_link_id"] is not None else str(r["source"])
        key = f"tracking:{r['tracking_link_id']}" if kind == "tracking" else f"{kind}:{r['name'] or ''}"
        joins, leaves = int(r["joins"]), int(r["leaves"])
        out.append(
            {
                "key": key,
                "kind": kind,
                "name": r["name"],
                "joins": joins,
                "leaves": leaves,
                "net": joins - leaves,
            }
        )
    return out


# --- summary ------------------------------------------------------------------------------------------


async def summary(db: Db, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    s = await load_settings(db)
    month_start, month_end = month_bounds(now)
    prev_start, _ = month_bounds(month_start - timedelta(seconds=1))
    today = tehran_midnight(now)

    month_out, month_in = await _sum_out(db, month_start)
    last_month_out, _ = await _sum_out(db, prev_start, month_start)
    today_out, _ = await _sum_out(db, today)
    first, last = (await db.execute(select(func.min(NodeSample.at), func.max(NodeSample.at)))).one()
    covered_from = max(month_start, first) if first is not None else None
    covered_days = (now - covered_from).total_seconds() / 86400 if covered_from is not None else 0.0
    avg_day = int(month_out / covered_days) if covered_days >= 1 / 24 else None

    rows = await _node_rows(db, month_start)
    so_far, projected, per_node, guessed = await _cost(db, now)
    overage = overage_cost(rows, month_start)
    total_cost = so_far + overage
    per_tb = money(total_cost / (Decimal(month_out) / Decimal(TB))) if month_out > 0 else None

    signals = {sig.node_id: sig for sig in await capacity_signals(db, now=now, settings=s)}
    servers: list[dict[str, Any]] = []
    for r in rows:
        node_id = str(r["node_id"])
        sig = signals.get(node_id)
        out_b, inc_b = r["out_bytes"], r["included_bytes"]
        servers.append(
            {
                "node_id": node_id,
                "name": r["name"],
                "location": r["location"],
                "server_type": r["server_type"],
                "state": r["state"] or "deleted",
                "is_test": bool(r["is_test"]),
                "month_out_bytes": _int(r["out_month"]),
                "month_in_bytes": _int(r["in_month"]),
                "period_out_bytes": out_b,
                "included_bytes": inc_b,
                "traffic_pct": (out_b * 100.0 / inc_b) if out_b is not None and inc_b else None,
                "net_out_bps": _f(r["net_out_bps_avg"]),
                "cpu_avg": sig.cpu_window_avg if sig else None,
                "cpu_max": sig.cpu_window_max if sig else None,
                "signal": sig.signal if sig else "ok",
                "fresh": bool(sig.fresh) if sig else False,
                "sampled_at": iso(r["sampled_at"]),
                "cost_month_eur": money(per_node[node_id]) if node_id in per_node else None,
            }
        )
    warnings = [
        {
            "kind": "traffic" if sig.signal == "near_traffic_limit" else "cpu",
            "node_id": sig.node_id,
            "name": sig.name,
            "value": sig.traffic_pct if sig.signal == "near_traffic_limit" else sig.cpu_window_avg,
            "threshold": s.traffic_warn_pct if sig.signal == "near_traffic_limit" else s.cpu_warn_pct,
        }
        for sig in signals.values()
        if sig.fresh and sig.signal != "ok"
    ]
    # A node near its traffic limit AND hot shows both.
    for sig in signals.values():
        if (
            sig.fresh
            and sig.signal == "near_traffic_limit"
            and sig.cpu_window_avg is not None
            and sig.cpu_window_avg >= s.cpu_warn_pct
        ):
            warnings.append(
                {
                    "kind": "cpu",
                    "node_id": sig.node_id,
                    "name": sig.name,
                    "value": sig.cpu_window_avg,
                    "threshold": s.cpu_warn_pct,
                }
            )

    main, test = await _main_chat(db)
    channel: dict[str, Any] = {"main": None, "test": None}
    top: list[dict[str, Any]] = []
    for role, ch in (("main", main), ("test", test)):
        chat_id = ch.get("chat_id") if ch else None
        if not isinstance(chat_id, int):
            continue
        members, members_at = await _latest_count(db, chat_id)
        info: dict[str, Any] = {
            "title": str(ch.get("title") or "") if ch else "",
            "username": str(ch.get("username") or "") if ch else "",
            "members": members,
            "members_at": iso(members_at),
        }
        for label, since in (
            ("today", today),
            ("7d", now - timedelta(days=7)),
            ("30d", now - timedelta(days=30)),
        ):
            joins, leaves = await _moves(db, chat_id, since)
            info[f"joins_{label}"], info[f"leaves_{label}"], info[f"net_{label}"] = (
                joins,
                leaves,
                joins - leaves,
            )
        first_event = (
            await db.execute(select(func.min(MemberEvent.at)).where(MemberEvent.chat_id == chat_id))
        ).scalar_one_or_none()
        info["tracking_since"] = iso(first_event)
        channel[role] = info
        if role == "main":
            top = await sources(db, chat_id, now - timedelta(days=30))

    status = await store.get_setting(db, STATUS_KEY) or {}
    return {
        "generated_at": iso(now),
        "month": {"start": iso(month_start), "end": iso(month_end), "days_covered": round(covered_days, 3)},
        "traffic": {
            "month_out_bytes": month_out,
            "month_in_bytes": month_in,
            "today_out_bytes": today_out,
            "avg_day_out_bytes": avg_day,
            "last_month_out_bytes": last_month_out,
            "sampling_since": iso(first),
            "last_sample_at": iso(last),
        },
        "cost": {
            "month_to_date_eur": money(so_far),
            "traffic_overage_eur": money(overage),
            "total_month_to_date_eur": money(total_cost),
            "projected_month_eur": money(projected),
            "per_tb_eur": per_tb,
            "priced_by_upper_bound": guessed,
        },
        "servers": servers,
        "warnings": warnings,
        "channel": channel,
        "top_sources": top,
        "thresholds": s.model_dump(),
        "collector": {
            part: status.get(part)
            for part in ("nodes", "channels", "channel_stats")
            if isinstance(status, dict)
        },
    }


# --- series -------------------------------------------------------------------------------------------


def _day_list(now: datetime, days: int) -> list[date]:
    last = tehran_day(now)
    return [last - timedelta(days=i) for i in range(days - 1, -1, -1)]


async def series(db: Db, now: datetime | None = None, days: int = 30) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    days = max(1, min(days, MAX_DAYS))
    day_list = _day_list(now, days)
    since = datetime.combine(day_list[0], datetime.min.time(), tzinfo=TEHRAN).astimezone(UTC)

    traffic = {
        r.day: (int(r.out_b or 0), int(r.in_b or 0))
        for r in (
            await db.execute(
                text(
                    "SELECT (at AT TIME ZONE 'Asia/Tehran')::date AS day, sum(out_delta_bytes) AS out_b, "
                    "sum(in_delta_bytes) AS in_b FROM analytics_node_samples WHERE at >= :since GROUP BY 1"
                ),
                {"since": since},
            )
        ).all()
    }
    main, _ = await _main_chat(db)
    chat_id = main.get("chat_id") if main else None
    moves: dict[date, tuple[int, int]] = {}
    members: list[dict[str, Any]] = []
    top: list[dict[str, Any]] = []
    if isinstance(chat_id, int):
        for r in (
            await db.execute(
                text(
                    "SELECT (at AT TIME ZONE 'Asia/Tehran')::date AS day, "
                    "count(*) FILTER (WHERE kind = 'join') AS j, count(*) FILTER (WHERE kind = 'leave') AS l "
                    "FROM analytics_member_events WHERE chat_id = :chat AND at >= :since GROUP BY 1"
                ),
                {"chat": chat_id, "since": since},
            )
        ).all():
            moves[r.day] = (int(r.j), int(r.l))
        samples = (
            await db.execute(
                select(ChannelSample.at, ChannelSample.member_count)
                .where(ChannelSample.chat_id == chat_id, ChannelSample.at >= since)
                .order_by(ChannelSample.at)
            )
        ).all()
        step = max(1, math.ceil(len(samples) / MAX_POINTS))
        picked = list(samples[::step])
        if samples and picked[-1] is not samples[-1]:
            picked.append(samples[-1])
        members = [{"at": iso(r.at), "count": int(r.member_count)} for r in picked]
        top = await sources(db, chat_id, since, limit=TOP_SOURCES)

    cpu_since = max(since, now - timedelta(days=7))
    cpu = [
        {"at": iso(r.hour), "avg": _f(r.avg), "max": _f(r.max), "nodes": int(r.nodes)}
        for r in (
            await db.execute(
                text(
                    "SELECT date_trunc('hour', at) AS hour, avg(cpu_avg) AS avg, max(cpu_avg) AS max, "
                    "count(DISTINCT node_id) AS nodes FROM analytics_node_samples "
                    "WHERE at >= :since AND cpu_avg IS NOT NULL GROUP BY 1 ORDER BY 1"
                ),
                {"since": cpu_since},
            )
        ).all()
    ]
    months: list[dict[str, Any]] = []
    start, end = month_bounds(now)
    for _ in range(6):
        months.append(
            {"start": iso(start), "end": iso(end), "out_bytes": (await _sum_out(db, start, end))[0]}
        )
        end = start
        start, _ = month_bounds(start - timedelta(seconds=1))
    months.reverse()
    return {
        "days": [
            {
                "day": d.isoformat(),
                "out_bytes": traffic.get(d, (0, 0))[0] if d in traffic else None,
                "in_bytes": traffic.get(d, (0, 0))[1] if d in traffic else None,
                "joins": moves.get(d, (0, 0))[0] if isinstance(chat_id, int) else None,
                "leaves": moves.get(d, (0, 0))[1] if isinstance(chat_id, int) else None,
            }
            for d in day_list
        ],
        "months": months,
        "cpu": cpu,
        "members": members,
        "sources": top,
    }


# --- proxy users (D39) --------------------------------------------------------------------------------


async def usage(db: Db, now: datetime | None = None, days: int = 30) -> dict[str, Any]:
    """Users and traffic per country / region / ISP, per proxy, peak hours; from the agents' counters."""
    now = now or datetime.now(UTC)
    days = max(1, min(days, MAX_DAYS))
    since_day = now.date() - timedelta(days=days - 1)
    per_day = [
        {"day": r.day.isoformat(), "unique_users": int(r.users or 0), "nodes": int(r.nodes)}
        for r in (
            await db.execute(
                text(
                    "SELECT day, sum(u) AS users, count(*) AS nodes FROM ("
                    " SELECT node_id, day, max(unique_users) AS u FROM analytics_usage_daily"
                    " WHERE day >= :since GROUP BY node_id, day) x GROUP BY day ORDER BY day"
                ),
                {"since": since_day},
            )
        ).all()
    ]
    dims: dict[str, list[dict[str, Any]]] = {}
    for dim in ("country", "region", "asn"):
        rows = (
            await db.execute(
                text(
                    "SELECT key, sum(users) AS user_days, count(DISTINCT day) AS days, sum(bytes) AS bytes "
                    "FROM analytics_usage_dims WHERE dim = :dim AND day >= :since GROUP BY key "
                    "ORDER BY 2 DESC, 1 LIMIT 30"
                ),
                {"dim": dim, "since": since_day},
            )
        ).all()
        dims[dim] = [
            {
                "key": r.key,
                "avg_daily_users": round(int(r.user_days) / max(1, int(r.days)), 1),
                "user_days": int(r.user_days),
                "bytes": int(r.bytes),
            }
            for r in rows
        ]
    pct = (
        await db.execute(
            text(
                "SELECT sum(p50_mb * unique_users) / NULLIF(sum(unique_users), 0) AS p50, "
                "sum(p90_mb * unique_users) / NULLIF(sum(unique_users), 0) AS p90, "
                "sum(p99_mb * unique_users) / NULLIF(sum(unique_users), 0) AS p99, "
                "sum(unique_users) AS users FROM analytics_usage_daily "
                "WHERE day = (SELECT max(day) FROM analytics_usage_daily WHERE p50_mb IS NOT NULL) "
                "AND p50_mb IS NOT NULL"
            )
        )
    ).one()
    pct_day = (
        await db.execute(text("SELECT max(day) FROM analytics_usage_daily WHERE p50_mb IS NOT NULL"))
    ).scalar_one_or_none()
    heat_rows = (
        await db.execute(
            text(
                "SELECT extract(isodow FROM t) AS dow, extract(hour FROM t) AS h, sum(b) AS bytes, "
                "max(c) AS conns FROM (SELECT ((day + make_interval(hours => hour)) AT TIME ZONE 'UTC') "
                "AT TIME ZONE 'Asia/Tehran' AS t, bytes_up + bytes_down AS b, conns_max AS c "
                "FROM analytics_usage_hourly WHERE day >= :since) x GROUP BY 1, 2"
            ),
            {"since": since_day},
        )
    ).all()
    heatmap = [
        {"dow": int(r.dow), "hour": int(r.h), "bytes": int(r.bytes or 0), "conns": int(r.conns or 0)}
        for r in heat_rows
    ]
    proxies = [
        {
            "kind": r.kind,
            "ref": r.ref,
            "bytes_up": int(r.up),
            "bytes_down": int(r.down),
            "conns_max": int(r.conns),
        }
        for r in (
            await db.execute(
                text(
                    "SELECT kind, ref, sum(bytes_up) AS up, sum(bytes_down) AS down, max(conns_max) AS conns "
                    "FROM analytics_usage_hourly WHERE day >= :since GROUP BY kind, ref "
                    "ORDER BY sum(bytes_up + bytes_down) DESC LIMIT 20"
                ),
                {"since": since_day},
            )
        ).all()
    ]
    hourly_since = now - timedelta(hours=48)
    hourly = [
        {"at": iso(r.hour_at), "bytes_up": int(r.up), "bytes_down": int(r.down)}
        for r in (
            await db.execute(
                text(
                    "SELECT hour_at, sum(bytes_up) AS up, sum(bytes_down) AS down FROM ("
                    " SELECT (day + make_interval(hours => hour)) AT TIME ZONE 'UTC' AS hour_at,"
                    " bytes_up, bytes_down FROM analytics_usage_hourly WHERE day >= :d) x"
                    " WHERE hour_at >= :t GROUP BY hour_at ORDER BY hour_at"
                ),
                {"d": hourly_since.date(), "t": hourly_since},
            )
        ).all()
    ]
    has_data = bool(per_day or heatmap or proxies)
    return {
        "has_data": has_data,
        "min_users": 3,
        "days": per_day,
        "by_country": dims["country"],
        "by_region": dims["region"],
        "by_asn": dims["asn"],
        "per_user_mb": {
            "day": pct_day.isoformat() if pct_day else None,
            "p50": _f(pct.p50),
            "p90": _f(pct.p90),
            "p99": _f(pct.p99),
            "users": int(pct.users or 0),
        },
        "heatmap": heatmap,
        "proxies": proxies,
        "hourly": hourly,
    }


# --- channel statistics (D38) and post views -----------------------------------------------------------


async def channel_stats(db: Db) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for role in ("main", "test"):
        row = (
            await db.execute(
                select(ChannelStatsSnapshot.__table__)
                .where(ChannelStatsSnapshot.role == role)
                .order_by(ChannelStatsSnapshot.at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            out[role] = None
            continue
        out[role] = {
            "at": iso(row.at),
            "status": row.status,
            "participants": row.participants,
            "period_start": iso(row.period_start),
            "period_end": iso(row.period_end),
            "followers": row.followers,
            "followers_prev": row.followers_prev,
            "views_per_post": row.views_per_post,
            "views_per_post_prev": row.views_per_post_prev,
            "shares_per_post": row.shares_per_post,
            "shares_per_post_prev": row.shares_per_post_prev,
            "reactions_per_post": row.reactions_per_post,
            "reactions_per_post_prev": row.reactions_per_post_prev,
            "notifications_pct": row.notifications_pct,
            "graphs": row.graphs,
        }
    posts = [
        {
            "message_id": int(r.message_id),
            "views": int(r.views),
            "forwards": int(r.forwards),
            "reactions": r.reactions,
            "at": iso(r.at),
            "posted_at": iso(r.posted_at),
            "kind": r.kind,
        }
        for r in (
            await db.execute(
                text(
                    "SELECT v.message_id, v.views, v.forwards, v.reactions, v.at, "
                    "p.created_at AS posted_at, p.kind FROM analytics_post_views v LEFT JOIN channel_posts p "
                    "ON p.chat_id = v.chat_id AND p.message_id = v.message_id "
                    "WHERE v.role = 'main' ORDER BY v.message_id DESC LIMIT 30"
                )
            )
        ).all()
    ]
    return {"main": out.get("main"), "test": out.get("test"), "posts": posts}


# --- tracking links -----------------------------------------------------------------------------------


async def links(db: Db, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    week = now - timedelta(days=7)
    rows = (
        await db.execute(
            text(
                """
                SELECT t.id, t.name, t.purpose, t.invite_link, t.created_at, t.renamed_at,
                       count(e.id) FILTER (WHERE e.kind = 'join') AS joins,
                       count(e.id) FILTER (WHERE e.kind = 'leave') AS leaves,
                       count(e.id) FILTER (WHERE e.kind = 'join' AND e.at >= :week) AS joins_7d,
                       max(e.at) FILTER (WHERE e.kind = 'join') AS last_join_at
                FROM analytics_tracking_links t
                LEFT JOIN analytics_member_events e ON e.tracking_link_id = t.id
                GROUP BY t.id ORDER BY t.created_at DESC, t.id DESC
                """
            ),
            {"week": week},
        )
    ).mappings()
    return [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "purpose": r["purpose"],
            "invite_link": r["invite_link"],
            "created_at": iso(r["created_at"]),
            "renamed_at": iso(r["renamed_at"]),
            "joins": int(r["joins"]),
            "leaves": int(r["leaves"]),
            "net": int(r["joins"]) - int(r["leaves"]),
            "joins_7d": int(r["joins_7d"]),
            "last_join_at": iso(r["last_join_at"]),
        }
        for r in rows
    ]


async def capacity(
    db: Db, now: datetime | None = None, s: AnalyticsSettings | None = None
) -> list[dict[str, Any]]:
    return [
        {
            "node_id": sig.node_id,
            "name": sig.name,
            "signal": sig.signal,
            "traffic_pct": sig.traffic_pct,
            "cpu_avg": sig.cpu_window_avg,
            "cpu_max": sig.cpu_window_max,
            "sampled_at": iso(sig.sampled_at),
            "fresh": sig.fresh,
        }
        for sig in await capacity_signals(db, now=now, settings=s)
    ]
