"""Our channels' statistics through the operator account (decision D38).

Telegram gives a channel's statistics (followers over time, views and shares per post, views by
source, new followers by source, languages, top hours) only to an admin USER account, never to a
bot. The operator account (D37) reads them every 3 hours, for the configured main and test channels
only, and only while the fleet's kill switch is OFF and the account is connected:

1. ``contacts.resolveUsername`` of the channel's public username (its id must be the configured one),
2. ``channels.getFullChannel``: participant count, ``stats_dc`` and whether stats may be viewed,
3. ``stats.getBroadcastStats`` (+ ``stats.loadAsyncGraph`` for graphs Telegram sends later), only
   when the stats live on the account's own data centre (another one would need an authorization
   export, which is not allowed: status ``stats_dc_other``),
4. ``messages.getMessagesViews`` of our own posts (``channel_posts``), never counting a view.

Everything is Telegram's own aggregate numbers; nothing about a person is read or stored. This
module never imports Telethon (only proxyhub/mtproxy/userclient.py does): the TL objects it reads
are handled by attribute name.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.db.models_analytics import ChannelStatsSnapshot, PostViews
from proxyhub.db.models_telegram import ChannelPost
from proxyhub.mtproxy import state
from proxyhub.mtproxy import store as mtp_store
from proxyhub.mtproxy.userclient import ChannelRef, ChannelScope, ClientOptions, MtpError, operator_session
from proxyhub.telegram import store as tg_store
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

# The graphs the owner asked for, by BroadcastStats attribute -> stored name.
GRAPHS: Final[dict[str, str]] = {
    "followers_graph": "followers",
    "growth_graph": "growth",
    "views_by_source_graph": "views_by_source",
    "new_followers_by_source_graph": "new_followers_by_source",
    "languages_graph": "languages",
    "top_hours_graph": "top_hours",
    "interactions_graph": "interactions",
}
MAX_POINTS: Final = 400
MAX_SERIES: Final = 24
MAX_LABEL: Final = 64
MAX_ASYNC_GRAPHS: Final = len(GRAPHS)
POSTS_PER_READ: Final = 100
CHANNEL_ID_OFFSET: Final = 10**12  # Bot API id of a channel is -(10^12 + channel_id)


def bare_channel_id(chat_id: int) -> int | None:
    """Bot API ``-100xxxxxxxxxx`` -> MTProto channel id; None for anything else."""
    if chat_id >= -CHANNEL_ID_OFFSET:
        return None
    return -chat_id - CHANNEL_ID_OFFSET


# --- parsing Telegram's TL objects (by attribute; no Telethon import here) ----------------------------


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_graph_json(data: str) -> dict[str, Any] | None:
    """A StatsGraph's ``json.data`` (Telegram's chart JSON: ``columns``, ``names``, ``types``) ->
    ``{"x": [...], "series": [{"key", "name", "type", "values"}]}``, bounded in size."""
    try:
        doc = json.loads(data)
    except (TypeError, ValueError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("columns"), list):
        return None
    raw_names, raw_kinds = doc.get("names"), doc.get("types")
    names: dict[str, Any] = raw_names if isinstance(raw_names, dict) else {}
    kinds: dict[str, Any] = raw_kinds if isinstance(raw_kinds, dict) else {}
    x: list[float] = []
    series: list[dict[str, Any]] = []
    for column in doc["columns"]:
        if not isinstance(column, list) or not column or not isinstance(column[0], str):
            continue
        key, values = column[0], [_num(v) for v in column[1:]][-MAX_POINTS:]
        if kinds.get(key) == "x" or key == "x":
            x = [v for v in values if v is not None]
            continue
        if len(series) >= MAX_SERIES:
            continue
        name = names.get(key)
        series.append(
            {
                "key": key[:16],
                "name": str(name)[:MAX_LABEL] if isinstance(name, str) else key[:16],
                "type": str(kinds.get(key, "line"))[:16],
                "values": [0.0 if v is None else v for v in values],
            }
        )
    out: dict[str, Any] = {"x": x, "series": series}
    if doc.get("percentage") is True:
        out["percentage"] = True
    if doc.get("stacked") is True:
        out["stacked"] = True
    return out


def parse_graph(graph: Any) -> dict[str, Any] | None:
    """StatsGraph -> parsed; StatsGraphAsync -> ``{"async_token": ...}``; StatsGraphError -> error."""
    kind = type(graph).__name__
    if kind == "StatsGraph":
        data = getattr(getattr(graph, "json", None), "data", None)
        return parse_graph_json(data) if isinstance(data, str) else None
    if kind == "StatsGraphAsync":
        token = getattr(graph, "token", None)
        return {"async_token": str(token)} if token else None
    if kind == "StatsGraphError":
        return {"error": str(getattr(graph, "error", ""))[:MAX_LABEL]}
    return None


def _abs(value: Any) -> tuple[float | None, float | None]:
    """StatsAbsValueAndPrev -> (current, previous)."""
    return _num(getattr(value, "current", None)), _num(getattr(value, "previous", None))


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    return None


@dataclass
class Snapshot:
    role: str
    chat_id: int
    status: str = "ok"
    participants: int | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    numbers: dict[str, float | None] = field(default_factory=dict)
    graphs: dict[str, Any] = field(default_factory=dict)
    # (message id, views, forwards, reactions or None)
    posts: list[tuple[int, int, int, int | None]] = field(default_factory=list)


def parse_broadcast_stats(stats: Any, snap: Snapshot) -> list[tuple[str, str]]:
    """Fill ``snap`` from a BroadcastStats. Returns (stored graph name, async token) to load later."""
    period = getattr(stats, "period", None)
    snap.period_start = _when(getattr(period, "min_date", None))
    snap.period_end = _when(getattr(period, "max_date", None))
    for attr in ("followers", "views_per_post", "shares_per_post", "reactions_per_post"):
        current, previous = _abs(getattr(stats, attr, None))
        snap.numbers[attr] = current
        snap.numbers[f"{attr}_prev"] = previous
    notes = getattr(stats, "enabled_notifications", None)
    part, total = _num(getattr(notes, "part", None)), _num(getattr(notes, "total", None))
    snap.numbers["notifications_pct"] = part * 100.0 / total if part is not None and total else None
    pending: list[tuple[str, str]] = []
    for attr, name in GRAPHS.items():
        parsed = parse_graph(getattr(stats, attr, None))
        if parsed is None:
            continue
        if "async_token" in parsed:
            pending.append((name, parsed["async_token"]))
        else:
            snap.graphs[name] = parsed
    for item in getattr(stats, "recent_posts_interactions", None) or []:
        if type(item).__name__ != "PostInteractionCountersMessage":
            continue
        msg_id = getattr(item, "msg_id", None)
        if isinstance(msg_id, int):
            snap.posts.append(
                (
                    msg_id,
                    int(getattr(item, "views", 0) or 0),
                    int(getattr(item, "forwards", 0) or 0),
                    int(getattr(item, "reactions", 0) or 0),
                )
            )
    return pending


# --- one collection run ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    role: str
    chat_id: int
    channel_id: int
    username: str


async def _targets(engine: AsyncEngine) -> list[Target]:
    async with engine.connect() as conn:
        channels = await tg_store.channels(conn)
    out: list[Target] = []
    for role, ch in channels.items():
        chat_id, username = ch.get("chat_id"), str(ch.get("username") or "")
        if not isinstance(chat_id, int) or not username.startswith("@"):
            continue  # a private channel without a username cannot be resolved by this account
        channel_id = bare_channel_id(chat_id)
        if channel_id is not None:
            out.append(Target(role, chat_id, channel_id, username[1:].lower()))
    return out


async def _our_post_ids(engine: AsyncEngine, role: str, chat_id: int) -> list[int]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(ChannelPost.message_id)
                .where(ChannelPost.role == role, ChannelPost.chat_id == chat_id)
                .order_by(ChannelPost.created_at.desc())
                .limit(POSTS_PER_READ)
            )
        ).scalars()
        return [int(m) for m in rows]


async def store_snapshot(engine: AsyncEngine, snap: Snapshot, now: datetime) -> None:
    n = snap.numbers
    async with engine.begin() as conn:
        await conn.execute(
            pg_insert(ChannelStatsSnapshot).values(
                role=snap.role,
                chat_id=snap.chat_id,
                at=now,
                status=snap.status[:40],
                participants=snap.participants,
                period_start=snap.period_start,
                period_end=snap.period_end,
                followers=n.get("followers"),
                followers_prev=n.get("followers_prev"),
                views_per_post=n.get("views_per_post"),
                views_per_post_prev=n.get("views_per_post_prev"),
                shares_per_post=n.get("shares_per_post"),
                shares_per_post_prev=n.get("shares_per_post_prev"),
                reactions_per_post=n.get("reactions_per_post"),
                reactions_per_post_prev=n.get("reactions_per_post_prev"),
                notifications_pct=n.get("notifications_pct"),
                graphs=snap.graphs,
            )
        )
        for msg_id, views, forwards, reactions in snap.posts:
            stmt = pg_insert(PostViews).values(
                role=snap.role,
                chat_id=snap.chat_id,
                message_id=msg_id,
                views=max(0, views),
                forwards=max(0, forwards),
                reactions=reactions,
                at=now,
            )
            update: dict[str, Any] = {
                "views": stmt.excluded.views,
                "forwards": stmt.excluded.forwards,
                "at": now,
            }
            if reactions is not None:
                update["reactions"] = stmt.excluded.reactions
            await conn.execute(
                stmt.on_conflict_do_update(constraint="uq_analytics_post_views_chat_message", set_=update)
            )


async def collect(
    engine: AsyncEngine, vault: Vault, *, options: ClientOptions | None = None, now: datetime | None = None
) -> str:
    """One run. Returns ``kill_switch``, ``not_connected``, ``no_channel``, ``ok`` or ``failed:<code>``.

    The caller holds the operator account's lock (proxyhub.mtproxy.store.LOCK_NAME).
    """
    now = now or datetime.now(UTC)
    try:
        async with engine.connect() as conn:
            if await mtp_store.kill_switch_on(conn):
                return "kill_switch"
            row = await mtp_store.load_row(conn)
            if row.state != state.CONNECTED:
                return "not_connected"
            creds = await mtp_store.load_creds(conn, vault)
            session = await mtp_store.load_session(conn, vault)
    except VaultError:
        return "failed:session_unreadable"
    if creds is None or session is None:
        return "not_connected"
    targets = await _targets(engine)
    if not targets:
        return "no_channel"
    scope = ChannelScope(
        channel_ids=frozenset(t.channel_id for t in targets),
        usernames=frozenset(t.username for t in targets),
    )
    snaps: list[Snapshot] = []
    outcome = "ok"
    try:
        async with operator_session(creds, session, options=options, channels=scope) as op:
            for t in targets:
                snap = Snapshot(role=t.role, chat_id=t.chat_id)
                snaps.append(snap)
                try:
                    await _read_one(engine, op, t, snap)
                except MtpError as exc:
                    snap.status = exc.code
                    if exc.code in ("session_terminated", "flood_wait"):
                        outcome = f"failed:{exc.code}"
                        break
    except MtpError as exc:
        outcome = f"failed:{exc.code}"
    for snap in snaps:
        await store_snapshot(engine, snap, now)
    if outcome == "ok" and any(s.status != "ok" for s in snaps):
        outcome = "partial"
    if outcome != "ok":
        log.warning("analytics: channel statistics %s", outcome)
    return outcome


async def _read_one(engine: AsyncEngine, op: Any, t: Target, snap: Snapshot) -> None:
    ref: ChannelRef = await op.resolve_channel(t.username, t.channel_id)
    full = await op.channel_full(ref)
    count = getattr(full, "participants_count", None)
    snap.participants = int(count) if isinstance(count, int) else None
    stats_dc = getattr(full, "stats_dc", None)
    if getattr(full, "can_view_stats", None) is not True:
        snap.status = "stats_unavailable"
    elif isinstance(stats_dc, int) and stats_dc != op.home_dc:
        snap.status = "stats_dc_other"
    else:
        stats = await op.broadcast_stats(ref)
        pending = parse_broadcast_stats(stats, snap)
        for name, token in pending[:MAX_ASYNC_GRAPHS]:
            parsed = parse_graph(await op.load_graph(token))
            if parsed is not None and "async_token" not in parsed:
                snap.graphs[name] = parsed
    ids = await _our_post_ids(engine, t.role, t.chat_id)
    if ids:
        from_stats = {p[0]: p[3] for p in snap.posts}
        views = await op.messages_views(ref, ids)
        merged = {p[0]: p for p in snap.posts}
        for msg_id, v, f in views:
            merged[msg_id] = (msg_id, v, f, from_stats.get(msg_id))
        snap.posts = list(merged.values())
