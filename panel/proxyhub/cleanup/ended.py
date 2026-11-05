"""«پاک کردن پایان‌یافته‌ها»: remove ENDED records from the database (the owner's button).

Everything happens in ONE transaction of the caller, in the order below (children before parents,
so no foreign key is left dangling and no ``ON DELETE CASCADE`` removes anything uncounted). The
caller commits it, or rolls it back for the dry-run preview: the preview runs the very same
statements, so its counts are exactly what the purge would remove at that moment.

1. **Ended Reality configs** (status other than ``active``) that ended before the cutoff, with their
   ``reality_usage`` rows. A config stays while a post younger than the edit window still carries it
   as active: its provider must still report the end so that post gets its mark.
2. **Deleted nodes** (state ``deleted`` before the cutoff) with every row that hangs on them:
   events, agent records, Telemt config and users, the MTProto proxy, probe samples, operations,
   Xray status and the node's own Reality key. A deleted node stays while any of this holds:

   * it counts toward this month's bill (rule 8): Hetzner had a server for it and it was deleted
     this month (or has no deletion time), or it was created within the fleet's creation window.
     The budget cap and the hourly creation cap read exactly these nodes
     (``proxyhub.fleet.snapshot.load_nodes``); removing one would let the fleet spend past the cap;
   * a ``cost_ledger`` row points at it (the ledger is never touched);
   * a ``cf_records`` row points at it: a DNS record the Cloudflare loop has not removed yet (that
     row is how the panel knows the record is its own, so it is never removed here);
   * a Reality config made for it is still there (a slot's config lives on with the slot's next
     server), or one of its usage rows counts toward a config that stays;
   * it has a location measurement from Iran inside the fleet's scoring window
     (``proxyhub.fleet.locations.WINDOW``): the fleet still uses it to choose locations;
   * a post younger than the edit window carries its MTProto link as active.

   ``ip_history`` rows (the memory of burned IPs) stay; the schema only clears their node link.
3. **Retired sponsor registrations** (``mtproxy_registrations.state = 'retired'`` before the
   cutoff) with their ``sponsor_flows`` and ``mtproxy_stats``, once no Telemt user and no MTProto
   proxy of that user name exists any more (so the proxy can never come back for its tag).
4. **Finished TEST-channel post rows**: ``posts`` of role ``test`` older than the edit window, than
   24 hours (the longest gap setting) and than the cutoff, planned for a day before yesterday
   (Tehran), whose send and last edit are done, with their ``post_links``; and ``channel_posts`` of
   role ``test`` that belong to such a post (or to no post at all), with no edit waiting. A row in
   the main channel's chat is never taken, whatever its role says.
5. **Finished outbox jobs** (``done_at`` before the cutoff) that no post, channel post or analytics
   alert refers to (the publisher finds a post's message and its last edit through them).

Only the tables in :data:`ALLOWED_TABLES` are ever deleted from (each DELETE checks it), and
:data:`REFERENCES` says how every foreign key into them is handled (a test compares it with the
database's own catalog). Nothing comes back but counts: no secret, link, address or text.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.fleet.actions import iso
from proxyhub.fleet.locations import WINDOW as LOCATION_WINDOW
from proxyhub.fleet.planner import CREATION_WINDOW
from proxyhub.posts import settings as posting
from proxyhub.safety.budget import month_bounds
from proxyhub.telegram import store as tg_store
from proxyhub.telegram.schedule import MAX_POST_INTERVAL_MIN, TEHRAN

Db = AsyncConnection | AsyncSession

MIN_AGE_HOURS: Final = 1
MAX_AGE_HOURS: Final = 720
DEFAULT_MIN_AGE_HOURS: Final = 1

# The ONLY tables this module deletes from, in the order the result lists them.
ALLOWED_TABLES: Final = (
    "reality_usage",
    "reality_configs",
    "probe_network_results",
    "probe_measurements",
    "probe_rounds",
    "probe_node_state",
    "probe_location_reach",
    "agent_node_reports",
    "agent_nodes",
    "agent_certificates",
    "agent_registration_tokens",
    "telemt_users",
    "telemt_node_configs",
    "mtproto_proxies",
    "node_operations",
    "reality_node_status",
    "reality_keys",
    "node_events",
    "nodes",
    "mtproxy_stats",
    "sponsor_flows",
    "mtproxy_registrations",
    "post_links",
    "posts",
    "channel_posts",
    "outbox",
)
# Never deleted from here, whatever happens (the owner's ledger and log, settings, the vault,
# accounts, logins, the burned-IP memory and the DNS records the panel owns).
NEVER_TABLES: Final = frozenset(
    {
        "cost_ledger",
        "actions_log",
        "settings",
        "secrets",
        "hetzner_accounts",
        "admin_users",
        "admin_sessions",
        "admin_recovery_codes",
        "ip_history",
        "cf_records",
        "cf_zones",
        "content_items",
    }
)

DELETED_FIRST: Final = "deleted first, counted"
GUARD: Final = "guard: the parent row stays while this row exists"
SET_NULL: Final = "kept: the schema clears the link (ON DELETE SET NULL)"
# Every foreign key INTO a table of ALLOWED_TABLES ("child.column -> parent") and how it is handled.
REFERENCES: Final[dict[str, str]] = {
    "reality_usage.config_id -> reality_configs": DELETED_FIRST,
    "reality_usage.node_id -> nodes": GUARD,
    "reality_configs.node_id -> nodes": GUARD,
    "reality_configs.key_id -> reality_keys": GUARD,
    "probe_network_results.measurement_id -> probe_measurements": DELETED_FIRST,
    "probe_measurements.node_id -> nodes": DELETED_FIRST,
    "probe_rounds.node_id -> nodes": DELETED_FIRST,
    "probe_node_state.node_id -> nodes": DELETED_FIRST,
    "probe_location_reach.node_id -> nodes": DELETED_FIRST,
    "agent_node_reports.node_id -> nodes": DELETED_FIRST,
    "agent_nodes.node_id -> nodes": DELETED_FIRST,
    "agent_certificates.node_id -> nodes": DELETED_FIRST,
    "agent_registration_tokens.node_id -> nodes": DELETED_FIRST,
    "telemt_users.node_id -> nodes": DELETED_FIRST,
    "telemt_node_configs.node_id -> nodes": DELETED_FIRST,
    "mtproto_proxies.node_id -> nodes": DELETED_FIRST,
    "node_operations.node_id -> nodes": DELETED_FIRST,
    "reality_node_status.node_id -> nodes": DELETED_FIRST,
    "reality_keys.node_id -> nodes": DELETED_FIRST,
    "node_events.node_id -> nodes": DELETED_FIRST,
    "cost_ledger.node_id -> nodes": GUARD,
    "cf_records.node_id -> nodes": GUARD,
    "ip_history.node_id -> nodes": SET_NULL,
    "telemt_users.registration_id -> mtproxy_registrations": GUARD,
    "mtproxy_stats.registration_id -> mtproxy_registrations": DELETED_FIRST,
    "sponsor_flows.registration_id -> mtproxy_registrations": DELETED_FIRST,
    "post_links.post_id -> posts": DELETED_FIRST,
    "posts.outbox_id -> outbox": GUARD,
    "posts.last_edit_outbox_id -> outbox": GUARD,
    "channel_posts.outbox_id -> outbox": GUARD,
}


def _sql(*parts: str) -> str:
    """One statement from this module's own constant fragments (never from input)."""
    return " ".join(parts)


# A post younger than the edit window that still carries a link as active (its provider must
# still report the link's end). The last part ties ``pl.key`` to the row being checked.
_CARRIED = (
    "EXISTS (SELECT 1 FROM post_links pl JOIN posts p ON p.id = pl.post_id "
    "WHERE pl.status = 'active' AND p.created_at >= :window_start AND"
)
_CARRIES_CONFIG = _sql(_CARRIED, "pl.key IN ('reality:' || rc.id, 'reality:' || rc.id || ':' || 'xhttp'))")
_CARRIES_NODE = _sql(_CARRIED, "pl.key LIKE 'mtproto:' || n.id::text || ':%')")

_ENDED_CONFIGS = _sql(
    "SELECT rc.id FROM reality_configs rc",
    "WHERE rc.status <> 'active' AND rc.ended_at < :cutoff AND NOT",
    _CARRIES_CONFIG,
    "ORDER BY rc.id FOR UPDATE OF rc",
)
_NODE_ENDED = "COALESCE(n.deleted_at, n.updated_at) < :cutoff"
_NODE_IN_LEDGER = "EXISTS (SELECT 1 FROM cost_ledger c WHERE c.node_id = n.id)"
_NODE_HAS_DNS = "EXISTS (SELECT 1 FROM cf_records r WHERE r.node_id = n.id)"
_NODE_REALITY = (
    "(EXISTS (SELECT 1 FROM reality_configs rc WHERE rc.node_id = n.id) "
    "OR EXISTS (SELECT 1 FROM reality_configs rc JOIN reality_keys k ON k.id = rc.key_id "
    "WHERE k.node_id = n.id) "
    "OR EXISTS (SELECT 1 FROM reality_usage u WHERE u.node_id = n.id))"
)
_NODE_REACH = "EXISTS (SELECT 1 FROM probe_location_reach l WHERE l.node_id = n.id AND l.at >= :reach_since)"
# The nodes the budget and the creation caps count (proxyhub/fleet/snapshot.py load_nodes and
# proxyhub/fleet/costs.py node_cost_items): billed this month, or created within the window.
_NODE_BILLED = (
    "((n.hetzner_id IS NOT NULL AND (n.deleted_at IS NULL OR n.deleted_at >= :month_start)) "
    "OR n.created_at >= :creation_since)"
)
_DELETED_NODES = _sql(
    "SELECT n.id FROM nodes n WHERE n.state = 'deleted' AND",
    _NODE_ENDED,
    "AND NOT",
    _NODE_BILLED,
    "AND NOT",
    _NODE_IN_LEDGER,
    "AND NOT",
    _NODE_HAS_DNS,
    "AND NOT",
    _NODE_REALITY,
    "AND NOT",
    _NODE_REACH,
    "AND NOT",
    _CARRIES_NODE,
    "ORDER BY n.id FOR UPDATE OF n",
)
_NODES_KEPT = _sql(
    "SELECT CASE WHEN NOT (",
    _NODE_ENDED,
    ") THEN 'too_recent' WHEN",
    _NODE_BILLED,
    "THEN 'billed_this_month' WHEN",
    _NODE_IN_LEDGER,
    "THEN 'cost_ledger' WHEN",
    _NODE_HAS_DNS,
    "THEN 'dns_record' WHEN",
    _NODE_REALITY,
    "THEN 'reality_in_use' WHEN",
    _NODE_REACH,
    "THEN 'location_reach' WHEN",
    _CARRIES_NODE,
    "THEN 'in_recent_post' ELSE 'other' END AS reason, count(*) AS n",
    "FROM nodes n WHERE n.state = 'deleted' GROUP BY 1",
)
_CONFIGS_KEPT = _sql(
    "SELECT CASE WHEN rc.ended_at IS NULL OR rc.ended_at >= :cutoff THEN 'too_recent' WHEN",
    _CARRIES_CONFIG,
    "THEN 'in_recent_post' ELSE 'other' END AS reason, count(*) AS n",
    "FROM reality_configs rc WHERE rc.status <> 'active' GROUP BY 1",
)
_REG_PROXY_EXISTS = (
    "(EXISTS (SELECT 1 FROM telemt_users u WHERE u.registration_id = r.id OR u.name = r.secret_name) "
    "OR EXISTS (SELECT 1 FROM mtproto_proxies m WHERE m.user_name = r.secret_name))"
)
_RETIRED_REGS = _sql(
    "SELECT r.id FROM mtproxy_registrations r WHERE r.state = 'retired' AND r.updated_at < :cutoff",
    "AND NOT",
    _REG_PROXY_EXISTS,
    "ORDER BY r.id FOR UPDATE OF r",
)
_REGS_KEPT = _sql(
    "SELECT CASE WHEN r.updated_at >= :cutoff THEN 'too_recent' WHEN",
    _REG_PROXY_EXISTS,
    "THEN 'proxy_exists' ELSE 'other' END AS reason, count(*) AS n",
    "FROM mtproxy_registrations r WHERE r.state = 'retired' GROUP BY 1",
)
_TEST_POSTS = (
    "SELECT p.id FROM posts p "
    "LEFT JOIN outbox o ON o.id = p.outbox_id LEFT JOIN outbox e ON e.id = p.last_edit_outbox_id "
    "WHERE p.role = 'test' AND p.created_at < :post_cutoff AND p.plan_day < :keep_from_day "
    "AND (p.outbox_id IS NULL OR o.done_at IS NOT NULL) "
    "AND (p.last_edit_outbox_id IS NULL OR e.id IS NULL OR e.done_at IS NOT NULL) "
    "AND NOT EXISTS (SELECT 1 FROM channel_posts cp WHERE cp.outbox_id = p.outbox_id "
    "AND (cp.role <> 'test' OR cp.chat_id IS NOT DISTINCT FROM :main_chat)) "
    "ORDER BY p.id FOR UPDATE OF p"
)
_EDIT_WAITING = (
    "EXISTS (SELECT 1 FROM outbox j WHERE j.done_at IS NULL "
    "AND j.kind IN ('edit_post', 'operator_edit_post') AND j.payload->>'post_id' = cp.id::text)"
)
_TEST_CHANNEL_POSTS = _sql(
    "SELECT cp.id FROM channel_posts cp",
    "WHERE cp.role = 'test' AND cp.chat_id IS DISTINCT FROM :main_chat",
    "AND cp.created_at < :post_cutoff AND (cp.last_edited_at IS NULL OR cp.last_edited_at < :cutoff)",
    "AND (cp.outbox_id IS NULL",
    "OR NOT EXISTS (SELECT 1 FROM posts p WHERE p.outbox_id = cp.outbox_id)",
    "OR EXISTS (SELECT 1 FROM posts p WHERE p.outbox_id = cp.outbox_id AND p.id = ANY(:post_ids)))",
    "AND NOT",
    _EDIT_WAITING,
    "ORDER BY cp.id FOR UPDATE OF cp",
)
_FINISHED_OUTBOX = (
    "outbox.done_at IS NOT NULL AND outbox.done_at < :cutoff "
    "AND NOT EXISTS (SELECT 1 FROM posts p "
    "WHERE p.outbox_id = outbox.id OR p.last_edit_outbox_id = outbox.id) "
    "AND NOT EXISTS (SELECT 1 FROM channel_posts cp WHERE cp.outbox_id = outbox.id) "
    "AND NOT EXISTS (SELECT 1 FROM analytics_alerts a WHERE a.outbox_id = outbox.id)"
)
# Children of a deleted node, in order (probe_network_results goes first, through its measurement).
_NODE_CHILDREN: Final = (
    (
        "probe_network_results",
        "measurement_id IN (SELECT id FROM probe_measurements WHERE node_id = ANY(:ids))",
    ),
    ("probe_measurements", "node_id = ANY(:ids)"),
    ("probe_rounds", "node_id = ANY(:ids)"),
    ("probe_node_state", "node_id = ANY(:ids)"),
    ("probe_location_reach", "node_id = ANY(:ids)"),
    ("agent_node_reports", "node_id = ANY(:ids)"),
    ("agent_nodes", "node_id = ANY(:ids)"),
    ("agent_certificates", "node_id = ANY(:ids)"),
    ("agent_registration_tokens", "node_id = ANY(:ids)"),
    ("telemt_users", "node_id = ANY(:ids)"),
    ("telemt_node_configs", "node_id = ANY(:ids)"),
    ("mtproto_proxies", "node_id = ANY(:ids)"),
    ("node_operations", "node_id = ANY(:ids)"),
    ("reality_node_status", "node_id = ANY(:ids)"),
    (
        "reality_keys",
        "node_id = ANY(:ids) "
        "AND NOT EXISTS (SELECT 1 FROM reality_configs rc WHERE rc.key_id = reality_keys.id)",
    ),
    ("node_events", "node_id = ANY(:ids)"),
)


@dataclass
class PurgeResult:
    dry_run: bool
    min_age_hours: int
    cutoff: datetime
    removed: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ALLOWED_TABLES, 0))
    # Ended things left in place, by kind and reason (too_recent, location_reach, ...).
    kept: dict[str, dict[str, int]] = field(default_factory=dict)
    ip_history_unlinked: int = 0

    @property
    def total(self) -> int:
        return sum(self.removed.values())

    def as_json(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "min_age_hours": self.min_age_hours,
            "cutoff": iso(self.cutoff),
            "removed": dict(self.removed),
            "total": self.total,
            "kept": {k: dict(v) for k, v in self.kept.items()},
            "ip_history_unlinked": self.ip_history_unlinked,
        }

    def log_context(self) -> dict[str, Any]:
        """For actions_log: the counts that are not zero (no ids, no names)."""
        return {
            "min_age_hours": self.min_age_hours,
            "removed": {k: v for k, v in self.removed.items() if v},
            "total": self.total,
            "kept": {k: dict(v) for k, v in self.kept.items() if v},
            "ip_history_unlinked": self.ip_history_unlinked,
        }


class _Deleter:
    def __init__(self, db: Db, result: PurgeResult) -> None:
        self.db = db
        self.result = result

    async def delete(self, table: str, where: str, params: dict[str, Any]) -> int:
        if table not in ALLOWED_TABLES:
            raise ValueError("cleanup may not touch this table: " + table)
        # The table name comes from the allowlist above and the condition from this module's
        # constants; every value is a bound parameter.
        res = await self.db.execute(text(_sql("DELETE FROM", table, "WHERE", where)), params)
        count = int(getattr(res, "rowcount", 0) or 0)
        self.result.removed[table] += count
        return count


async def _ids(db: Db, sql: str, params: dict[str, Any]) -> list[Any]:
    return list((await db.execute(text(sql), params)).scalars())


async def _reasons(db: Db, sql: str, params: dict[str, Any]) -> dict[str, int]:
    return {str(r.reason): int(r.n) for r in (await db.execute(text(sql), params)).all()}


async def _main_chat_id(db: Db) -> int | None:
    main = (await tg_store.channels(db)).get("main") or {}
    chat_id = main.get("chat_id")
    return chat_id if isinstance(chat_id, int) and not isinstance(chat_id, bool) else None


async def purge_ended(db: Db, *, now: datetime, min_age_hours: int, dry_run: bool) -> PurgeResult:
    """Remove the ended rows (module doc) in the caller's transaction and count them. The caller
    commits, or rolls back when ``dry_run`` (nothing here commits or rolls back)."""
    if isinstance(min_age_hours, bool) or not MIN_AGE_HOURS <= min_age_hours <= MAX_AGE_HOURS:
        raise ValueError("min_age_hours out of range")
    # Short waits: a worker holding one of these rows wins, and the owner is asked to try again.
    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    await db.execute(text("SET LOCAL statement_timeout = '60s'"))
    cutoff = now - timedelta(hours=min_age_hours)
    result = PurgeResult(dry_run=dry_run, min_age_hours=min_age_hours, cutoff=cutoff)
    edit_window = timedelta(hours=(await posting.load(db)).edit_window_hours)
    params: dict[str, Any] = {
        "cutoff": cutoff,
        "window_start": now - edit_window,
        "reach_since": now - LOCATION_WINDOW,
        "month_start": month_bounds(now)[0],
        "creation_since": now - CREATION_WINDOW,
        "post_cutoff": min(cutoff, now - edit_window, now - timedelta(minutes=MAX_POST_INTERVAL_MIN)),
        "keep_from_day": now.astimezone(TEHRAN).date() - timedelta(days=1),
        "main_chat": await _main_chat_id(db),
    }
    d = _Deleter(db, result)
    await _configs(db, d, params)
    await _nodes(db, d, params, result)
    await _registrations(db, d, params)
    await _test_posts(db, d, params)
    await d.delete("outbox", _FINISHED_OUTBOX, params)
    result.kept = {
        "nodes": await _reasons(db, _NODES_KEPT, params),
        "reality_configs": await _reasons(db, _CONFIGS_KEPT, params),
        "registrations": await _reasons(db, _REGS_KEPT, params),
    }
    return result


async def _configs(db: Db, d: _Deleter, params: dict[str, Any]) -> None:
    ids = await _ids(db, _ENDED_CONFIGS, params)
    if ids:
        await d.delete("reality_usage", "config_id = ANY(:ids)", {"ids": ids})
        await d.delete("reality_configs", "id = ANY(:ids) AND status <> 'active'", {"ids": ids})


async def _nodes(db: Db, d: _Deleter, params: dict[str, Any], result: PurgeResult) -> None:
    ids: Sequence[uuid.UUID] = await _ids(db, _DELETED_NODES, params)
    if not ids:
        return
    result.ip_history_unlinked = int(
        (
            await db.execute(text("SELECT count(*) FROM ip_history WHERE node_id = ANY(:ids)"), {"ids": ids})
        ).scalar_one()
    )
    for table, where in _NODE_CHILDREN:
        await d.delete(table, where, {"ids": ids})
    await d.delete("nodes", "id = ANY(:ids) AND state = 'deleted'", {"ids": ids})


async def _registrations(db: Db, d: _Deleter, params: dict[str, Any]) -> None:
    ids = await _ids(db, _RETIRED_REGS, params)
    if not ids:
        return
    await db.execute(
        text("SELECT id FROM sponsor_flows WHERE registration_id = ANY(:ids) FOR UPDATE"), {"ids": ids}
    )
    await d.delete("mtproxy_stats", "registration_id = ANY(:ids)", {"ids": ids})
    await d.delete("sponsor_flows", "registration_id = ANY(:ids)", {"ids": ids})
    await d.delete("mtproxy_registrations", "id = ANY(:ids) AND state = 'retired'", {"ids": ids})


async def _test_posts(db: Db, d: _Deleter, params: dict[str, Any]) -> None:
    post_ids = await _ids(db, _TEST_POSTS, params)
    channel_post_ids = await _ids(db, _TEST_CHANNEL_POSTS, {**params, "post_ids": post_ids})
    if post_ids:
        await d.delete("post_links", "post_id = ANY(:ids)", {"ids": post_ids})
        await d.delete("posts", "id = ANY(:ids) AND role = 'test'", {"ids": post_ids})
    if channel_post_ids:
        await d.delete(
            "channel_posts",
            "id = ANY(:ids) AND role = 'test' AND chat_id IS DISTINCT FROM :main_chat",
            {"ids": channel_post_ids, "main_chat": params["main_chat"]},
        )
