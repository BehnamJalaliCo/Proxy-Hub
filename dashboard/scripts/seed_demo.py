#!/usr/bin/env python3
"""Demo data for the dashboard's screenshots: fills the THROWAWAY database ``ph_shots`` so every
page shows its real, busy state (nodes, proxies, posts, the review queue, statistics, events).

    cd panel && . <env.sh> && .venv/bin/python ../dashboard/scripts/seed_demo.py
    cd panel && . <env.sh> && .venv/bin/python ../dashboard/scripts/seed_demo.py --beat   # see below
    cd panel && . <env.sh> && .venv/bin/python ../dashboard/scripts/seed_demo.py --wipe   # demo data out

HARD GUARD: the script refuses to run (exit 2) unless the database NAME in PROXYHUB_DATABASE_URL is
exactly ``ph_shots``, and checks again on the open connection (``SELECT current_database()``). It
can never seed a real panel.

Nothing here is real: servers have documentation addresses (RFC 5737), domains end in ``.example``
(RFC 2606), the news texts and their sources are invented, tokens are placeholder words that are
not even token shaped (so no code can send them anywhere). No worker must run against this database:
the rows describe servers and Telegram chats that do not exist.

Idempotent: every run first removes what an earlier run made (it owns every non-auth table of this
database) and writes the same data set again, with times relative to now. The owner account, the
sessions and the other auth tables are never touched; neither are the events and content items the
panel itself wrote.

``--beat`` only keeps the worker heartbeat (and the "last seen" times a live panel refreshes every
minute) fresh until it is stopped: run it in the background while the screenshots are taken, so the
overview does not show a dead worker halfway through.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlsplit

EXPECTED_DATABASE = "ph_shots"


def _database_name() -> str | None:
    """The database name of PROXYHUB_DATABASE_URL (never printed: the URL holds a password)."""
    raw = os.environ.get("PROXYHUB_DATABASE_URL", "")
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if not parts.scheme.startswith("postgresql"):
        return None
    return parts.path[1:] if parts.path.startswith("/") else None


if _database_name() != EXPECTED_DATABASE:
    print(
        f"seed_demo: refused. PROXYHUB_DATABASE_URL must name the throwaway database "
        f"'{EXPECTED_DATABASE}' and nothing else; nothing was read or changed.",
        file=sys.stderr,
    )
    sys.exit(2)

# Only now is anything of the panel imported (its config reads the environment).
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import random  # noqa: E402
import struct  # noqa: E402
import uuid  # noqa: E402
import zlib  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any  # noqa: E402

from sqlalchemy import insert, select, text, update  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession  # noqa: E402

from proxyhub.agent_api.desired import create_telemt_user, set_telemt_node_config  # noqa: E402
from proxyhub.config import get_config  # noqa: E402
from proxyhub.content import qc_settings  # noqa: E402
from proxyhub.db import (  # noqa: E402,F401 - every model module, so the metadata is complete
    models_acme,
    models_agent,
    models_analytics,
    models_auth,
    models_content,
    models_fleet,
    models_live,
    models_mtproto,
    models_net,
    models_posts,
    models_reality,
    models_sponsor,
    models_telegram,
)
from proxyhub.db.models import (  # noqa: E402
    ActionLog,
    Base,
    HetznerAccount,
    MtproxyRegistration,
    Node,
    NodeEvent,
    Outbox,
    SponsorCampaign,
)
from proxyhub.db.models_acme import AcmeCertificate  # noqa: E402
from proxyhub.db.models_agent import AgentNode, AgentNodeReport, TelemtUser  # noqa: E402
from proxyhub.db.models_analytics import (  # noqa: E402
    ChannelSample,
    ChannelStatsSnapshot,
    MemberEvent,
    NodeSample,
    PostViews,
    TrackingLink,
    UsageDay,
    UsageDim,
    UsageHour,
)
from proxyhub.db.models_content import (  # noqa: E402
    ContentCandidate,
    ContentMedia,
    ContentQcCall,
    ContentSource,
)
from proxyhub.db.models_fleet import (  # noqa: E402
    AccountObservation,
    AccountServer,
    FleetAlert,
    FleetPlan,
    HetznerOffer,
    IpHistory,
)
from proxyhub.db.models_live import NodeOperation  # noqa: E402
from proxyhub.db.models_mtproto import MtprotoProxy  # noqa: E402
from proxyhub.db.models_net import (  # noqa: E402
    CfRecord,
    CfState,
    CfZone,
    DomainCheck,
    ProbeCycle,
    ProbeLocationReach,
    ProbeMeasurement,
    ProbeNetworkResult,
    ProbeNodeState,
    ProbeRound,
    ProbeSource,
    TargetState,
)
from proxyhub.db.models_posts import ContentItem, Post, PostLink  # noqa: E402
from proxyhub.db.models_reality import (  # noqa: E402
    RealityConfig,
    RealityKey,
    RealityTrafficHour,
    RealityUsage,
)
from proxyhub.db.models_sponsor import MtproxybotEntry, SponsorFlow  # noqa: E402
from proxyhub.db.models_telegram import ChannelPost  # noqa: E402
from proxyhub.db.session import get_engine  # noqa: E402
from proxyhub.fleet.actions import iso  # noqa: E402
from proxyhub.fleet.live import FLEET_NODE_PREFIX  # noqa: E402
from proxyhub.fleet.settings_store import save_fleet_settings  # noqa: E402
from proxyhub.mtproto.keys import load_proxy_seal_key  # noqa: E402
from proxyhub.mtproto.urls import extra_user_name, label_for, proxy_key, user_name_for  # noqa: E402
from proxyhub.mtproxy import state as account_state  # noqa: E402
from proxyhub.mtproxy import store as account_store  # noqa: E402
from proxyhub.mtproxy.validation import mask_phone  # noqa: E402
from proxyhub.posts import emoji_art, operator, own_pack, plan  # noqa: E402
from proxyhub.posts import settings as posting_settings  # noqa: E402
from proxyhub.reality.keys import new_key_pair, new_short_id, new_ws_path  # noqa: E402
from proxyhub.reality.links import config_label  # noqa: E402
from proxyhub.reality.settings import PORT as REALITY_PORT  # noqa: E402
from proxyhub.reality.settings import XHTTP_PORT, RealitySettings, save_reality_settings  # noqa: E402
from proxyhub.reality.store import XrayUserTotal, config_email, store_xray_report  # noqa: E402
from proxyhub.settings_schema import FleetSettings  # noqa: E402
from proxyhub.sponsor import inventory, pacing, replies  # noqa: E402
from proxyhub.sponsor import store as sponsor_store  # noqa: E402
from proxyhub.targets import settings as target_settings  # noqa: E402
from proxyhub.telegram import store as tg_store  # noqa: E402
from proxyhub.telegram.schedule import TEHRAN, QuietWindow  # noqa: E402
from proxyhub.vault import SecretValue, Vault, hcloud_secret_name  # noqa: E402

# --- what a run owns ---------------------------------------------------------------------------------

# Never touched: the owner's account and sessions, the migration marker, and the tables that also
# hold rows the panel wrote itself (those are cleaned row by row below).
KEEP_TABLES = frozenset(
    {
        "admin_users",
        "admin_setup_tokens",
        "admin_enrollments",
        "admin_sessions",
        "admin_recovery_codes",
        "auth_throttle",
        "alembic_version",
        "settings",
        "secrets",
        "actions_log",
        "content_items",
    }
)
SETTING_KEYS = (
    "fleet",
    "worker.heartbeat",
    "telegram.bot",
    "telegram.owner",
    "telegram.channels",
    "telegram.posting",
    "telegram.updates",
    "telegram.health",
    "telegram.last_alert",
    "posting",
    "posting.state",
    "posts.operator",
    "posts.operator.pack",
    "posts.operator.rights",
    "posts.operator.hold",
    "posts.own_pack",
    "mtproxy.account",
    "sponsor.settings",
    "sponsor.pacing",
    "sponsor.mtproxybot",
    "analytics.status",
    "content.qc",
    "content.qc.state",
    "reality",
    "reality.state",
    "targets",
    "targets.state",
)
SECRET_WHERE = (
    "name LIKE 'hcloud:%' OR name LIKE 'reality:key:%' "
    "OR name IN ('telegram:bot', 'cloudflare:token', 'mtproxy:session')"
)
# Not token shaped on purpose: the panel's own clients refuse to send such a value anywhere.
PLACEHOLDER_SECRET = "demo-placeholder-not-a-token"  # noqa: S105

NS = uuid.UUID("6b1f0c1e-8f3a-4f0e-9d53-70687368746f")
RND = random.Random(20261112)  # noqa: S311 - demo numbers, nothing secret
GB = 1000**3
TB = 1000**4
TELEMT_VERSION = "3.5.16"
XRAY_VERSION = "26.10.31"
MT_PORT = 8443

NOW = datetime.now(UTC).replace(microsecond=0)


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def uid(name: str) -> uuid.UUID:
    return uuid.uuid5(NS, name)


def hex32(name: str) -> str:
    return hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()


def hex64(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


def fa(n: int | str) -> str:
    return str(n).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


# --- the world -----------------------------------------------------------------------------------------

ZONE_ACTIVE = "aftab-cdn.example"
ZONES: list[dict[str, Any]] = [
    {"name": ZONE_ACTIVE, "pool": "active", "status": "active", "seen_d": 34, "changed_d": 9.9},
    {"name": "sepehr-link.example", "pool": "standby", "status": "active", "seen_d": 20, "changed_d": 20},
    {"name": "mahtab-net.example", "pool": "standby", "status": "active", "seen_d": 6, "changed_d": 6},
    {"name": "darya-edge.example", "pool": "none", "status": "pending", "seen_d": 1, "changed_d": None},
    {"name": "kavir-proxy.example", "pool": "filtered", "status": "active", "seen_d": 48, "changed_d": 0.4},
    {"name": "alborz-gate.example", "pool": "retired", "status": "active", "seen_d": 60, "changed_d": 30},
    {"name": "zagros-cdn.example", "pool": "deleted", "status": "gone", "seen_d": 9, "changed_d": 8.9},
]

ACCOUNTS: list[dict[str, Any]] = [
    {"key": "a", "name": "Hetzner-Nodes-07", "state": "active", "reason": "verified", "age_d": 34, "limit": 5},
    {"key": "b", "name": "Hetzner-Nodes-08", "state": "active", "reason": "verified", "age_d": 12, "limit": 4},
    {"key": "c", "name": "Hetzner-Nodes-06", "state": "retired", "reason": "retired_by_owner", "age_d": 71},
    {"key": "d", "name": "Hetzner-Nodes-05", "state": "locked", "reason": "token_rejected", "age_d": 96},
]

# Iranian measuring networks (operators, never people); the first and the third decide (P15).
NETS: list[tuple[int, str]] = [
    (197207, "Mobile Communication Company of Iran PLC"),
    (44244, "Iran Cell Service and Communication Company"),
    (57218, "Rightel Communication Service Company PJS"),
    (58224, "Iran Telecommunication Company PJS"),
    (31549, "Aria Shatel PJSC"),
]

# One row per server. reach: per network of NETS, "o" open, "c" closed, "-" not measured.
NODES: list[dict[str, Any]] = [
    {"key": "s1", "slot": 1, "acct": "a", "loc": "hel1", "type": "cpx02", "state": "ready", "age_d": 21,
     "ip": "203.0.113.24", "agent": "0.5.0", "verdict": "ok", "reach": "ooooo", "conns": 61, "gb_day": 58},
    {"key": "s2", "slot": 2, "acct": "a", "loc": "hel1", "type": "cpx02", "state": "ready", "age_d": 21,
     "ip": "203.0.113.87", "agent": "0.5.0", "verdict": "ok", "reach": "oooo-", "conns": 84, "gb_day": 71,
     "hot": True},
    {"key": "s3", "slot": 3, "acct": "a", "loc": "ash", "type": "cpx11", "state": "ready", "age_d": 5,
     "ip": "198.51.100.42", "agent": "0.5.0", "verdict": "ok", "reach": "ooo-o", "conns": 37, "gb_day": 24},
    {"key": "s4", "slot": 4, "acct": "b", "loc": "hil", "type": "cpx11", "state": "ready", "age_d": 11,
     "ip": "192.0.2.118", "agent": "0.4.0", "verdict": "ok", "reach": "oooo-", "conns": 29, "gb_day": 21},
    {"key": "s5", "slot": 5, "acct": "b", "loc": "sin", "type": "cpx11", "state": "ready", "age_d": 11,
     "ip": "192.0.2.201", "agent": "0.5.0", "verdict": "suspect", "reach": "oocoo", "conns": 48, "gb_day": 36},
    {"key": "s6", "slot": 6, "acct": "b", "loc": "ash", "type": "cpx11", "state": "ready", "age_d": 6,
     "ip": "198.51.100.173", "agent": "0.4.0", "verdict": "ok", "reach": "coo-o", "conns": 22, "gb_day": 19},
    {"key": "s7", "slot": 7, "acct": "a", "loc": "hel1", "type": "cpx02", "state": "unhealthy", "age_d": 16,
     "ip": "203.0.113.140", "agent": "0.5.0", "verdict": "ok", "reach": "ooooo", "conns": 0, "gb_day": 44},
    {"key": "s8", "slot": 8, "acct": "b", "loc": "hil", "type": "cpx11", "state": "burned", "age_d": 11,
     "ip": "192.0.2.66", "agent": "0.4.0", "verdict": "blocked", "reach": "ccccc", "conns": 3, "gb_day": 17},
    {"key": "s8n", "slot": 8, "acct": "a", "loc": "hel1", "type": "cpx02", "state": "booting", "age_min": 4,
     "ip": "203.0.113.203"},
    # history: gone servers of the last week
    {"key": "h3", "slot": 3, "acct": "a", "loc": "fsn1", "type": "cpx02", "state": "deleted", "age_d": 24,
     "ip": "198.51.100.77", "agent": "0.4.0", "verdict": "blocked", "reach": "cocoo", "gone_d": 4.9,
     "why": "location_removed", "gb_day": 30},
    {"key": "h6", "slot": 6, "acct": "b", "loc": "hil", "type": "cpx11", "state": "deleted", "age_d": 11.5,
     "ip": "192.0.2.9", "agent": "0.4.0", "verdict": "blocked", "reach": "cccc-", "gone_d": 5.9,
     "why": "burned", "gb_day": 15},
    {"key": "t1", "slot": None, "acct": "a", "loc": "hel1", "type": "cpx02", "state": "deleted", "age_d": 3.2,
     "ip": "203.0.113.9", "agent": "0.5.0", "gone_d": 3.0, "why": "owner", "test": True, "gb_day": 1},
]
INCLUDED = {"hel1": 20 * TB, "fsn1": 20 * TB, "nbg1": 20 * TB, "ash": 1 * TB, "hil": 1 * TB, "sin": TB // 2}
PRICES = {  # (hourly, monthly) net EUR; invented but in the real range
    "cpx02": {"hel1": ("0.0104", "6.49"), "fsn1": ("0.0104", "6.49"), "nbg1": ("0.0104", "6.49")},
    "cpx11": {"ash": ("0.0088", "5.49"), "hil": ("0.0088", "5.49"), "sin": ("0.0112", "6.99")},
}
SERVING = ("ready", "unhealthy", "burned")

MAIN_CHAT = -1002233445566
TEST_CHAT = -1002233445577
MAIN_USERNAME = "@ipoxxo"
TEST_USERNAME = "@ipoxxo_lab"
OWNER_CHAT = 5550001234
BOT_ID = 7412365890
BOT_USERNAME = "ipoxxo_panel_bot"


def node_id(key: str) -> uuid.UUID:
    return uid(f"node:{key}")


def node_name(n: dict[str, Any]) -> str:
    nid = node_id(n["key"])
    return f"ph-test-{nid.hex[:8]}" if n.get("test") else f"{FLEET_NODE_PREFIX}{n['slot']}-{nid.hex[:8]}"


def created_at(n: dict[str, Any]) -> datetime:
    return ago(minutes=n["age_min"]) if "age_min" in n else ago(days=n["age_d"])


def ready_at(n: dict[str, Any]) -> datetime | None:
    return None if n["state"] == "booting" else created_at(n) + timedelta(minutes=4, seconds=20)


def deleted_at(n: dict[str, Any]) -> datetime | None:
    return ago(days=n["gone_d"]) if n["state"] == "deleted" else None


def slot_host(slot: int) -> str:
    return f"slot{slot}.{ZONE_ACTIVE}"


def live(n: dict[str, Any]) -> bool:
    return n["state"] != "deleted"


def node(key: str) -> dict[str, Any]:
    return next(n for n in NODES if n["key"] == key)


# --- cleaning ------------------------------------------------------------------------------------------


async def wipe(conn: AsyncConnection) -> None:
    owned = [t.name for t in Base.metadata.sorted_tables if t.name not in KEEP_TABLES]
    await conn.execute(text("TRUNCATE TABLE " + ", ".join(f'"{t}"' for t in owned) + " RESTART IDENTITY"))
    await conn.execute(text("DELETE FROM content_items WHERE origin = 'owner'"))
    await conn.execute(text("DELETE FROM actions_log WHERE reasons->'context'->>'demo' = 'true'"))
    await conn.execute(text(f"DELETE FROM secrets WHERE {SECRET_WHERE}"))  # noqa: S608 - a constant
    await conn.execute(text("DELETE FROM settings WHERE key = ANY(:keys)"), {"keys": list(SETTING_KEYS)})
    for table in ("content_items", "actions_log"):
        await conn.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "  # noqa: S608 - constants
                f"GREATEST((SELECT COALESCE(max(id), 0) FROM {table}), 1))"
            )
        )


async def put(conn: AsyncConnection, key: str, value: dict[str, Any], at: datetime | None = None) -> None:
    await tg_store.put_setting(conn, key, value)
    if at is not None:
        await conn.execute(text("UPDATE settings SET updated_at = :at WHERE key = :k"), {"at": at, "k": key})


# --- fleet: settings, accounts, nodes ------------------------------------------------------------------------


async def seed_fleet(conn: AsyncConnection, vault: Vault) -> None:
    await save_fleet_settings(
        conn,
        FleetSettings(
            kill_switch=False,
            dry_run=False,
            budget_cap_eur=Decimal("150.00"),
            node_cap_total=10,
            creations_per_hour_cap=3,
            target_slots=9,
            allowed_server_types=["cpx02", "cpx11"],
            locations=["hel1", "ash", "hil", "sin"],
        ),
    )
    await put(conn, "worker.heartbeat", {"at": iso(NOW)}, NOW)

    for a in ACCOUNTS:
        aid = uid(f"account:{a['key']}")
        made = ago(days=a["age_d"])
        await conn.execute(
            insert(HetznerAccount).values(
                id=aid,
                name=a["name"],
                token_secret_name=hcloud_secret_name(aid),
                state=a["state"],
                state_reason=a["reason"],
                learned_server_limit=a.get("limit"),
                learned_server_limit_at=ago(days=2, hours=3) if a.get("limit") else None,
                created_at=made,
                updated_at=made if a["state"] == "active" else ago(days=min(a["age_d"] - 1, 13)),
            )
        )
        if a["state"] == "active":
            await vault.put(conn, hcloud_secret_name(aid), SecretValue(PLACEHOLDER_SECRET))
            mine = [n for n in NODES if n["acct"] == a["key"] and live(n)]
            await conn.execute(
                insert(AccountObservation).values(
                    account_id=aid,
                    checked_at=ago(minutes=3),
                    ok=True,
                    guard_ok=True,
                    servers_total=len(mine),
                    managed_servers=len(mine),
                    foreign_servers=0,
                    last_ok_at=ago(minutes=3),
                )
            )
            for n in mine:
                await conn.execute(
                    insert(AccountServer).values(
                        account_id=aid,
                        hetzner_id=hetzner_id(n),
                        name=node_name(n),
                        labels={"managed-by": "proxy-hub", "role": "node", "slot": str(n["slot"])},
                        delete_protected=False,
                        seen_at=ago(minutes=3),
                    )
                )
            for stype, by_loc in PRICES.items():
                for loc, (hourly, monthly) in by_loc.items():
                    await conn.execute(
                        insert(HetznerOffer).values(
                            account_id=aid,
                            server_type=stype,
                            location=loc,
                            price_hourly_net=Decimal(hourly),
                            price_monthly_net=Decimal(monthly),
                            included_traffic_bytes=INCLUDED[loc],
                            deprecated=False,
                            available=True,
                            fetched_at=ago(minutes=38),
                        )
                    )
        elif a["state"] == "locked":
            await conn.execute(
                insert(AccountObservation).values(
                    account_id=aid,
                    checked_at=ago(days=13),
                    ok=False,
                    guard_ok=None,
                    error="token_rejected",
                    last_ok_at=ago(days=13, hours=1),
                    servers_total=0,
                )
            )

    for n in NODES:
        nid = node_id(n["key"])
        made, ready, gone = created_at(n), ready_at(n), deleted_at(n)
        burned = None
        if n["state"] == "burned":
            burned = ago(minutes=26)
        elif gone is not None and n.get("why") == "burned":
            burned = gone - timedelta(minutes=40)
        await conn.execute(
            insert(Node).values(
                id=nid,
                account_id=uid(f"account:{n['acct']}"),
                hetzner_id=hetzner_id(n),
                name=node_name(n),
                slot=n["slot"],
                location=n["loc"],
                server_type=n["type"],
                ipv4=n["ip"],
                ipv6=None,
                state=n["state"],
                is_test=bool(n.get("test")),
                created_at=made,
                updated_at=gone or burned or ready or made,
                ready_at=ready,
                burned_at=burned,
                retired_at=gone - timedelta(minutes=2) if gone else None,
                deleted_at=gone,
            )
        )
        since = {
            "ready": ready,
            "unhealthy": ago(minutes=7),
            "burned": ago(minutes=26),
            "booting": made + timedelta(seconds=50),
            "deleted": gone,
        }[n["state"]]
        await conn.execute(
            insert(NodeOperation).values(
                node_id=nid,
                requested_by="admin:owner" if n.get("test") else "fleet",
                requested_at=made,
                state_since=since,
                create_attempts=1,
                create_attempted_at=made + timedelta(seconds=20),
                server_seen_at=made + timedelta(seconds=45),
                retire_requested_at=gone - timedelta(minutes=2) if gone else None,
                retire_reason=n.get("why") if gone else None,
                delete_attempts=1 if gone else 0,
                updated_at=since,
            )
        )
        await conn.execute(insert(NodeEvent), node_events(n))
        prefix = ".".join(n["ip"].split(".")[:3]) + ".0/24"
        hit = n["state"] == "burned" or n.get("why") == "burned"
        await conn.execute(
            insert(IpHistory).values(
                ip=n["ip"],
                prefix24=prefix,
                node_id=nid,
                account_id=uid(f"account:{n['acct']}"),
                location=n["loc"],
                first_seen_at=made + timedelta(seconds=45),
                burned_at=burned if hit else None,
                burned_reason="probe_blocked" if hit else None,
            )
        )
    # an address that burned before this week (its node row is long gone)
    await conn.execute(
        insert(IpHistory).values(
            ip="198.51.100.230",
            prefix24="198.51.100.0/24",
            node_id=None,
            account_id=uid("account:c"),
            location="fsn1",
            first_seen_at=ago(days=19),
            burned_at=ago(days=17),
            burned_reason="probe_blocked",
        )
    )

    plan_actions: list[dict[str, Any]] = []
    for slot in range(1, 10):
        if slot <= 6:
            action, allowed, reasons = "none", True, ["slot_healthy"]
        elif slot == 7:
            action, allowed, reasons = "none", True, ["slot_unhealthy"]
        elif slot == 8:
            action, allowed, reasons = "none", True, ["replacement_in_progress"]
        else:
            action, allowed, reasons = "create", False, ["account_server_limit_reached"]
        plan_actions.append(
            {"action": action, "target": f"slot-{slot}", "allowed": allowed, "reasons": reasons,
             "context": {"slot": slot, "server_type": "cpx02", "location": "hel1"} if action == "create" else {"slot": slot}}
        )
    await conn.execute(
        insert(FleetPlan).values(
            generated_at=ago(seconds=41),
            dry_run=False,
            fingerprint=hex64("plan"),
            logged=True,
            actions=plan_actions,
            summary={
                "projected_month_eur": "55.01",
                "projected_after_eur": "55.01",
                "live_nodes": 9,
                "creates_allowed": 0,
                "creates_blocked": 1,
                "deletes": 0,
                "missing_prices": [],
            },
        )
    )
    await conn.execute(
        insert(FleetAlert),
        [
            {"code": "account_server_limit_reached", "active": True, "since": ago(hours=2, minutes=10),
             "last_seen_at": ago(seconds=41), "last_sent_at": ago(hours=2, minutes=5),
             "detail": {"slot": 9}},
            {"code": "burned_ip_streak", "active": True, "since": ago(minutes=24),
             "last_seen_at": ago(seconds=41), "last_sent_at": ago(minutes=24), "detail": {"count": 3}},
            {"code": "no_other_location", "active": False, "since": ago(days=6), "last_seen_at": ago(days=6),
             "last_sent_at": ago(days=6), "detail": {}},
        ],
    )


def hetzner_id(n: dict[str, Any]) -> int:
    return 61_200_000 + int(node_id(n["key"]).hex[:5], 16)


def node_events(n: dict[str, Any]) -> list[dict[str, Any]]:
    nid, made = node_id(n["key"]), created_at(n)
    out: list[dict[str, Any]] = []

    def ev(at: datetime, kind: str, **detail: Any) -> None:
        out.append({"node_id": nid, "at": at, "kind": kind, "detail": detail})

    def state(at: datetime, old: str, new: str, reason: str) -> None:
        ev(at, "state", **{"from": old, "to": new, "reason": reason})

    ev(made, "requested", slot=n["slot"], location=n["loc"], server_type=n["type"], by="admin:owner" if n.get("test") else "fleet")
    state(made + timedelta(seconds=18), "requested", "creating", "gates_passed")
    ev(made + timedelta(seconds=20), "create_started", location=n["loc"], server_type=n["type"], image="ph-node-2026-11")
    ev(made + timedelta(seconds=44), "server_created", hetzner_id=hetzner_id(n), ipv4=n["ip"])
    state(made + timedelta(seconds=45), "creating", "booting", "server_created")
    if n["state"] == "booting":
        return out
    ready = ready_at(n)
    assert ready is not None
    ev(ready - timedelta(seconds=70), "agent_registered", serial=hex32(n["key"])[:16], not_after=iso(made + timedelta(days=30)))
    state(ready - timedelta(seconds=69), "booting", "registering", "agent_registered")
    state(ready, "registering", "ready", "agent_reported")
    if n["state"] == "ready" and n.get("age_d", 0) > 15:
        ev(ago(days=3, hours=5), "agent_cert_renewed", serial=hex32(n["key"] + "r")[:16], not_after=iso(ago(days=-27)))
    if n["key"] == "s2":
        state(ago(days=8, hours=2), "ready", "unhealthy", "heartbeat_lost")
        state(ago(days=8, hours=1, minutes=52), "unhealthy", "ready", "agent_recovered")
    if n["state"] == "unhealthy":
        state(ago(minutes=7), "ready", "unhealthy", "heartbeat_lost")
    if n["state"] == "burned":
        state(ago(minutes=26), "ready", "burned", "verdict_blocked")
    gone = deleted_at(n)
    if gone is not None:
        why = n["why"]
        if why == "burned":
            state(gone - timedelta(minutes=40), "ready", "burned", "verdict_blocked")
            state(gone - timedelta(minutes=2), "burned", "retiring", "fleet_burned")
        elif why == "owner":
            state(gone - timedelta(minutes=2), "ready", "retiring", "owner_retire")
        else:
            state(gone - timedelta(minutes=2), "ready", "retiring", f"fleet_{why}")
        ev(gone - timedelta(seconds=70), "delete_started", hetzner_id=hetzner_id(n))
        ev(gone - timedelta(seconds=4), "server_deleted", hetzner_id=hetzner_id(n))
        state(gone, "retiring", "deleted", "server_deleted")
    return out


# --- Cloudflare: zones, DNS names, checks from Iran, the certificate -----------------------------------------


def zone_id(name: str) -> str:
    return hex32(f"zone:{name}")


async def seed_cloudflare(conn: AsyncConnection, vault: Vault) -> None:
    await vault.put(conn, "cloudflare:token", SecretValue(PLACEHOLDER_SECRET))
    await conn.execute(
        insert(CfState).values(
            id=1, verified=True, checked_at=ago(minutes=12), error=None, missing=[], token_kind="account",
            zones_refreshed_at=ago(minutes=12), updated_at=ago(minutes=12),
        )
    )
    for z in ZONES:
        changed = ago(days=z["changed_d"]) if z["changed_d"] is not None else None
        await conn.execute(
            insert(CfZone).values(
                id=zone_id(z["name"]),
                name=z["name"],
                status=z["status"],
                pool_state=z["pool"],
                pool_changed_at=changed,
                dns_edit=None if z["status"] == "pending" else True,
                seen_at=ago(minutes=12) if z["status"] != "gone" else ago(days=9),
                filtered_at=changed if z["pool"] == "filtered" else (ago(days=10) if z["pool"] == "deleted" else None),
                deleted_at=changed if z["pool"] == "deleted" else None,
                created_at=ago(days=z["seen_d"]),
                updated_at=changed or ago(days=z["seen_d"]),
            )
        )
    # filter checks from Iran (Globalping DNS through Iranian resolvers)
    def nets(hijacked: tuple[int, ...]) -> list[dict[str, Any]]:
        return [
            {"asn": asn, "network": name, "answered": 2 if i < 3 else 1, "hijacked": (2 if i < 3 else 1) if asn in hijacked else 0}
            for i, (asn, name) in enumerate(NETS)
        ]

    def check(zone: str, at: datetime, pool: str, hij: tuple[int, ...], verdict: str, reasons: list[str], status: str = "ok") -> dict[str, Any]:
        detail = nets(hij) if status == "ok" else []
        return {
            "zone_id": zone_id(zone), "at": at, "pool_state": pool, "status": status, "ref": hex32(f"{zone}{at}")[:16],
            "probes_asked": 12, "probes": sum(d["answered"] for d in detail) if detail else 0,
            "answered": sum(d["answered"] for d in detail), "hijacked": sum(d["hijacked"] for d in detail),
            "networks_answered": len(detail), "networks_hijacked": sum(1 for d in detail if d["hijacked"]),
            "detail": detail, "verdict": verdict, "verdict_reasons": reasons,
        }

    mci_rtl = (197207, 57218, 44244)
    await conn.execute(
        insert(DomainCheck),
        [
            check(ZONE_ACTIVE, ago(hours=1, minutes=40), "active", (), "clear", ["clear"]),
            check(ZONE_ACTIVE, ago(minutes=38), "active", (), "clear", ["clear"]),
            check("sepehr-link.example", ago(hours=3, minutes=5), "standby", (), "clear", ["clear"]),
            check("mahtab-net.example", ago(hours=4, minutes=20), "standby", (58224,), "unknown", ["weak_signal"]),
            check("kavir-proxy.example", ago(hours=10, minutes=30), "standby", mci_rtl, "unknown", ["single_round"]),
            check("kavir-proxy.example", ago(hours=9, minutes=50), "standby", mci_rtl, "filtered", ["filtered"]),
            check("alborz-gate.example", ago(days=30, hours=2), "active", (), "unknown", ["no_answers"], status="no_probes"),
            check("zagros-cdn.example", ago(days=10, hours=1), "active", mci_rtl + (58224,), "filtered", ["filtered"]),
        ],
    )
    # the slot names on the active domain follow the node that serves each slot
    for slot in range(1, 9):
        owner = next(n for n in NODES if n["slot"] == slot and n["state"] in SERVING)
        name = slot_host(slot)
        await conn.execute(
            insert(CfRecord).values(
                id=uid(f"record:{name}"),
                zone_id=zone_id(ZONE_ACTIVE),
                cf_record_id=hex32(f"record:{name}"),
                name=name,
                type="A",
                content=owner["ip"],
                ttl=60,
                proxied=False,
                purpose="slot",
                node_id=node_id(owner["key"]),
                created_at=max(ago(days=9.9), ready_at(owner) or NOW),
                updated_at=max(ago(days=9.9), ready_at(owner) or NOW),
            )
        )
    await conn.execute(
        insert(AcmeCertificate).values(
            domain=ZONE_ACTIVE, zone_id=zone_id(ZONE_ACTIVE), not_before=ago(days=9.9), not_after=ago(days=-80),
            serial_hex=hex32("cert")[:36], fingerprint_sha256=hex64("cert"), issued_at=ago(days=9.9),
            last_attempt_at=ago(days=9.9), attempts=0, updated_at=ago(days=9.9),
        )
    )


# --- agents, Telemt, MTProto proxies ---------------------------------------------------------------------------


def mt_users(n: dict[str, Any]) -> list[tuple[str, int]]:
    """(Telemt user name, ordinal) of a node's public proxies: its primary, and for two slots one
    extra link (decision D49)."""
    nid = node_id(n["key"])
    users = [(user_name_for(nid), 0)]
    if n["key"] in ("s1", "s2"):
        users.append((extra_user_name(nid, 1), 1))
    return users


async def seed_agents(conn: AsyncConnection) -> None:
    seal_key = load_proxy_seal_key()
    session = AsyncSession(bind=conn)
    try:
        for n in NODES:
            if "agent" not in n:
                continue
            nid, ready, gone = node_id(n["key"]), ready_at(n), deleted_at(n)
            assert ready is not None
            if n["state"] == "unhealthy":
                seen = ago(minutes=9, seconds=10)
            elif gone is not None:
                seen = gone - timedelta(minutes=3)
            else:
                seen = ago(seconds=RND.randint(8, 50))
            etag = '"' + hex64(f"etag:{n['key']}") + '"'
            await conn.execute(
                insert(AgentNode).values(
                    node_id=nid, agent_version=n["agent"], telemt_version=TELEMT_VERSION,
                    registered_at=ready - timedelta(seconds=70), last_seen_at=seen, last_served_etag=etag,
                    last_report_at=seen, last_applied_etag=etag, last_apply_ok=True, last_apply_error=None,
                )
            )
            if live(n):
                for back in (2, 1, 0):
                    at = seen - timedelta(minutes=back)
                    await conn.execute(
                        insert(AgentNodeReport).values(
                            node_id=nid, at=at, applied_etag=etag, apply_ok=True, telemt_up=True,
                            telemt_uptime_s=(at - ready).total_seconds(), connections_total=RND.randint(40_000, 900_000),
                            connections_bad_total=RND.randint(200, 4000), handshake_timeouts_total=RND.randint(50, 900),
                            me_route_drops_total=RND.randint(0, 12), accept_permit_timeouts_total=0,
                            users_reported=len(mt_users(n)), sys_uptime_s=(at - created_at(n)).total_seconds(),
                            load1=round(RND.uniform(0.08, 1.9), 2), mem_total_bytes=2 * 1024**3,
                            mem_available_bytes=RND.randint(900, 1500) * 1024**2, cpu_count=2,
                        )
                    )
            if n.get("test"):
                continue
            await set_telemt_node_config(session, nid, ports=[MT_PORT], tls_domain=slot_host(n["slot"]))
            for name, ordinal in mt_users(n):
                user = await create_telemt_user(session, seal_key, nid, name)
                user.enabled = gone is None
                user.last_current_connections = 0 if gone else max(0, int(n.get("conns", 0) * (0.7 if ordinal == 0 else 0.3)))
                octets = int(n.get("gb_day", 5) * GB * (seen - ready).total_seconds() / 86400 * 0.42)
                user.last_total_octets = int(octets * (0.75 if ordinal == 0 else 0.25))
                user.last_stats_at = seen
                await session.flush()
                await conn.execute(
                    insert(MtprotoProxy).values(
                        node_id=nid, user_name=name, ordinal=ordinal, created_at=ready - timedelta(seconds=50),
                        live_at=ready + timedelta(seconds=35), disabled_at=gone - timedelta(minutes=2) if gone else None,
                        updated_at=gone or ready,
                    )
                )
        await session.flush()
    finally:
        await session.close()


# --- Telegram: bot, owner, channels, the operator account -------------------------------------------------------


async def seed_telegram(conn: AsyncConnection, vault: Vault) -> None:
    await vault.put(conn, tg_store.BOT_SECRET, SecretValue(PLACEHOLDER_SECRET))
    await put(conn, tg_store.KEY_BOT, {"id": BOT_ID, "username": BOT_USERNAME, "set_at": iso(ago(days=33))})
    await put(conn, tg_store.KEY_OWNER, {"chat_id": OWNER_CHAT, "paired_at": iso(ago(days=33))})
    rights = {"can_post": True, "can_edit": True, "can_delete": False, "checked_at": iso(ago(minutes=14))}
    await put(
        conn,
        tg_store.KEY_CHANNELS,
        {
            "main": {"chat_id": MAIN_CHAT, "username": MAIN_USERNAME, "title": "پروکسی و کانفیگ رایگان | iPoxxo", **rights},
            "test": {"chat_id": TEST_CHAT, "username": TEST_USERNAME, "title": "آزمایشگاه iPoxxo", **rights},
        },
    )
    await tg_store.save_posting_settings(
        conn,
        tg_store.PostingSettings(
            quiet_windows=[QuietWindow.parse("01:30", "07:00"), QuietWindow.parse("13:00", "13:40")],
            post_interval_min=45,
        ),
    )
    await put(conn, tg_store.KEY_UPDATES, {"bot_id": BOT_ID, "offset": 48213377, "last_poll_at": iso(ago(seconds=6)), "error": None})
    await put(conn, tg_store.KEY_LAST_ALERT, {"at": iso(ago(minutes=24))})
    await posting_settings.save(conn, posting_settings.PostingConfig(main_enabled=True))

    # The second (operator) account as connected: its status row, and a placeholder where the
    # session would be, so the pages that ask "is a session stored" say yes. It is not a session
    # string and there are no API credentials next to it, so nothing can ever log in with it.
    await vault.put(conn, account_store.SESSION_SECRET, SecretValue(PLACEHOLDER_SECRET))
    row = account_state.connected(
        account_state.AccountRow(phone_masked=mask_phone("+447700900123")),  # Ofcom's drama range
        session_id=hex32("operator-session")[:16],
        user_id=5_000_001,
        first_name="پشتیبان کانال",
        mtproxybot_id=93_372_553,
        check_error=None,
        now=ago(days=27),
    )
    row = account_state.AccountRow(**{**row.to_json(), "last_check_at": iso(ago(minutes=11))})
    await account_store.save_row(conn, row)

    # posts go out through that account, with the channel's own emoji pack
    found = await own_pack.target(conn)
    if isinstance(found, own_pack.Target):
        alts = tuple(a.alt for a in emoji_art.ART)
        entries = tuple((str(5_368_000_000_000_000_000 + i * 7919), alt) for i, alt in enumerate(alts))
        await own_pack.save_state(
            conn,
            own_pack.PackState(
                name=found.name, title=found.title, owner_id=found.owner_id, alts=alts, entries=entries,
                created_at=ago(days=20), checked_at=ago(hours=5), attempted_at=ago(hours=5),
            ),
        )
        await operator.save_config(conn, operator.OperatorConfig(poster="operator", emoji_pack=found.name))
        await operator.save_pack(
            conn,
            operator.StoredPack(
                short_name=found.name, title=found.title, entries=tuple((int(i), alt) for i, alt in entries),
                fetched_at=ago(hours=5), attempted_at=ago(hours=5),
            ),
        )
    admin = {"status": "admin", "can_post": True, "can_edit": True, "can_delete": False, "error": None, "telegram_error": None}
    await operator.save_rights(
        conn,
        {
            "checked_at": iso(ago(minutes=22)),
            "channels": {"main": {"chat_id": MAIN_CHAT, **admin}, "test": {"chat_id": TEST_CHAT, **admin}},
        },
    )


# --- sponsor: registrations in @MTProxybot, campaigns, the bot's own list ------------------------------------------


async def seed_sponsor(conn: AsyncConnection) -> None:
    await sponsor_store.save_settings(conn, sponsor_store.SponsorSettings(default_channel="ipoxxo", lead_minutes=120))
    regs: dict[str, tuple[uuid.UUID, str | None]] = {}  # node key -> (registration id, tag)

    async def flow(n: dict[str, Any], *, tag: bool, at: datetime, **values: Any) -> uuid.UUID:
        nid = node_id(n["key"])
        user = user_name_for(nid)
        flow_id = await sponsor_store.insert_candidate(
            conn, host=slot_host(n["slot"]) if live(n) else n["ip"], port=MT_PORT, user_name=user,
            proxy_key=proxy_key(nid, user), now=at,
        )
        if tag:
            ad_tag = hex32(f"tag:{n['key']}")
            reg_id = await sponsor_store.create_registration(
                conn, flow_id, host=slot_host(n["slot"]) if live(n) else n["ip"], port=MT_PORT, secret_name=user,
                ad_tag=ad_tag, now=at + timedelta(minutes=3),
            )
            await conn.execute(
                update(MtproxyRegistration).where(MtproxyRegistration.id == reg_id).values(created_at=at)
            )
            regs[n["key"]] = (reg_id, ad_tag)
            await conn.execute(
                update(TelemtUser)
                .where(TelemtUser.node_id == nid)
                .values(ad_tag=ad_tag)
            )
            await conn.execute(
                update(TelemtUser).where(TelemtUser.node_id == nid, TelemtUser.name == user).values(registration_id=reg_id)
            )
        if values:
            await sponsor_store.update_flow(conn, flow_id, values.pop("updated", at), **values)
        return flow_id

    async def promoted(key: str, at: datetime, channel: str | None = "ipoxxo") -> None:
        reg_id, _ = regs[key]
        await sponsor_store.update_reg(conn, reg_id, at, sponsor_channel=channel)

    for key in ("s1", "s2", "s4", "s5", "s8"):
        n = node(key)
        at = (ready_at(n) or NOW) + timedelta(minutes=6)
        await flow(n, tag=True, at=at, last_step="sent_channel", promotion_set_at=at + timedelta(minutes=5), tag_applied_at=at + timedelta(minutes=4))
        await promoted(key, at + timedelta(minutes=5))
    n = node("s3")  # the sponsor was set half an hour ago: not yet visible in Telegram
    at = (ready_at(n) or NOW) + timedelta(minutes=6)
    await flow(n, tag=True, at=at, last_step="sent_channel", promotion_set_at=ago(minutes=34), tag_applied_at=at + timedelta(minutes=4))
    await promoted("s3", ago(minutes=34))
    n = node("s6")  # registered, the sponsor not set yet (the bot did not answer; next try soon)
    at = (ready_at(n) or NOW) + timedelta(minutes=6)
    await flow(n, tag=True, at=at, last_step="pressed_promotion", attempts=2, last_error="no_reply",
               next_attempt_at=ago(minutes=-11), tag_applied_at=at + timedelta(minutes=4))
    n = node("s7")  # stuck on a reply the panel does not understand: the owner was told
    at = (ready_at(n) or NOW) + timedelta(minutes=6)
    await flow(
        n, tag=True, at=at, last_step="sent_channel", attempts=1, stuck_reason="channel_refused", stuck_hint="admin_required",
        stuck_at=ago(hours=5, minutes=12), alerted_at=ago(hours=5, minutes=12), tag_applied_at=at + timedelta(minutes=4),
        last_bot_text=(
            "Sorry, you must be an administrator of the channel to promote it.\n"
            "Please add this account as an admin of @ipoxxo and send the link again."
        ),
    )
    for key in ("h3", "h6"):  # the registrations of servers that are gone
        n = node(key)
        at = (ready_at(n) or NOW) + timedelta(minutes=6)
        await flow(n, tag=True, at=at, last_step="sent_channel", promotion_set_at=at + timedelta(minutes=5))
        gone = deleted_at(n)
        await sponsor_store.update_reg(conn, regs[key][0], gone or NOW, state="retired", state_reason="node_gone", sponsor_channel="ipoxxo")

    await conn.execute(
        insert(SponsorCampaign),
        [
            {"id": uid("campaign:1"), "customer": "فروشگاه اینترنتی نیکان", "channel": "nikan_store_demo",
             "starts_at": ago(days=9), "ends_at": ago(days=7), "price": Decimal("45.00"), "currency": "EUR",
             "paid": True, "registration_ids": {}, "state": "finished", "created_at": ago(days=12), "updated_at": ago(days=7)},
            {"id": uid("campaign:2"), "customer": "آکادمی زبان پارسه", "channel": "parseh_academy_demo",
             "starts_at": ago(days=-2, hours=-3), "ends_at": ago(days=-4, hours=-3), "price": Decimal("60.00"),
             "currency": "USDT", "paid": True, "registration_ids": {}, "state": "scheduled",
             "created_at": ago(days=1, hours=4), "updated_at": ago(days=1, hours=4)},
            {"id": uid("campaign:3"), "customer": "کافه‌کتاب راوی", "channel": "ravi_cafe_demo",
             "starts_at": ago(days=5), "ends_at": ago(days=4), "price": Decimal("20.00"), "currency": "EUR",
             "paid": False, "registration_ids": {}, "state": "cancelled", "created_at": ago(days=8), "updated_at": ago(days=6)},
        ],
    )
    await put(conn, pacing.KEY, pacing.Pace(sent=tuple(ago(minutes=m) for m in (52, 51, 49, 37, 36, 34, 12)), hold_until=None).to_json())

    # @MTProxybot's own /myproxies list as the operator account last read it
    read_at = ago(hours=3, minutes=8)
    entries: list[dict[str, Any]] = []

    def entry(host: str, port: int | None, tag: str | None, reg: uuid.UUID | None, **more: Any) -> None:
        label = f"{host}:{port}" if port else host
        label = f"{label} {tag}" if tag else label
        fp = replies.label_fingerprint(label)
        ordinal = sum(1 for e in entries if e["label_fp"] == fp)
        entries.append(
            {"id": uid(f"entry:{len(entries)}"), "host": host, "port": port, "label_shape": replies.shape(label, limit=200),
             "label_fp": fp, "ordinal": ordinal, "tag_registration_id": reg, "page": len(entries) // 6,
             "position": len(entries), "pressable": True, "first_seen_at": more.pop("first", ago(days=20)),
             "seen_at": read_at, "menu_read_at": None, "menu_channel": None, "menu_note": None,
             "removal_state": None, "removal_reason": None, "removal_at": None, **more}
        )

    for key in ("s1", "s2", "s4", "s5", "s8"):
        entry(slot_host(node(key)["slot"]), MT_PORT, regs[key][1], regs[key][0], menu_read_at=read_at, menu_channel="ipoxxo",
              first=(ready_at(node(key)) or NOW) + timedelta(minutes=9))
    entry(slot_host(3), MT_PORT, regs["s3"][1], regs["s3"][0], menu_read_at=read_at, menu_note="not_shown", first=ago(days=4, hours=22))
    entry(slot_host(6), MT_PORT, regs["s6"][1], regs["s6"][0], first=ago(days=5, hours=20))
    entry(slot_host(7), MT_PORT, regs["s7"][1], regs["s7"][0], menu_read_at=read_at, menu_note="not_shown", first=ago(days=15, hours=20))
    entry(node("h3")["ip"], MT_PORT, regs["h3"][1], regs["h3"][0], first=ago(days=23), removal_state="observed",
          removal_reason="remove_button_found", removal_at=ago(hours=3))
    entry(node("h6")["ip"], MT_PORT, regs["h6"][1], regs["h6"][0], first=ago(days=11))
    entry("198.51.100.230", 443, None, None, first=ago(days=19))  # an old address of ours, its rows gone
    entry("proxy.old-setup.example", 443, None, None, first=ago(days=40))  # nothing the panel knows
    await conn.execute(insert(MtproxybotEntry), entries)

    job = inventory.Job(
        kind=inventory.CLEANUP, mode=inventory.OBSERVE, state=inventory.DONE, requested_by="admin:owner",
        requested_at=ago(hours=3, minutes=12), started_at=ago(hours=3, minutes=11), finished_at=ago(hours=3),
        list_read_at=read_at, observed=f"{node('h3')['ip']}:{MT_PORT}", reason="removal_disabled",
    )
    screens = [
        {"stage": "list", "text": "Here is the list of all proxies you created:", "about": "/myproxies",
         "buttons": [["<host:port> [hex]"], ["<host:port> [hex]"], ["<host:port> [hex]"], ["»"]], "answer": None, "at": iso(read_at)},
        {"stage": "menu", "text": "Proxy: <host:port>\nSecret: [hex]\nTag: [hex]\nPromoted channel: not set", "about": f"{node('h3')['ip']}:{MT_PORT}",
         "buttons": [["Stats", "Set promotion"], ["Delete proxy"], ["« Back"]], "answer": None, "at": iso(ago(hours=3, minutes=2))},
        {"stage": "menu", "text": "Proxy: <host:port>\nSecret: [hex]\nTag: [hex]\nPromoted channel: @ipoxxo", "about": f"{slot_host(1)}:{MT_PORT}",
         "buttons": [["Stats", "Set promotion"], ["Delete proxy"], ["« Back"]], "answer": None, "at": iso(ago(hours=3, minutes=5))},
    ]
    await inventory.save_state(
        conn,
        inventory.BotListState(
            removal_enabled=False,
            job=job,
            last_list=inventory.ListInfo(read_at=read_at, pages=2, complete=True, note=None, total=len(entries)),
            observation={"at": iso(ago(hours=3, minutes=2)), "address": f"{node('h3')['ip']}:{MT_PORT}", "remove_buttons": 1,
                         "names_proxy": True, "remove_named": True, "result": "remove_button_found"},
            screens=tuple(screens),
            check_queued_at=ago(hours=3, minutes=12),
            checked_at=ago(hours=3),
        ),
    )


# --- Reality: keys, configs, what the nodes report -----------------------------------------------------------------


async def seed_reality(conn: AsyncConnection, vault: Vault) -> dict[str, list[int]]:
    """Returns the config ids per node key (newest last)."""
    settings = RealitySettings()
    await save_reality_settings(conn, settings)
    key_ids: dict[int, int] = {}
    for slot in range(1, 9):
        pair = new_key_pair()
        name = f"reality:key:slot:{slot}"
        await vault.put(conn, name, pair.private)
        key_ids[slot] = (
            await conn.execute(
                insert(RealityKey)
                .values(slot=slot, node_id=None, public_key=pair.public, short_id=new_short_id(), target=settings.target,
                        server_name=settings.target_host, secret_name=name, ws_path=new_ws_path(), created_at=ago(days=21))
                .returning(RealityKey.id)
            )
        ).scalar_one()

    made: dict[str, list[int]] = {}
    cap = settings.cap_bytes

    async def config(key: str, *, born_h: float, status: str = "active", used: float = 0.0, ended_h: float | None = None,
                     transport: str = "ws_tls", life_h: float = 48) -> int:
        n = node(key)
        born = ago(hours=born_h)
        cid = (
            await conn.execute(
                insert(RealityConfig)
                .values(
                    node_id=node_id(key), slot=n["slot"], key_id=key_ids[n["slot"]], uuid=uid(f"config:{key}:{born_h}"),
                    host=slot_host(n["slot"]) if transport == "ws_tls" else n["ip"], created_at=born,
                    expires_at=born + timedelta(hours=life_h), data_cap_bytes=cap, used_bytes=int(cap * used),
                    status=status, ended_at=ago(hours=ended_h) if ended_h is not None else None, transport=transport,
                )
                .returning(RealityConfig.id)
            )
        ).scalar_one()
        made.setdefault(key, []).append(int(cid))
        if used:
            await conn.execute(
                insert(RealityUsage).values(config_id=cid, node_id=node_id(key), total_bytes=int(cap * used),
                                            last_reported=int(cap * used), updated_at=ago(hours=ended_h or 0, minutes=1))
            )
        return int(cid)

    # ended in the last days (oldest first, so the newest configs get the highest numbers)
    await config("s1", born_h=150, status="expired", used=0.31, ended_h=102)
    await config("s2", born_h=147, status="expired", used=0.58, ended_h=99)
    await config("s4", born_h=131, status="full", used=1.0, ended_h=96, life_h=48)
    await config("s5", born_h=128, status="expired", used=0.22, ended_h=80)
    await config("s1", born_h=101, status="expired", used=0.44, ended_h=53, transport="reality")
    await config("s2", born_h=99, status="full", used=1.0, ended_h=61)
    await config("s7", born_h=97, status="expired", used=0.37, ended_h=49)
    await config("s8", born_h=60, status="burned", used=0.12, ended_h=0.4)
    await config("s8", born_h=33, status="burned", used=0.05, ended_h=0.4)
    await config("s4", born_h=95, status="expired", used=0.41, ended_h=47)
    await config("s5", born_h=79, status="expired", used=0.66, ended_h=31)
    await config("s3", born_h=76, status="expired", used=0.19, ended_h=28)
    # active now: two per serving slot, half a lifetime apart
    spread = {"s1": (0.47, 0.11), "s2": (0.93, 0.28), "s3": (0.22, 0.04), "s4": (0.35, 0.09),
              "s5": (0.61, 0.17), "s6": (0.14, 0.03), "s7": (0.52, 0.2)}
    for i, (key, (old, new)) in enumerate(spread.items()):
        await config(key, born_h=41 - i * 1.5, used=old)
        await config(key, born_h=17 - i * 1.5, used=new)

    for n in NODES:
        if n["state"] not in SERVING:
            continue
        reported = ago(minutes=9, seconds=10) if n["state"] == "unhealthy" else ago(seconds=RND.randint(10, 50))
        host = slot_host(n["slot"])
        new_agent = n["agent"] == "0.5.0"
        totals = (int(n["gb_day"] * GB * 0.06 * 3), int(n["gb_day"] * GB * 0.52 * 3))
        active = [
            XrayUserTotal(config_email(cid), 0)
            for cid in made.get(n["key"], [])[-2:]
            if n["state"] != "burned"
        ]
        await store_xray_report(
            conn, node_id(n["key"]), enabled=True, up=True, version=XRAY_VERSION,
            uptime_s=(reported - (ready_at(n) or NOW)).total_seconds(), target_ok=True,
            checked=(f"{host}:{REALITY_PORT}", host), apply_error=None, users=[], now=reported,
            xhttp_port=XHTTP_PORT if n["agent"] >= "0.4.0" else None, traffic=totals if new_agent else None,
        )
        await conn.execute(
            text("UPDATE reality_node_status SET users_reported = :n WHERE node_id = :id"),
            {"n": len(active), "id": node_id(n["key"])},
        )
        if new_agent:  # D61: the node's hourly up/down totals of the last two days
            hour = NOW.replace(minute=0, second=0)
            rows = []
            for h in range(1, 49):
                at = hour - timedelta(hours=h)
                if at < (ready_at(n) or NOW):
                    break
                load = day_curve(at)
                down = int(n["gb_day"] * GB * 0.52 / 24 * load * RND.uniform(0.85, 1.15))
                rows.append({"node_id": node_id(n["key"]), "hour": at, "up": int(down * 0.11), "down": down})
            if rows:
                await conn.execute(text("DELETE FROM reality_traffic_hours WHERE node_id = :id"), {"id": node_id(n["key"])})
                await conn.execute(insert(RealityTrafficHour), rows)
    return made


def day_curve(at: datetime) -> float:
    """How busy an hour is (Tehran time): quiet before dawn, a long evening peak."""
    h = at.astimezone(TEHRAN).hour
    curve = [0.45, 0.3, 0.2, 0.14, 0.12, 0.16, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0,
             1.05, 1.0, 0.95, 1.0, 1.1, 1.25, 1.45, 1.7, 1.9, 2.0, 1.6, 0.9]
    return curve[h]


# --- probes from Iran, candidate sites ---------------------------------------------------------------------------------


async def seed_probes(conn: AsyncConnection) -> None:
    await conn.execute(
        insert(ProbeSource),
        [
            {"name": "globalping", "ok": True, "note": None, "checked_at": ago(minutes=2), "ratelimit_limit": 500,
             "ratelimit_remaining": 312, "ratelimit_reset_at": ago(minutes=-41), "tls_probes": 9},
            {"name": "check-host", "ok": True, "note": None, "checked_at": ago(minutes=2), "ratelimit_limit": None,
             "ratelimit_remaining": None, "ratelimit_reset_at": None, "tls_probes": None},
        ],
    )
    measured = [n for n in NODES if n["state"] in SERVING]
    cycles = [(62, 61), (32, 31), (3, 2)]
    for started_m, finished_m in cycles:
        latest = started_m == cycles[-1][0]
        started = ago(minutes=started_m)
        cycle = (
            await conn.execute(
                insert(ProbeCycle).values(started_at=started, finished_at=ago(minutes=finished_m)).returning(ProbeCycle.id)
            )
        ).scalar_one()
        await conn.execute(
            insert(ProbeMeasurement),
            [
                {"cycle_id": cycle, "node_id": None, "at": started, "source": "globalping", "kind": "https", "role": "control",
                 "target": "github.com", "port": 443, "status": "ok", "ref": hex32(f"ctl{started_m}")[:16]},
                {"cycle_id": cycle, "node_id": None, "at": started, "source": "check-host", "kind": "tcp", "role": "control",
                 "target": "www.hetzner.com", "port": 443, "status": "ok", "ref": hex32(f"ctc{started_m}")[:12]},
            ],
        )
        for n in measured:
            nid = node_id(n["key"])
            closed = n["reach"].count("c")
            await conn.execute(
                insert(ProbeRound).values(
                    cycle_id=cycle, node_id=nid, started_at=started, finished_at=started + timedelta(seconds=48),
                    trigger="schedule", target_ip=n["ip"], port=MT_PORT,
                    observations={"globalping": {"state": "blocked" if closed >= 4 else "ok", "reason": None},
                                  "check-host": {"state": "blocked" if closed >= 4 else "ok", "reason": None}},
                    verdict=n["verdict"], verdict_reasons=[],
                )
            )
            if not latest:
                continue
            for port in (REALITY_PORT, MT_PORT):
                mid = (
                    await conn.execute(
                        insert(ProbeMeasurement)
                        .values(cycle_id=cycle, node_id=nid, at=started + timedelta(seconds=6), source="globalping", kind="https",
                                role="node", target=slot_host(n["slot"]), port=port, status="ok", ref=hex32(f"{n['key']}{port}")[:16])
                        .returning(ProbeMeasurement.id)
                    )
                ).scalar_one()
                rows = []
                for (asn, name), mark in zip(NETS, n["reach"], strict=True):
                    opened = mark == "o"
                    rows.append(
                        {"measurement_id": mid, "network_key": f"asn:{asn}", "network": name, "asn": asn,
                         "ok": 2 if opened else 0, "fail": 2 if mark == "c" else 0, "nodata": 0,
                         "rtt_ms": round(RND.uniform(180, 420), 1) if opened else None,
                         "detail": {"failure": "tls_timeout"} if mark == "c" else {}}
                    )
                await conn.execute(insert(ProbeNetworkResult), rows)
            mid = (
                await conn.execute(
                    insert(ProbeMeasurement)
                    .values(cycle_id=cycle, node_id=nid, at=started + timedelta(seconds=9), source="check-host", kind="tcp",
                            role="node", target=n["ip"], port=MT_PORT, status="ok", ref=hex32(f"{n['key']}tcp")[:12])
                    .returning(ProbeMeasurement.id)
                )
            ).scalar_one()
            await conn.execute(
                insert(ProbeNetworkResult),
                [
                    {"measurement_id": mid, "network_key": f"ch:ir{i}", "network": f"check-host ir{i} ({city})", "asn": None,
                     "ok": 0 if closed >= 4 and i != 2 else 3, "fail": 3 if closed >= 4 and i != 2 else 0, "nodata": 0,
                     "rtt_ms": None if closed >= 4 and i != 2 else round(RND.uniform(90, 260), 1), "detail": {}}
                    for i, city in ((1, "Tehran"), (2, "Shiraz"), (3, "Tabriz"))
                ],
            )
    for n in NODES:
        if "reach" not in n:
            continue
        nid = node_id(n["key"])
        at = deleted_at(n) - timedelta(hours=1) if deleted_at(n) else ago(minutes=2)  # type: ignore[operator]
        await conn.execute(
            insert(ProbeNodeState).values(
                node_id=nid, last_round_at=at, verdict=n["verdict"], verdict_at=at,
                verdict_reasons=["tls_closed_on_key_networks"] if n["verdict"] == "blocked" else [],
            )
        )
        await conn.execute(
            insert(ProbeLocationReach),
            [
                {"node_id": nid, "network_key": f"asn:{asn}", "network": name, "asn": asn, "location": n["loc"],
                 "state": "open" if mark == "o" else "closed", "at": at}
                for (asn, name), mark in zip(NETS, n["reach"], strict=True)
                if mark != "-"
            ],
        )

    # candidate REALITY targets / fake-TLS domains, measured by a TLS handshake from Iran
    def round_(marks: str, base: float) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for (asn, name), mark in zip(NETS[:4], marks, strict=True):
            state = {"o": "up", "c": "down", "-": "nodata"}[mark]
            out[f"asn:{asn}"] = {"state": state, "network": name, "asn": asn, "ms": round(base * RND.uniform(0.8, 1.3), 1) if state == "up" else None}
        return out

    sites = [
        ("github.com", "oooo", "oooo", 310, "ok"), ("www.speedtest.net", "oooo", "oo-o", 420, "ok"),
        ("aws.amazon.com", "ocoo", "oooo", 380, None), ("www.cloudflare.com", "oooo", "oooo", 260, "ok"),
        ("www.mozilla.org", "cccc", "cccc", 0, "fail"), ("addons.mozilla.org", "coco", "cooo", 0, None),
        ("www.samsung.com", "oo-o", "----", 510, None), ("dl.google.com", "oooo", "oooo", 240, "ok"),
        ("www.microsoft.com", "oooo", "oooo", 290, None),
    ]
    await conn.execute(
        insert(TargetState),
        [
            {"host": host, "measured_at": ago(hours=1, minutes=25), "iran": round_(now_marks, base or 300),
             "prev_measured_at": ago(hours=7, minutes=25), "iran_prev": round_(prev_marks, base or 300),
             "node_ok_at": ago(minutes=4) if check == "ok" else None, "node_fail_at": ago(hours=2) if check == "fail" else None,
             "updated_at": ago(hours=1, minutes=24)}
            for host, now_marks, prev_marks, base, check in sites
        ],
    )
    await target_settings.save_state(conn, {"last_full_at": iso(ago(hours=1, minutes=25))})


# --- content: the owner's own texts, sources, the review queue ----------------------------------------------------

OWN_TEXTS: list[tuple[str, str, list[str]]] = [
    ("🔐 رمز دومرحله‌ای تلگرام را روشن کنید", "در <b>تنظیمات ← حریم خصوصی و امنیت ← تأیید دومرحله‌ای</b> یک رمز بگذارید. با این کار حتی اگر کد پیامکی به دست کس دیگری بیفتد، بدون رمز شما وارد حساب نمیشود.", ["تلگرام", "امنیت"]),
    ("📶 چرا پروکسی گاهی کند میشود؟", "در ساعت‌های شلوغ شب، همه با هم وصل میشوند. اگر یک پروکسی کند شد، پروکسی بعدی همین پست را امتحان کنید؛ هر کدام روی سرور جدایی است.", ["پروکسی"]),
    ("🧹 حافظه‌ی تلگرام را سبک کنید", "در <b>تنظیمات ← داده‌ها و ذخیره‌سازی ← مصرف حافظه</b> میتوانید کش را پاک کنید و سقف نگهداری رسانه را روی یک هفته بگذارید. پیام‌ها پاک نمیشوند.", ["تلگرام", "ترفند"]),
    ("🛡 لینک ناشناس را باز نکنید", "پیامی که میگوید «حساب شما مسدود میشود» یا «جایزه برده‌اید» تقریباً همیشه فریب است. تلگرام هیچ‌وقت از شما کد ورود نمیخواهد.", ["امنیت"]),
    ("📡 چند پروکسی را با هم ذخیره کنید", "تلگرام فهرست پروکسی‌ها را نگه میدارد و اگر یکی قطع شد خودش سراغ بعدی میرود. کافی است گزینه‌ی «استفاده از پروکسی» روشن بماند.", ["پروکسی", "ترفند"]),
    ("🔕 گروه‌های شلوغ را بی‌صدا کنید", "روی نام گروه بزنید و «بی‌صدا» را برای همیشه انتخاب کنید. با پوشه‌ها هم میتوانید گفت‌وگوهای مهم را از بقیه جدا نگه دارید.", ["تلگرام"]),
    ("🕵️ آخرین بازدید را پنهان کنید", "در <b>حریم خصوصی ← آخرین بازدید و آنلاین</b> گزینه‌ی «هیچ‌کس» را بگذارید. میتوانید برای چند مخاطب خاص استثنا بگذارید.", ["تلگرام", "حریم‌خصوصی"]),
    ("⚡️ کانفیگ ریلیتی چیست؟", "کانفیگ‌های این کانال برای برنامه‌هایی مثل v2rayNG و Streisand است. هر کانفیگ ۴۸ ساعت یا تا پر شدن حجمش کار میکند و بعد کانفیگ تازه میگذاریم.", ["کانفیگ"]),
    ("📲 نسخه‌ی رسمی تلگرام را نصب کنید", "نسخه‌های غیررسمی ممکن است پیام‌های شما را بخوانند. تلگرام را فقط از فروشگاه رسمی گوشی یا سایت telegram.org بگیرید.", ["امنیت", "تلگرام"]),
    ("🔋 مصرف باتری تلگرام را کم کنید", "در <b>تنظیمات ← صرفه‌جویی در مصرف انرژی</b> پخش خودکار ویدیو و استیکرهای متحرک را خاموش کنید. روی گوشی‌های قدیمی فرق زیادی دارد.", ["تلگرام", "گوشی"]),
    ("🌐 اگر وصل نشد، اول این را امتحان کنید", "حالت هواپیما را چند ثانیه روشن و خاموش کنید تا نشانی اینترنت همراه عوض شود. خیلی وقت‌ها همین کافی است.", ["پروکسی", "ترفند"]),
    ("🗂 پوشه‌بندی گفت‌وگوها", "در <b>تنظیمات ← پوشه‌های گفت‌وگو</b> برای کار، خانواده و کانال‌ها پوشه‌ی جدا بسازید. فهرست اصلی خلوت میشود و چیزی گم نمیشود.", ["تلگرام", "ترفند"]),
    ("📵 شماره‌ی خود را پنهان کنید", "در <b>حریم خصوصی ← شماره‌ی تلفن</b> «هیچ‌کس» را بگذارید تا اعضای گروه‌ها شماره‌ی شما را نبینند.", ["حریم‌خصوصی"]),
    ("💾 پیام‌های ذخیره‌شده، دفترچه‌ی شما", "هر چیزی را برای خودتان بفرستید: لینک، عکس، یادداشت. با برچسب‌ها و جست‌وجو بعداً راحت پیدا میشود.", ["تلگرام"]),
    ("🚦 پروکسی فعال چه رنگی است؟", "در فهرست پروکسی‌های تلگرام، کنار پروکسی سالم زمان پاسخ (مثلاً ۲۰۰ میلی‌ثانیه) نوشته میشود. «در دسترس نیست» یعنی آن را بردارید.", ["پروکسی"]),
    ("🧭 سرعت دانلود را در تلگرام بسنجید", "یک فایل ۱۰ مگابایتی را از «پیام‌های ذخیره‌شده» دوباره دانلود کنید. اگر کند بود، پروکسی دیگری از همین کانال را انتخاب کنید.", ["پروکسی", "ترفند"]),
    ("🔏 قفل برنامه بگذارید", "در <b>حریم خصوصی ← قفل با رمز</b> برای خود تلگرام رمز جدا بگذارید تا اگر گوشی دست کسی افتاد، گفت‌وگوها باز نشود.", ["امنیت", "تلگرام"]),
]

SOURCES: list[dict[str, Any]] = [
    {"key": "roz", "kind": "telegram", "ref": "khabar_rooz_demo", "label": "خبر روز", "count": 14, "ok_min": 3},
    {"key": "tech", "kind": "rss", "ref": "https://feeds.didehban-fanavari.example/latest.xml", "label": "دیده‌بان فناوری", "count": 6, "ok_min": 2},
    {"key": "sport", "kind": "telegram", "ref": "varzesh_nama_demo", "label": "ورزش‌نما", "count": 9, "ok_min": 4},
    {"key": "bazar", "kind": "rss", "ref": "https://bazar-emrooz.example/rss", "label": "بازار امروز", "count": 0, "ok_min": 250,
     "error": "timeout", "failures": 4},
    {"key": "click", "kind": "telegram", "ref": "dagh_tarin_demo", "label": "داغ‌ترین‌ها", "count": 0, "ok_min": 1500,
     "disabled": True},
]

# (source key, category, title, text). Invented, everyday news; no real person or outlet.
NEWS: list[tuple[str, str, str, str]] = [
    ("roz", "society", "🌧 بارش باران در ۱۲ استان از فردا", "سازمان هواشناسی از ورود سامانه‌ی بارشی تازه خبر داد. از فردا در استان‌های شمالی و غربی باران و در ارتفاعات برف پیش‌بینی میشود و دمای هوا تا ۸ درجه کم میشود."),
    ("roz", "society", "🚇 ساعت کار مترو در روزهای پایان هفته بیشتر شد", "شرکت بهره‌برداری مترو اعلام کرد قطارها در پنجشنبه و جمعه تا ساعت ۲۳:۳۰ مسافر جابه‌جا میکنند. فاصله‌ی حرکت قطارها در ساعت‌های شلوغ هم کمتر شده است."),
    ("tech", "science_tech", "📱 به‌روزرسانی تازه‌ی تلگرام با پوشه‌های اشتراکی", "در نسخه‌ی تازه میتوان یک پوشه از گفت‌وگوها را با لینک برای دیگران فرستاد. جست‌وجو در پیام‌های ذخیره‌شده هم سریع‌تر شده و برچسب‌گذاری پیام‌ها اضافه شده است."),
    ("sport", "sport", "⚽️ پیروزی دو بر یک تیم ملی فوتسال در دیدار دوستانه", "تیم ملی فوتسال در دومین بازی تدارکاتی خود با دو گل به پیروزی رسید. هر دو گل در نیمه‌ی دوم و روی ضدحمله زده شد. بازی بعدی هفته‌ی آینده برگزار میشود."),
    ("roz", "economy", "⛽️ سهمیه‌ی بنزین این ماه بدون تغییر واریز شد", "سهمیه‌ی ماهانه‌ی بنزین بامداد امروز به کارت‌های سوخت واریز شد. مقدار و قیمت سهمیه نسبت به ماه گذشته تغییری نکرده است."),
    ("tech", "science_tech", "🔋 باتری‌هایی که در ده دقیقه شارژ میشوند به بازار نزدیک شدند", "یک شرکت سازنده از آغاز تولید آزمایشی باتری‌های حالت جامد خبر داد. این باتری‌ها در آزمایش‌ها پس از هزار بار شارژ هنوز ۹۰ درصد ظرفیت خود را داشتند."),
    ("sport", "sport", "🏐 صعود تیم والیبال نوجوانان به نیمه‌نهایی", "تیم والیبال نوجوانان با پیروزی سه بر یک در آخرین بازی گروهی به نیمه‌نهایی رسید. سرمربی تیم گفت دریافت‌های خوب کلید این برد بود."),
    ("roz", "health", "💉 آغاز واکسیناسیون رایگان آنفلوآنزا برای سالمندان", "مراکز بهداشت از امروز واکسن آنفلوآنزا را برای افراد بالای ۶۵ سال رایگان تزریق میکنند. برای نوبت‌گیری کافی است کارت ملی همراه باشد."),
    ("roz", "society", "🏫 مدرسه‌های سه شهرستان فردا غیرحضوری شد", "به دلیل بارش برف و لغزندگی جاده‌ها، کلاس‌های همه‌ی مقطع‌ها در سه شهرستان کوهستانی فردا غیرحضوری برگزار میشود."),
    ("tech", "science_tech", "🛰 پرتاب موفق ماهواره‌ی سنجش از دور دانشگاهی", "ماهواره‌ی کوچک دانشجویی در مدار ۵۰۰ کیلومتری قرار گرفت و نخستین سیگنال‌هایش دریافت شد. این ماهواره برای پایش خشکسالی و پوشش گیاهی ساخته شده است."),
    ("sport", "sport", "🥇 طلای وزنه‌برداری جوانان در دسته‌ی ۸۱ کیلوگرم", "وزنه‌بردار جوان کشورمان با مجموع ۳۴۰ کیلوگرم روی سکوی نخست رفت و رکورد دوضرب رده‌ی سنی خود را هم جابه‌جا کرد."),
    ("roz", "culture", "🎬 جشنواره‌ی فیلم کوتاه از شنبه آغاز میشود", "در این دوره ۶۲ فیلم کوتاه در چهار بخش داستانی، مستند، پویانمایی و تجربی نمایش داده میشود. تماشای فیلم‌ها در سه سالن برای همه رایگان است."),
    ("tech", "science_tech", "💡 مصرف برق مرکزهای داده با خنک‌سازی تازه نصف شد", "پژوهشگران با فروبردن سرورها در مایع نارسانا مصرف برق خنک‌سازی را تا ۵۰ درصد پایین آوردند. این روش در یک مرکز داده‌ی آزمایشی شش ماه بی‌وقفه کار کرده است."),
    ("roz", "economy", "🍎 قیمت میوه در میدان‌های تره‌بار کاهش یافت", "با رسیدن محصول باغ‌های شمال، قیمت پرتقال و نارنگی در میدان‌های میوه و تره‌بار نسبت به هفته‌ی پیش حدود ۱۵ درصد کمتر شده است."),
    ("sport", "sport", "🏀 لیگ بسکتبال با شش بازی از سر گرفته میشود", "هفته‌ی هشتم لیگ برتر بسکتبال فردا با شش دیدار در شهرهای مختلف برگزار میشود. تیم صدرنشین در خانه میزبان تیم سوم جدول است."),
    ("roz", "society", "🚗 محدودیت ترافیکی جاده‌های شمال در تعطیلات", "پلیس راه اعلام کرد از ظهر چهارشنبه تا شامگاه جمعه، رفت‌وآمد در دو محور شمالی در ساعت‌های شلوغ یک‌طرفه میشود."),
    ("tech", "science_tech", "🤖 دستیار هوشمند تازه، نوشته‌ی فارسی را بهتر میفهمد", "نسخه‌ی تازه‌ی یک مدل زبانی در آزمون‌های درک متن فارسی نمره‌ی بالاتری گرفته است و خلاصه‌نویسی و ترجمه‌ی محاوره‌ای را روان‌تر انجام میدهد."),
    ("roz", "health", "🥗 توصیه‌های تغذیه برای روزهای سرد", "کارشناسان تغذیه میگویند در فصل سرد، مصرف مرکبات، سبزی‌های برگ‌دار و نوشیدنی‌های گرم بدون شکر به قوی ماندن بدن کمک میکند."),
    ("sport", "sport", "🤼 قهرمانی تیم کشتی آزاد در جام دوستانه", "تیم کشتی آزاد با شش پیروزی از هشت کشتی فینال قهرمان جام شد. دو کشتی‌گیر جوان نخستین مدال طلای بزرگسالان خود را گرفتند."),
    ("roz", "society", "📚 کتابخانه‌های عمومی یک هفته رایگان عضو میگیرند", "به مناسبت هفته‌ی کتاب، عضویت یک‌ساله در همه‌ی کتابخانه‌های عمومی تا پایان هفته رایگان است و ساعت کار سالن‌های مطالعه هم بیشتر شده."),
    ("tech", "science_tech", "📷 دوربین گوشی‌های میان‌رده در شب بهتر شد", "حسگرهای تازه‌ی یک‌اینچی به گوشی‌های میان‌رده رسیده‌اند. در عکاسی شب، نویز کمتر و رنگ‌ها طبیعی‌تر دیده میشود."),
    ("roz", "economy", "🧾 مهلت ثبت اظهارنامه‌ی مالیاتی تمدید شد", "سازمان امور مالیاتی مهلت ثبت اظهارنامه‌ی کسب‌وکارهای کوچک را دو هفته تمدید کرد. ثبت اظهارنامه فقط از راه سامانه‌ی اینترنتی انجام میشود."),
    ("sport", "sport", "🚴 رکاب‌زن جوان پیراهن طلایی تور را حفظ کرد", "در مرحله‌ی کوهستانی تور دوچرخه‌سواری، رکاب‌زن ۲۲ ساله با اختلاف ۴۱ ثانیه صدرنشین ماند. فردا مرحله‌ی تایم‌تریل انفرادی برگزار میشود."),
    ("roz", "society", "🌫 شاخص آلودگی هوا در کلان‌شهرها به وضعیت نارنجی رسید", "با پایداری هوا، شاخص کیفیت هوا برای گروه‌های حساس ناسالم شده است. توصیه شده سالمندان و کودکان از فعالیت طولانی در فضای باز خودداری کنند."),
    ("tech", "science_tech", "🧬 تشخیص زودهنگام یک بیماری با آزمایش خون ساده", "گروهی از پژوهشگران روشی ساخته‌اند که با چند قطره خون، نشانه‌های اولیه‌ی بیماری را سال‌ها زودتر نشان میدهد. آزمایش بالینی گسترده سال آینده آغاز میشود."),
    ("roz", "culture", "🎻 کنسرت ارکستر سمفونیک در تالار شهر", "ارکستر سمفونیک در دو شب پیاپی قطعه‌هایی از آهنگسازان معاصر را اجرا میکند. بلیت‌ها از فردا به صورت اینترنتی فروخته میشود."),
    ("sport", "sport", "🏊 رکوردشکنی شناگر نوجوان در ۲۰۰ متر آزاد", "شناگر ۱۶ ساله در مسابقه‌های قهرمانی کشور رکورد ۲۰۰ متر آزاد رده‌ی سنی خود را ۸۳ صدم ثانیه بهتر کرد."),
    ("roz", "society", "🚌 خط تازه‌ی اتوبوس تندرو راه‌اندازی شد", "خط تازه‌ی اتوبوس تندرو با ۲۴ ایستگاه و ۶۰ اتوبوس نو، شرق و غرب شهر را به هم وصل میکند. سفر در هفته‌ی نخست رایگان است."),
]


async def seed_content(conn: AsyncConnection) -> dict[str, list[int]]:
    """Returns the ids of the content items and candidates that went out with posts."""
    item_ids: list[int] = []
    for i, (title, body, tags) in enumerate(OWN_TEXTS):
        status = "used" if i < 9 else ("approved" if i < 15 else "draft")
        used_at = ago(hours=6 + i * 9) if status == "used" else None
        item_ids.append(
            (
                await conn.execute(
                    insert(ContentItem)
                    .values(title=title, body=body, tags=tags, status=status, origin="owner",
                            used_count=(1 + i % 3) if status == "used" else 0, last_used_at=used_at,
                            created_at=ago(days=30 - i), updated_at=used_at or ago(days=30 - i))
                    .returning(ContentItem.id)
                )
            ).scalar_one()
        )

    source_ids: dict[str, int] = {}
    for i, s in enumerate(SOURCES):
        disabled = s.get("disabled", False)
        source_ids[s["key"]] = (
            await conn.execute(
                insert(ContentSource)
                .values(
                    kind=s["kind"], ref=s["ref"], label=s["label"], enabled=not disabled,
                    created_at=ago(days=18 - i * 2), updated_at=ago(minutes=s["ok_min"]),
                    last_fetch_at=ago(minutes=3 if not disabled else s["ok_min"]), last_ok_at=ago(minutes=s["ok_min"]),
                    last_error=s.get("error"), failures=s.get("failures", 0),
                    next_fetch_at=ago(minutes=-2) if not s.get("error") else ago(minutes=-47), last_count=s["count"],
                    disabled_reason="qc_rejects" if disabled else None, disabled_at=ago(days=1, hours=1) if disabled else None,
                    disabled_detail={"rejected": 17, "of": 20} if disabled else None,
                )
                .returning(ContentSource.id)
            )
        ).scalar_one()

    labels = {s["key"]: s for s in SOURCES}
    serial = {"n": 4100}

    async def candidate(src: str, category: str, title: str, body: str, status: str, *, age_h: float, **more: Any) -> int:
        s = labels[src]
        serial["n"] += RND.randint(1, 7)
        published = ago(hours=age_h)
        url = f"https://t.me/{s['ref']}/{serial['n']}" if s["kind"] == "telegram" else f"https://{s['ref'].split('/')[2]}/news/{serial['n']}"
        values: dict[str, Any] = {
            "source_id": source_ids[src], "source_kind": s["kind"], "source_label": s["label"], "external_id": str(serial["n"]),
            "url": url, "published_at": published, "views": RND.randint(900, 48_000) if s["kind"] == "telegram" else None,
            "source_text": f"{title.split(' ', 1)[1]}\n\n{body}\n\n🆔 @{s['ref']}" if s["kind"] == "telegram" else f"{title.split(' ', 1)[1]}\n\n{body}",
            "norm_text": "", "title": title, "body": body, "status": status, "media_state": "none",
            "created_at": published + timedelta(minutes=4), "updated_at": published + timedelta(minutes=9),
            "qc_category": category, "qc_tries": 1, "qc_backend": "bridge", "qc_model": "sonnet",
            "qc_at": published + timedelta(minutes=9),
        }
        values.update(more)
        return int((await conn.execute(insert(ContentCandidate).values(**values).returning(ContentCandidate.id))).scalar_one())

    news = list(NEWS)
    used_ids: list[int] = []
    out_media: list[int] = []
    # went out with posts (oldest first)
    for i in range(10):
        src, cat, title, body = news.pop()
        age = 80 - i * 7.5
        cid = await candidate(src, cat, title, body, "used", age_h=age, qc_decision="approve", decided_by="qc",
                              qc_reasons=["خبر روشن و بی‌حاشیه است", "متن کوتاه و خواناست"],
                              reviewed_at=ago(hours=age - 0.3), used_at=ago(hours=age - 1.2),
                              media_kind="photo" if i % 3 == 0 else None, media_state="ready" if i % 3 == 0 else "none")
        used_ids.append(cid)
    # approved, waiting for the next posts
    for i in range(4):
        src, cat, title, body = news.pop()
        cid = await candidate(src, cat, title, body, "approved", age_h=3.5 - i * 0.8, qc_decision="approve",
                              decided_by="owner" if i == 1 else "qc", qc_reasons=["خبر تازه و مناسب کانال است"],
                              reviewed_at=ago(hours=3.1 - i * 0.8), media_kind="photo" if i in (0, 2) else None,
                              media_state="ready" if i in (0, 2) else "none")
        if i in (0, 2):
            out_media.append(cid)
    # the review queue: what the supervisor left for the owner
    pending_notes: list[dict[str, Any]] = [
        {"qc_decision": None, "qc_at": None, "qc_tries": 0, "qc_backend": None, "qc_model": None, "qc_category": None},
        {"qc_decision": None, "qc_error": "bridge_timeout", "qc_tries": 2},
        {"qc_decision": None, "qc_at": None, "qc_tries": 0, "qc_backend": None, "qc_model": None, "qc_category": None},
        {"qc_decision": None, "qc_error": "rate_limited", "qc_tries": 1},
        {"qc_decision": None, "qc_at": None, "qc_tries": 0, "qc_backend": None, "qc_model": None, "qc_category": None},
        {"qc_decision": None, "qc_error": "over_cap", "qc_tries": 1, "qc_backend": "api", "qc_model": "claude-haiku-5-5"},
        {"qc_decision": None, "qc_at": None, "qc_tries": 0, "qc_backend": None, "qc_model": None, "qc_category": None},
        {"qc_decision": None, "qc_at": None, "qc_tries": 0, "qc_backend": None, "qc_model": None, "qc_category": None},
    ]
    pending_ids: list[int] = []
    for i, note in enumerate(pending_notes):
        src, cat, title, body = news.pop()
        photo = i in (0, 2, 3, 6)
        video = i == 5
        cid = await candidate(src, cat, title, body, "pending", age_h=0.2 + i * 0.35, media_kind="video" if video else ("photo" if photo else None),
                              media_state="ready" if photo else ("failed" if video else "none"),
                              media_error="too_big" if video else None, **note)
        pending_ids.append(cid)
        if photo:
            out_media.append(cid)
    # the same story from another source (shown as "also in")
    for i, original in enumerate(pending_ids[:3]):
        row = (await conn.execute(select(ContentCandidate.title, ContentCandidate.body).where(ContentCandidate.id == original))).one()
        other = "roz" if i != 0 else "sport"
        await candidate(other, "other", row.title, row.body, "duplicate", age_h=0.15 + i * 0.3, duplicate_of=original,
                        qc_at=None, qc_tries=0, qc_backend=None, qc_model=None, qc_category=None)
    # rejected by the supervisor or the owner
    rejects = [
        ("click", "entertainment", "😱 باورتان نمیشود این بازیگر چه گفت!", "جزئیات بیشتر را در کانال ما ببینید. همین حالا عضو شوید و برای دوستانتان بفرستید.",
         ["تیتر فریبنده است و خبری در متن نیست", "دعوت به عضویت در کانال دیگر دارد"], ["clickbait", "ads_scam"], ["signature"]),
        ("click", "other", "💰 با این روش در یک هفته درآمد دلاری داشته باشید", "فقط با یک گوشی و روزی یک ساعت. برای دریافت آموزش به شناسه‌ی زیر پیام بدهید.",
         ["تبلیغ درآمدزایی بی‌پشتوانه است"], ["ads_scam"], ["mention"]),
        ("click", "entertainment", "🔥 ویدیوی جنجالی که همه دنبالش هستند", "ویدیو را پیش از پاک شدن ببینید.", ["تیتر فریبنده و بدون محتواست"], ["clickbait", "low_quality"], []),
        ("roz", "incident", "🚨 تصادف زنجیره‌ای در آزادراه", "در پی مه‌گرفتگی، ۱۱ خودرو در آزادراه با هم برخورد کردند. تصاویر صحنه در ادامه آمده است.",
         ["تصویر صحنه دلخراش است"], ["graphic_violence"], []),
        ("sport", "sport", "⚽️ نتایج زنده را در کانال ما دنبال کنید", "برای دیدن نتایج زنده‌ی همه‌ی بازی‌ها به کانال زیر بروید.", ["تبلیغ کانال دیگر است، خبر نیست"], ["ads_scam"], ["signature", "link"]),
        ("click", "other", "📣 تخفیف ویژه‌ی فروشگاه فقط امروز", "کد تخفیف ۵۰ درصدی فقط تا امشب اعتبار دارد.", ["آگهی تجاری است"], ["ads_scam"], ["link"]),
        ("tech", "science_tech", "📱 مقایسه‌ی دو گوشی پرچمدار", "The full review is available on our website with benchmark tables and camera samples.", ["متن بیشتر انگلیسی است"], ["low_quality"], ["not_persian"]),
        ("roz", "politics", "🗳 بیانیه‌ی تازه درباره‌ی انتخابات شوراها", "متن کامل بیانیه در ادامه آمده است.", [], [], ["category_not_allowed"]),
    ]
    for i, (src, cat, title, body, reasons, flags, checks) in enumerate(rejects):
        age = 26.5 + i * 1.7 if src == "click" else 2 + i * 2.6  # the switched-off source gave nothing since
        await candidate(src, cat, title, body, "rejected", age_h=age, qc_decision="reject", qc_reasons=reasons, qc_flags=flags,
                        qc_checks=checks, decided_by="owner" if i == 3 else "qc", reviewed_at=ago(hours=age - 0.2))
    # the rest of that source's last twenty items: thirteen more rejected, three that passed
    junk = [
        ("😳 عکسی که همه درباره‌اش حرف میزنند", "برای دیدن عکس کامل وارد کانال شوید."),
        ("🎁 قرعه‌کشی بزرگ؛ همین حالا شرکت کنید", "فقط کافی است این پیام را برای ده نفر بفرستید."),
        ("⚠️ اگر این برنامه را دارید فوراً پاکش کنید", "جزئیات در ادامه؛ پیش از هر کاری کانال را دنبال کنید."),
        ("🤑 راز پولدار شدن در سی روز", "آموزش کامل فقط در کانال ویژه."),
        ("😱 ترفندی که هیچ‌کس به شما نمیگوید", "تا پاک نشده ببینید."),
    ]
    for i in range(13):
        title, body = junk[i % len(junk)]
        age = 30 + i * 3.3
        await candidate("click", "entertainment", title, body, "rejected", age_h=age, qc_decision="reject", decided_by="qc",
                        qc_reasons=["تیتر فریبنده است و خبری در متن نیست"], qc_flags=["clickbait"] if i % 3 else ["clickbait", "ads_scam"],
                        qc_checks=["signature"] if i % 2 else [], reviewed_at=ago(hours=age - 0.2))
    for i in range(3):
        await candidate("click", "lifestyle", "🍵 سه نوشیدنی گرم برای عصرهای پاییز", "دمنوش به، چای زنجبیلی و شیر و دارچین؛ هر سه را میشود در ده دقیقه آماده کرد.",
                        "expired", age_h=58 + i * 9, qc_decision="approve", decided_by="qc", qc_reasons=["متن ساده و بی‌حاشیه است"],
                        reviewed_at=ago(hours=57.5 + i * 9))
    # too old before anyone decided
    for i in range(5):
        src, cat, title, body = news.pop()
        await candidate(src, cat, title, body, "expired", age_h=52 + i * 6, qc_decision=None, qc_error="bridge_timeout" if i % 2 else None,
                        qc_at=None if i % 2 == 0 else ago(hours=51 + i * 6), qc_tries=i % 2)
    # the pictures the panel keeps for the queue and for approved items
    for cid in out_media:
        data = demo_png(480, 270, cid)
        await conn.execute(
            insert(ContentMedia).values(candidate_id=cid, role="main", mime="image/png", size=len(data), width=480, height=270,
                                        sha256=hashlib.sha256(data).hexdigest(), data=data, created_at=ago(minutes=30))
        )
    # the supervisor: calls to Claude, health of the bridge
    calls = []
    for i in range(37):
        at = ago(minutes=9 + i * 37)
        failed = i in (4, 11, 23)
        calls.append({"at": at, "backend": "bridge" if i % 9 else "api", "outcome": "bridge_timeout" if failed else "ok",
                      "reached": not failed or i == 11, "candidate_id": None, "duration_ms": RND.randint(6000, 41000),
                      "model": "sonnet" if i % 9 else "claude-sonnet-5-5", "input_tokens": RND.randint(1800, 5200),
                      "output_tokens": RND.randint(180, 520), "cost_usd": None if i % 9 else round(RND.uniform(0.01, 0.03), 4)})
    await conn.execute(insert(ContentQcCall), calls)
    await qc_settings.save_state(
        conn,
        {
            "health": {"ok": True, "calls_24h": 31, "daily_cap": 80, "protocol": 2, "busy": False, "logged_in": True,
                       "version_ok": True, "claude_version": "2.1.38", "last_outcome": "ok",
                       "rate_limit": {"status": "allowed", "type": "five_hour", "utilization": 0.37}, "at": iso(ago(minutes=4))},
            "last_ok_at": iso(ago(minutes=9)), "last_outcome": "ok", "last_outcome_at": iso(ago(minutes=9)),
            "slot": f"{NOW.astimezone(TEHRAN).date().isoformat()}:8", "slot_calls": 3, "alerts": {},
        },
    )
    return {"items": item_ids[:9], "candidates": used_ids}


def demo_png(width: int, height: int, seed: int) -> bytes:
    """A small placeholder picture, drawn here (sky, sun, two rows of hills): no real photo."""
    import math

    rnd = random.Random(seed * 7919)  # noqa: S311
    sky_top = (rnd.randrange(30, 110), rnd.randrange(90, 160), rnd.randrange(160, 225))
    sky_low = (rnd.randrange(215, 250), rnd.randrange(180, 225), rnd.randrange(130, 200))
    far = (rnd.randrange(60, 110), rnd.randrange(110, 150), rnd.randrange(90, 130))
    near = bytes(max(0, c - 40) for c in far)
    far_b, sun = bytes(far), bytes((253, 238, 178))
    sun_x, sun_y, sun_r = rnd.randrange(width // 5, width * 4 // 5), rnd.randrange(height // 6, height // 2), rnd.randrange(18, 34)
    p1, p2 = rnd.uniform(0, 6.28), rnd.uniform(0, 6.28)
    ridge_far = [int(height * 0.62 + math.sin(x / width * 8.2 + p1) * height * 0.07) for x in range(width)]
    ridge_near = [int(height * 0.78 + math.sin(x / width * 13.0 + p2) * height * 0.05) for x in range(width)]
    rows = bytearray()
    for y in range(height):
        t = y / (height - 1)
        row = bytearray(bytes(int(sky_top[i] + (sky_low[i] - sky_top[i]) * t) for i in range(3)) * width)
        dy = y - sun_y
        if abs(dy) <= sun_r:
            half = int((sun_r * sun_r - dy * dy) ** 0.5)
            lo, hi = max(0, sun_x - half), min(width, sun_x + half + 1)
            row[lo * 3 : hi * 3] = sun * (hi - lo)
        if y >= min(ridge_far):
            for x in range(width):
                if y >= ridge_near[x]:
                    row[x * 3 : x * 3 + 3] = near
                elif y >= ridge_far[x]:
                    row[x * 3 : x * 3 + 3] = far_b
        rows += b"\x00" + row

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


# --- posts in the channels -----------------------------------------------------------------------------------------------


async def seed_posts(conn: AsyncConnection, configs: dict[str, list[int]], content: dict[str, list[int]]) -> None:
    cfg = posting_settings.PostingConfig(main_enabled=True)
    quiet = (await tg_store.posting_settings(conn)).quiet_windows
    today = NOW.astimezone(TEHRAN).date()
    slots: list[tuple[str, Any]] = []
    for back in (5, 4):  # before the main channel was turned on: two days of posts in the test channel
        day = today - timedelta(days=back)
        slots += [("test", s) for s in plan.day_plan(day, count=4, start=cfg.start, end=cfg.end, quiet=quiet)]
    for back in (3, 2, 1, 0):
        day = today - timedelta(days=back)
        slots += [("main", s) for s in plan.day_plan(day, count=cfg.posts_per_day, start=cfg.start, end=cfg.end, quiet=quiet)]
    slots = [(role, s) for role, s in slots if s.at <= NOW]
    items, cands = list(content["items"]), list(content["candidates"])
    # the news candidates went out with every other one of the latest posts (the newest is still queued)
    main_posts = [i for i, (role, _) in enumerate(slots) if role == "main"]
    with_news = dict(zip(main_posts[-(2 * len(cands) + 1) : -1 : 2], cands, strict=False))
    news_media = {
        r.id: r.media_kind
        for r in (await conn.execute(select(ContentCandidate.id, ContentCandidate.media_kind))).all()
    }
    message = {"main": 211, "test": 57}
    ended_configs = {  # config id -> (status, hours ago it ended): what a post shows about its Reality link
        r.id: (r.status, r.ended_at)
        for r in (await conn.execute(select(RealityConfig.id, RealityConfig.status, RealityConfig.ended_at))).all()
    }
    all_configs = sorted(ended_configs)
    born = {r.id: r.created_at for r in (await conn.execute(select(RealityConfig.id, RealityConfig.created_at))).all()}
    views: list[dict[str, Any]] = []
    last = len(slots) - 1
    for i, (role, slot) in enumerate(slots):
        at = slot.at + timedelta(seconds=RND.randint(3, 40))
        age_h = (NOW - at).total_seconds() / 3600
        queued = i == last and age_h < 3  # the newest post is still in the outbox
        failed = role == "main" and i == last - 13
        poster = "operator" if role == "main" and i % 11 != 4 else "bot"
        candidate_id = with_news.get(i)
        item_id = None if candidate_id is not None else items[i % len(items)]
        text_html = f"<b>demo post {i}</b>"
        outbox_id = (
            await conn.execute(
                insert(Outbox)
                .values(kind="operator_send_post" if poster == "operator" else "send_post",
                        payload={"role": role, "text": text_html, "post_kind": "proxy"}, not_before=slot.at, attempts=1,
                        last_error="chat_write_forbidden" if failed else None, created_at=slot.at,
                        done_at=None if queued else at)
                .returning(Outbox.id)
            )
        ).scalar_one()
        message_id = None
        if not queued and not failed:
            message[role] += RND.randint(1, 2)
            message_id = message[role]
            await conn.execute(
                insert(ChannelPost).values(
                    role=role, chat_id=MAIN_CHAT if role == "main" else TEST_CHAT, message_id=message_id, kind="proxy",
                    text_sha256=tg_store.text_sha256(text_html), outbox_id=outbox_id, created_at=at,
                    last_edited_at=at + timedelta(hours=9) if age_h > 12 else None,
                )
            )
        # the links the post carried: six MTProto words and one Reality config
        serving = [n for n in NODES if n.get("agent") and not n.get("test") and (ready_at(n) or NOW) < at and (deleted_at(n) or NOW) > at - timedelta(hours=1)]
        serving = serving[i % 3 :] + serving[: i % 3]
        links: list[dict[str, Any]] = []
        for n in serving:
            for user, ordinal in mt_users(n):
                if len(links) >= cfg.max_mtproto:
                    break
                gone = deleted_at(n)
                if gone is not None and gone > at:
                    status, status_at = "expired", gone
                elif n["state"] == "burned":
                    status, status_at = "burned", ago(minutes=25)
                else:
                    status, status_at = "active", None
                links.append({"kind": "mtproto", "key": proxy_key(node_id(n["key"]), user), "slot": n["slot"],
                              "label": label_for(n["slot"], False, ordinal), "expires_at": None, "status": status,
                              "status_at": status_at, "operator": [None, "mci", "rightel", "irancell", None, "mci"][len(links) % 6]})
        usable = [c for c in all_configs if born[c] <= at]
        if usable:
            cid = max(usable, key=lambda c: born[c]) if i % 4 else usable[max(0, len(usable) - 2)]
            status, ended = ended_configs[cid]
            links.append({"kind": "reality", "key": f"reality:{cid}", "slot": None, "label": config_label(cid),
                          "expires_at": born[cid] + timedelta(hours=48), "status": status, "status_at": ended, "operator": None})
        changes = sum(1 for x in links if x["status"] != "active")
        gone_post = role == "main" and i == last - 21
        post_id = (
            await conn.execute(
                insert(Post)
                .values(
                    role=role, outbox_id=outbox_id, content_item_id=item_id, content_html="", channel_tag=MAIN_USERNAME,
                    poster=poster, plan_day=slot.day, plan_slot=slot.index, planned_at=slot.at, created_at=at,
                    last_edit_at=at + timedelta(hours=min(age_h * 0.8, 30)) if changes and message_id else None,
                    edits=min(changes, 3) if message_id else 0, edit_failures=5 if role == "main" and i == last - 17 else 0,
                    shown_sha256=tg_store.text_sha256(text_html), gone_at=at + timedelta(hours=5) if gone_post else None,
                    content_candidate_id=candidate_id,
                    media_kind=news_media.get(candidate_id) if candidate_id is not None else None,
                    breaking=False,
                )
                .returning(Post.id)
            )
        ).scalar_one()
        await conn.execute(insert(PostLink), [{"post_id": post_id, "position": p, "missing_since": None, **x} for p, x in enumerate(links)])
        if candidate_id is not None:  # the candidate's own times follow the post it went out with
            await conn.execute(
                update(ContentCandidate)
                .where(ContentCandidate.id == candidate_id)
                .values(published_at=at - timedelta(minutes=95), created_at=at - timedelta(minutes=90), qc_at=at - timedelta(minutes=48),
                        reviewed_at=at - timedelta(minutes=48), used_at=at, updated_at=at)
            )
        if message_id is not None and role == "main":
            reach = int(52 * (1 - 2.718 ** (-age_h / 9))) + RND.randint(3, 11)
            views.append({"role": role, "chat_id": MAIN_CHAT, "message_id": message_id, "views": reach,
                          "forwards": max(0, reach // 11 + RND.randint(-2, 2)), "reactions": max(0, reach // 14 + RND.randint(-1, 2)),
                          "at": ago(minutes=70)})
    await conn.execute(insert(PostViews), views)
    # two more jobs in the outbox: an alert on its way and one that failed for good
    await conn.execute(
        insert(Outbox),
        [
            {"kind": "alert", "payload": {"text": "demo alert"}, "not_before": ago(seconds=20), "attempts": 0,
             "last_error": None, "created_at": ago(seconds=20), "done_at": None},
            {"kind": "edit_post", "payload": {"role": "main", "text": "demo edit"}, "not_before": ago(hours=30), "attempts": 5,
             "last_error": "message_not_found", "created_at": ago(hours=30), "done_at": ago(hours=29)},
        ],
    )
    await posting_settings.save_state(conn, {"last_plan_day": today.isoformat()})


# --- statistics: traffic and load of the servers, the channel, where members came from ---------------------------------


async def seed_analytics(conn: AsyncConnection, configs: dict[str, list[int]]) -> None:
    month_start = NOW.replace(day=1, hour=0, minute=0, second=0)
    rows: list[dict[str, Any]] = []
    for n in NODES:
        if n["state"] == "booting":
            continue
        nid, start = node_id(n["key"]), ready_at(n)
        assert start is not None
        end = deleted_at(n) or NOW
        included = INCLUDED[n["loc"]]
        cores = 2
        times: list[tuple[datetime, int]] = []
        at = start.replace(minute=0, second=0) + timedelta(hours=1)
        dense_from = NOW - timedelta(hours=48)
        while at < min(end, dense_from):
            times.append((at, 3600))
            at += timedelta(hours=1)
        at = max(at, dense_from.replace(minute=0, second=0))
        newest = NOW - timedelta(minutes=4)
        while at <= min(end, newest):
            times.append((at, 900))
            at += timedelta(minutes=15)
        if end == NOW and n["state"] != "unhealthy":
            times.append((newest.replace(second=0), 900))
        out_total = in_total = 0
        period: tuple[int, int] | None = None
        seen: set[datetime] = set()
        for at, window in times:
            if at in seen:
                continue
            seen.add(at)
            if period != (at.year, at.month):  # Hetzner's counters start again with the billing month
                period, out_total, in_total = (at.year, at.month), 0, 0
            load = day_curve(at) * RND.uniform(0.8, 1.2)
            out_delta = int(n["gb_day"] * GB / 86400 * window * load)
            in_delta = int(out_delta * 0.19)
            out_total += out_delta
            in_total += in_delta
            cpu = min(97.0, 9 + 21 * load * RND.uniform(0.7, 1.3))
            if n.get("hot") and NOW - at < timedelta(hours=2):
                cpu = RND.uniform(82, 93)
            rows.append(
                {"node_id": nid, "account_id": uid(f"account:{n['acct']}"), "hetzner_id": hetzner_id(n), "node_name": node_name(n),
                 "server_type": n["type"], "location": n["loc"], "at": at, "out_bytes": out_total, "in_bytes": in_total,
                 "included_bytes": included, "out_delta_bytes": out_delta, "in_delta_bytes": in_delta,
                 "traffic_price_per_tb_eur": Decimal("7.40") if n["loc"] == "sin" else Decimal("1.00"),
                 "cpu_cores": cores, "cpu_avg": round(cpu, 1), "cpu_max": round(min(100.0, cpu * RND.uniform(1.1, 1.6)), 1),
                 "net_out_bps_avg": out_delta * 8 / window, "net_in_bps_avg": in_delta * 8 / window, "window_s": window}
            )
    # earlier months, from servers that are long gone: the six-month chart
    month = month_start
    for back, tb in enumerate((1.9, 1.35, 0.9, 0.52, 0.21), start=1):
        month = (month - timedelta(days=1)).replace(day=1)
        ghost = uid(f"ghost:{back}")
        days = ((month + timedelta(days=32)).replace(day=1) - month).days
        for d in range(days):
            at = month + timedelta(days=d, hours=12)
            if at > NOW - timedelta(days=22):
                continue
            delta = int(tb * TB / days * RND.uniform(0.8, 1.2))
            rows.append(
                {"node_id": ghost, "account_id": uid("account:c"), "hetzner_id": 58_000_000 + back, "node_name": f"{FLEET_NODE_PREFIX}{back}-{ghost.hex[:8]}",
                 "server_type": "cpx02", "location": "fsn1", "at": at, "out_bytes": delta * (d + 1), "in_bytes": delta * (d + 1) // 5,
                 "included_bytes": 20 * TB, "out_delta_bytes": delta, "in_delta_bytes": delta // 5, "traffic_price_per_tb_eur": Decimal("1.00"),
                 "cpu_cores": 2, "cpu_avg": None, "cpu_max": None, "net_out_bps_avg": delta * 8 / 86400, "net_in_bps_avg": delta * 8 / 86400 / 5,
                 "window_s": 86400}
            )
    for i in range(0, len(rows), 1000):
        await conn.execute(insert(NodeSample), rows[i : i + 1000])
    # the sin node is close to its included traffic (Hetzner's own counter of the billing period)
    await conn.execute(
        text(
            "UPDATE analytics_node_samples SET out_bytes = :v WHERE node_id = :id AND at = "
            "(SELECT max(at) FROM analytics_node_samples WHERE node_id = :id)"
        ),
        {"v": int(INCLUDED["sin"] * 0.83), "id": node_id("s5")},
    )

    # the channel: 3 members a month ago, 47 now
    links = [
        ("اینستاگرام", "لینک بیوی صفحه‌ی اینستاگرام", "Instagram", 28),
        ("گروه دوستان", "فرستاده‌شده در گروه‌های دوستان", "Friends", 21),
        ("بنر سایت", "بنر پایین سایت آموزش", "SiteBanner", 12),
    ]
    link_ids: dict[str, int] = {}
    for name, purpose, tg_name, age in links:
        link_ids[name] = (
            await conn.execute(
                insert(TrackingLink)
                .values(chat_id=MAIN_CHAT, name=name, purpose=purpose, invite_link=f"https://t.me/+Demo{tg_name}Link0",
                        telegram_name=tg_name, created_by="admin:owner", created_at=ago(days=age))
                .returning(TrackingLink.id)
            )
        ).scalar_one()
    plan_sources = (
        [("link", "اینستاگرام")] * 22 + [("link", "گروه دوستان")] * 10 + [("link", "بنر سایت")] * 6
        + [("plain", "لینک اصلی کانال")] * 9 + [("folder", None)] * 4 + [("other", None)] * 12
    )
    RND.shuffle(plan_sources)
    since = {name: ago(days=age) for name, _, _, age in links}
    # more joins in the recent days (the curve bends upward): about four in the last day
    times = sorted(NOW - timedelta(days=29.6 * (1 - RND.random() ** 0.5), minutes=25) for _ in plan_sources)
    joins: list[tuple[datetime, int, str, str | None]] = []
    for at, (kind, name) in zip(times, plan_sources, strict=True):
        if kind == "link" and at < since[name or ""]:
            kind, name = "other", None  # before that tracking link existed
        joins.append((at, 6_100_000_000 + RND.randint(0, 899_999_999), kind, name))
    events: list[dict[str, Any]] = []
    old_enough = [i for i, j in enumerate(joins) if j[0] < ago(days=4)]
    leavers = set(RND.sample(old_enough, min(18, len(old_enough))))
    moves: list[tuple[datetime, int]] = []
    for i, (at, user, kind, name) in enumerate(joins):
        base = {"role": "main", "chat_id": MAIN_CHAT, "telegram_user_id": user,
                "source": "link" if kind in ("link", "plain") else kind, "link_name": name,
                "tracking_link_id": link_ids[name] if kind == "link" and name else None}
        events.append({**base, "at": at, "kind": "join"})
        moves.append((at, 1))
        if i in leavers:
            left = at + timedelta(hours=RND.uniform(3, 60))
            events.append({**base, "at": left, "kind": "leave"})
            moves.append((left, -1))
    last_join = max(i for i, j in enumerate(joins) if j[0] < ago(hours=30) and i not in leavers)
    at, user, kind, name = joins[last_join]  # one member left this afternoon
    events.append({"role": "main", "chat_id": MAIN_CHAT, "telegram_user_id": user, "source": "link" if kind in ("link", "plain") else kind,
                   "link_name": name, "tracking_link_id": link_ids[name] if kind == "link" and name else None,
                   "at": ago(hours=5, minutes=12), "kind": "leave"})
    moves.append((ago(hours=5, minutes=12), -1))
    await conn.execute(insert(MemberEvent), events)
    moves.sort()
    net = sum(m for _, m in moves)
    count_at_start = 47 - net
    samples = []
    hour = NOW.replace(minute=0, second=0)
    for h in range(30 * 24, -1, -1):
        at = hour - timedelta(hours=h)
        samples.append({"role": "main", "chat_id": MAIN_CHAT, "at": at, "member_count": count_at_start + sum(m for t, m in moves if t <= at)})
        if h % 6 == 0:
            samples.append({"role": "test", "chat_id": TEST_CHAT, "at": at, "member_count": 2 if h > 200 else 3})
    if ago(minutes=6) > hour:
        samples.append({"role": "main", "chat_id": MAIN_CHAT, "at": ago(minutes=6), "member_count": 47})
    await conn.execute(insert(ChannelSample), samples)

    # Telegram's own channel statistics, as the operator account read them
    day0 = datetime.combine(NOW.date(), datetime.min.time(), tzinfo=UTC)
    xs = [int((day0 - timedelta(days=d)).timestamp() * 1000) for d in range(13, -1, -1)]
    members_by_day = [count_at_start + sum(m for t, m in moves if t <= day0 - timedelta(days=d - 1)) for d in range(13, -1, -1)]

    def series(key: str, name: str, values: list[float], kind: str = "line") -> dict[str, Any]:
        return {"key": key, "name": name, "type": kind, "values": values}

    def wave(lo: int, hi: int) -> list[float]:
        return [float(RND.randint(lo, hi)) for _ in xs]

    graphs = {
        "followers": {"x": xs, "series": [series("y0", "Total", [float(v) for v in members_by_day])]},
        "growth": {"x": xs, "series": [series("y0", "Joined", wave(1, 6)), series("y1", "Left", wave(0, 2))]},
        "new_followers_by_source": {"x": xs, "stacked": True, "series": [series("y0", "URL", wave(0, 4), "bar"), series("y1", "Search", wave(0, 2), "bar"), series("y2", "Groups", wave(0, 2), "bar"), series("y3", "Other", wave(0, 1), "bar")]},
        "views_by_source": {"x": xs, "stacked": True, "series": [series("y0", "Followers", wave(140, 420), "bar"), series("y1", "URL", wave(20, 130), "bar"), series("y2", "Groups", wave(30, 210), "bar"), series("y3", "Other", wave(5, 60), "bar")]},
        "languages": {"x": xs, "percentage": True, "stacked": True, "series": [series("y0", "Persian", wave(34, 41), "area"), series("y1", "English", wave(4, 8), "area"), series("y2", "Other", wave(0, 2), "area")]},
        "interactions": {"x": xs, "series": [series("y0", "Views", wave(210, 640)), series("y1", "Shares", wave(6, 41))]},
    }
    await conn.execute(
        insert(ChannelStatsSnapshot).values(
            role="main", chat_id=MAIN_CHAT, at=ago(minutes=70), status="ok", participants=47, period_start=ago(days=7), period_end=NOW,
            followers=47.0, followers_prev=33.0, views_per_post=41.6, views_per_post_prev=27.3, shares_per_post=3.4,
            shares_per_post_prev=1.9, reactions_per_post=2.8, reactions_per_post_prev=1.2, notifications_pct=72.3, graphs=graphs,
        )
    )
    await conn.execute(
        insert(ChannelStatsSnapshot).values(role="test", chat_id=TEST_CHAT, at=ago(minutes=70), status="stats_unavailable", participants=3, graphs={})
    )

    # proxy users (D39): counters the agents send, never a person
    hourly: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    dims: list[dict[str, Any]] = []
    regions = [("Tehran", 0.31), ("Isfahan", 0.11), ("Khorasan Razavi", 0.1), ("Fars", 0.09), ("East Azerbaijan", 0.07),
               ("Khuzestan", 0.06), ("Mazandaran", 0.05), ("Alborz", 0.05), ("Gilan", 0.04), ("Kerman", 0.03)]
    asns = [("AS197207", 0.34), ("AS44244", 0.29), ("AS57218", 0.08), ("AS58224", 0.13), ("AS31549", 0.05), ("AS16322", 0.04)]
    for n in NODES:
        if n.get("agent") != "0.5.0" or n.get("test"):
            continue
        nid = node_id(n["key"])
        boot = hex32(f"boot:{n['key']}")[:16]
        first = ready_at(n) or NOW
        for back in range(13, -1, -1):
            day = (NOW - timedelta(days=back)).date()
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
            if day_start + timedelta(days=1) < first:
                continue
            growth = 0.45 + 0.55 * (13 - back) / 13
            users = int((14 + n["gb_day"] * 0.9) * growth * RND.uniform(0.9, 1.1))
            received = min(NOW, day_start + timedelta(days=1))
            daily.append({"node_id": nid, "boot_id": boot, "day": day, "unique_users": users, "p50_mb": round(RND.uniform(55, 95), 1),
                          "p90_mb": round(RND.uniform(380, 620), 1), "p99_mb": round(RND.uniform(1900, 3400), 1), "received_at": received})
            total = int(n["gb_day"] * GB * growth)

            def bucket(dim: str, key: str, share: float) -> None:
                count = int(users * share)
                if key != "other" and count < 3:
                    return
                dims.append({"node_id": nid, "boot_id": boot, "day": day, "dim": dim, "key": key, "users": count,
                             "bytes": int(total * share), "received_at": received})  # noqa: B023

            bucket("country", "IR", 0.86)
            bucket("country", "TR", 0.06)
            bucket("country", "DE", 0.05)
            bucket("country", "other", 0.03)
            for key, share in regions:
                bucket("region", key, share * 0.86)
            bucket("region", "other", 0.16)
            for key, share in asns:
                bucket("asn", key, share)
            bucket("asn", "other", 0.07)
            for h in range(24):
                at = day_start + timedelta(hours=h)
                if at < first or at > NOW:
                    continue
                load = day_curve(at) * RND.uniform(0.85, 1.15) * growth
                refs = [("mtproto", name, 0.42 * (0.75 if o == 0 else 0.25)) for name, o in mt_users(n)]
                for cid, share in zip(configs.get(n["key"], [])[-2:], (0.36, 0.22), strict=False):
                    refs.append(("reality", f"cfg_{cid}", share))
                for kind, ref, share in refs:
                    down = int(n["gb_day"] * GB / 24 * load * share)
                    hourly.append({"node_id": nid, "boot_id": boot, "day": day, "hour": h, "kind": kind, "ref": ref,
                                   "bytes_up": int(down * 0.12), "bytes_down": down, "conns_max": max(1, int(n.get("conns", 20) * load * share * 1.6)),
                                   "received_at": min(NOW, at + timedelta(hours=1))})
    for model, data in ((UsageDay, daily), (UsageDim, dims), (UsageHour, hourly)):
        for i in range(0, len(data), 1000):
            await conn.execute(insert(model), data[i : i + 1000])
    await put(
        conn,
        "analytics.status",
        {
            "nodes": {"at": iso(ago(minutes=4)), "nodes": 8, "sampled": 7, "errors": None},
            "channels": {"at": iso(ago(minutes=6)), "sampled": 2, "errors": None},
            "channel_stats": {"at": iso(ago(minutes=70)), "outcome": "ok"},
        },
    )


# --- events (actions_log): what the panel and the owner did in the last two weeks ----------------------------------


async def seed_events(conn: AsyncConnection) -> None:
    rows: list[dict[str, Any]] = []

    def log(at: datetime, actor: str, action: str, target: str | None, *reasons: str, allowed: bool = True, dry: bool = False, result: str | None = None) -> None:
        rows.append({"at": at, "actor": actor, "action": action, "target": target, "dry_run": dry, "allowed": allowed,
                     "reasons": {"reasons": list(reasons), "context": {"demo": True}}, "result": result})

    owner = "admin:owner"
    # a month ago: the first setup (older than the two weeks the charts show)
    log(ago(days=34), owner, "account_add", "Hetzner-Nodes-07", "verified", result="account verified: 0 servers")
    log(ago(days=34), "account_guard", "account_active", "Hetzner-Nodes-07", "verified")
    log(ago(days=33, hours=5), owner, "settings_update", "fleet", "budget_cap_eur", "node_cap_total", result="2 fleet setting(s) changed")
    log(ago(days=33, hours=4), owner, "cloudflare_token_set", "cloudflare", result="token verified, 5 zones")
    log(ago(days=33, hours=3), owner, "telegram.bot_set", f"@{BOT_USERNAME}", "getme_ok")
    log(ago(days=33, hours=3), owner, "telegram.pairing_created", None, "valid_minutes:10")
    log(ago(days=33, hours=3), "telegram", "telegram.owner_paired", None)
    log(ago(days=33, hours=2), owner, "telegram.channel_set", MAIN_USERNAME, result="main")
    log(ago(days=33, hours=2), owner, "telegram.channel_set", TEST_USERNAME, result="test")
    log(ago(days=33, hours=1), owner, "telegram.settings_set", "posting")
    log(ago(days=27, hours=1), owner, "mtproxy.account_code_sent", "+44•••••23", "code_via:app")
    log(ago(days=27), owner, "mtproxy.account_connected", "+44•••••23", "mtproxybot_ok")
    log(ago(days=22), owner, "kill_switch_off", "fleet", "owner_request", result="kill switch OFF")
    log(ago(days=20), "operator_poster", "posts.own_pack_created", "ipoxxo", result="28 emoji")
    log(ago(days=20), owner, "posts.operator_set", "posts.operator", "poster_operator")
    for i, src in enumerate(SOURCES):
        log(ago(days=18 - i * 2, hours=4), owner, "content.source_added", src["label"])
    # the last two weeks
    log(ago(days=13, hours=20), "account_guard", "account_locked", "Hetzner-Nodes-05", "token_rejected", allowed=False)
    log(ago(days=12, hours=5), owner, "account_add", "Hetzner-Nodes-08", "verified", result="account verified: 0 servers")
    log(ago(days=12, hours=5), "account_guard", "account_active", "Hetzner-Nodes-08", "verified")
    log(ago(days=12, hours=2), owner, "account_retired", "Hetzner-Nodes-06", "retired_by_owner")
    log(ago(days=11, hours=23), "pricing", "pricing_change", "cpx11@sin", result="5.99 -> 6.99 EUR/month")
    for n in NODES:
        made, name = created_at(n), node_name(n)
        fleet_owned = not n.get("test")
        log(made, "fleet" if fleet_owned else owner, "node_request" if fleet_owned else "test_node_request",
            f"slot-{n['slot']}" if fleet_owned else name, result=f"{n['type']} in {n['loc']}")
        log(made + timedelta(seconds=44), "node_executor", "node_create", name, result=f"server created in {n['loc']}")
        if n["state"] == "booting":
            continue
        ready = ready_at(n) or NOW
        log(ready, "node_executor", "node_ready", name)
        if fleet_owned and n["key"] in ("s3", "s6"):
            log(ready + timedelta(minutes=9), "sponsor", "sponsor.registered", f"{slot_host(n['slot'])}:{MT_PORT}")
        gone = deleted_at(n)
        if gone is not None:
            if n["why"] == "burned":
                log(gone - timedelta(minutes=40), "probes", "probe_verdict", name, "verdict_blocked", allowed=False, result="blocked")
                log(gone - timedelta(minutes=40), "fleet", "node_burned", name, "verdict_blocked")
            log(gone - timedelta(minutes=2), "fleet" if fleet_owned else owner, "node_retire" if fleet_owned else "node_retire_request", name,
                n["why"] if n["why"] != "owner" else "owner_retire")
            log(gone, "node_executor", "node_delete", name, result="server deleted")
    log(ago(days=5, hours=1), owner, "settings_update", "fleet", "locations", result="1 fleet setting(s) changed")
    log(ago(days=8, hours=2), "fleet", "slot_unhealthy", "slot-2", "slot_unhealthy")
    # the domain pool
    log(ago(days=10), "domains", "domain_verdict", "zagros-cdn.example", "filtered", allowed=False)
    log(ago(days=10), "domains", "cloudflare_pool_change", "zagros-cdn.example", "domain_filtered", result="active -> filtered")
    log(ago(days=9.9), "domains", "cloudflare_pool_change", ZONE_ACTIVE, "replaces_filtered", "auto_promote", result="standby -> active")
    log(ago(days=9.9), "dns", "dns_create", f"slot1.{ZONE_ACTIVE}", result="8 slot names on the new active domain")
    log(ago(days=9.9), "acme", "acme_certificate_issued", f"*.{ZONE_ACTIVE}", result="valid 90 days")
    log(ago(days=8.9), "domains", "cloudflare_zone_delete", "zagros-cdn.example", "deleted")
    log(ago(days=6), "domains", "cloudflare_pool_change", "mahtab-net.example", "auto_new_zone", result="none -> standby")
    log(ago(days=4.9), "dns", "dns_update", slot_host(3), result=f"A {node('s3')['ip']}")
    log(ago(days=5.9), "dns", "dns_update", slot_host(6), result=f"A {node('s6')['ip']}")
    log(ago(hours=9, minutes=50), "domains", "domain_verdict", "kavir-proxy.example", "filtered", allowed=False)
    log(ago(hours=9, minutes=49), "domains", "cloudflare_pool_change", "kavir-proxy.example", "domain_filtered", result="standby -> filtered")
    # posts and the second account
    log(ago(days=5, hours=3), owner, "telegram.test_post", TEST_USERNAME)
    log(ago(days=3, hours=6), owner, "posts.settings_set", "posting", "main_enabled")
    log(ago(days=2, hours=7), "operator_poster", "posts.poster_fallback", MAIN_USERNAME, "flood_wait", allowed=False, result="sent by the bot instead")
    log(ago(hours=31), "publisher", "posts.message_gone", f"{MAIN_USERNAME}/228")
    log(ago(hours=19), "publisher", "posts.edit_given_up", f"{MAIN_USERNAME}/236", allowed=False)
    log(ago(minutes=42), "publisher", "posts.queued", MAIN_USERNAME, result="6 MTProto + 1 Reality")
    # the content supervisor
    log(ago(days=1, hours=1), "content_qc", "content.source_auto_disabled", "داغ‌ترین‌ها", result="17 of 20 rejected")
    log(ago(days=1, hours=1), "content_qc", "content.owner_alert", "داغ‌ترین‌ها")
    log(ago(hours=4), "content_qc", "content.qc_approved", "خبر روز")
    log(ago(hours=1, minutes=5), "content_qc", "content.qc_approved", "ورزش‌نما")
    log(ago(hours=2, minutes=5), "content_qc", "content.qc_rejected", "خبر روز", allowed=False)
    # sponsor and @MTProxybot
    log(ago(days=12), owner, "sponsor.campaign_created", "nikan_store_demo")
    log(ago(days=9), "sponsor", "sponsor.campaign_active", "nikan_store_demo")
    log(ago(days=7), "sponsor", "sponsor.campaign_finished", "nikan_store_demo")
    log(ago(days=6), owner, "sponsor.campaign_cancelled", "ravi_cafe_demo")
    log(ago(days=1, hours=4), owner, "sponsor.campaign_created", "parseh_academy_demo")
    log(ago(hours=5, minutes=12), "sponsor", "sponsor.flow_stuck", f"{slot_host(7)}:{MT_PORT}", "channel_refused", allowed=False)
    log(ago(hours=3, minutes=8), "sponsor", "sponsor.mtproxybot_list_read", "/myproxies", result="12 proxies on 2 pages")
    log(ago(hours=3, minutes=2), "sponsor", "sponsor.mtproxybot_observed", f"{node('h3')['ip']}:{MT_PORT}", "remove_button_found")
    log(ago(minutes=34), "sponsor", "sponsor.promotion_set", f"{slot_host(3)}:{MT_PORT}", result="@ipoxxo")
    log(ago(minutes=22), "sponsor", "sponsor.flow_failed", f"{slot_host(6)}:{MT_PORT}", "no_reply", allowed=False)
    # the owner's own visits
    log(ago(days=7, hours=3), "auth", "login_ok", "owner", "method:totp")
    log(ago(days=3, hours=7), "auth", "login_ok", "owner", "method:totp")
    log(ago(hours=6), "auth", "login_failed", "owner", "method:totp", allowed=False)
    log(ago(hours=6), "auth", "login_ok", "owner", "method:totp")
    # today: a full account, a filtered address and what followed
    log(ago(hours=2, minutes=10), "fleet", "node_request", "slot-9", "account_server_limit_reached", allowed=False)
    log(ago(hours=2, minutes=5), "fleet", "fleet_alert", "account_server_limit_reached")
    log(ago(hours=1, minutes=40), owner, "probe_run_requested", node_name(node("s5")))
    log(ago(hours=1, minutes=38), "probes", "probe_verdict", node_name(node("s5")), result="suspect")
    log(ago(minutes=26), "probes", "probe_verdict", node_name(node("s8")), "verdict_blocked", allowed=False, result="blocked")
    log(ago(minutes=26), "fleet", "node_burned", node_name(node("s8")), "verdict_blocked")
    log(ago(minutes=24), "fleet", "fleet_alert", "burned_ip_streak")
    log(ago(minutes=7), "fleet", "slot_unhealthy", "slot-7", "slot_unhealthy")
    # the daily routine of the loops, so that no day of the two weeks is empty
    routine = [
        ("publisher", "posts.queued", MAIN_USERNAME, "6 MTProto + 1 Reality"),
        ("content_qc", "content.qc_approved", "خبر روز", None),
        ("reality", "reality_config_created", "slot:2", "48 h / 1000 GB"),
        ("reality", "reality_config_ended", "slot:4", "expired"),
        ("content_qc", "content.qc_approved", "دیده‌بان فناوری", None),
        ("publisher", "posts.queued", TEST_USERNAME, "6 MTProto + 1 Reality"),
    ]
    for back in range(1, 14):
        day = (NOW - timedelta(days=back)).astimezone(TEHRAN).date()
        have = sum(1 for r in rows if r["at"].astimezone(TEHRAN).date() == day)
        noon = datetime.combine(day, datetime.min.time(), tzinfo=TEHRAN) + timedelta(hours=12)
        for k in range(have, 3):
            actor, action, target, result = routine[(back + k) % len(routine)]
            log(noon + timedelta(hours=RND.uniform(-3, 9)), actor, action, target, result=result)
    rows.sort(key=lambda r: r["at"])
    await conn.execute(insert(ActionLog), rows)


# --- running ---------------------------------------------------------------------------------------------------------------


async def ensure_throwaway(conn: AsyncConnection) -> None:
    name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    if name != EXPECTED_DATABASE:
        raise SystemExit(f"seed_demo: refused. Connected to another database than '{EXPECTED_DATABASE}'.")


async def seed() -> None:
    vault = Vault.from_config()
    engine = get_engine()
    async with engine.begin() as conn:
        await ensure_throwaway(conn)
        await wipe(conn)
        await seed_fleet(conn, vault)
        await seed_cloudflare(conn, vault)
        await seed_agents(conn)
        await seed_telegram(conn, vault)
        await seed_sponsor(conn)
        configs = await seed_reality(conn, vault)
        await seed_probes(conn)
        content = await seed_content(conn)
        await seed_posts(conn, configs, content)
        await seed_analytics(conn, configs)
        await seed_events(conn)
        counts = {
            t: (await conn.execute(text(f'SELECT count(*) FROM "{t}"'))).scalar_one()  # noqa: S608 - metadata names
            for t in ("nodes", "posts", "content_candidates", "analytics_node_samples", "actions_log")
        }
    await engine.dispose()
    print("seed_demo: demo data written to ph_shots: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


async def only_wipe() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await ensure_throwaway(conn)
        await wipe(conn)
    await engine.dispose()
    print("seed_demo: demo data removed from ph_shots")


async def beat() -> None:
    """Keep what a live panel refreshes every minute fresh, until stopped (Ctrl-C or SIGTERM)."""
    engine = get_engine()
    print("seed_demo: keeping the worker heartbeat fresh in ph_shots; stop with Ctrl-C")
    try:
        while True:
            async with engine.begin() as conn:
                await ensure_throwaway(conn)
                await conn.execute(text("UPDATE settings SET updated_at = now() WHERE key = 'worker.heartbeat'"))
                await conn.execute(
                    text(
                        "UPDATE agent_nodes a SET last_seen_at = now() - interval '20 seconds', "
                        "last_report_at = now() - interval '20 seconds' FROM nodes n "
                        "WHERE n.id = a.node_id AND n.state IN ('ready', 'burned')"
                    )
                )
            await asyncio.sleep(20)
    finally:
        await engine.dispose()


def main() -> None:
    if make_url(get_config().async_database_url()).database != EXPECTED_DATABASE:
        raise SystemExit(f"seed_demo: refused. The panel's configured database is not '{EXPECTED_DATABASE}'.")
    args = sys.argv[1:]
    if args == ["--beat"]:
        try:
            asyncio.run(beat())
        except KeyboardInterrupt:
            pass
    elif args == ["--wipe"]:
        asyncio.run(only_wipe())
    elif not args:
        asyncio.run(seed())
    else:
        raise SystemExit("usage: seed_demo.py [--beat | --wipe]")


if __name__ == "__main__":
    main()
