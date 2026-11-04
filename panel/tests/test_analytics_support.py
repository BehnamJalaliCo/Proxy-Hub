"""Shared data and helpers for the analytics tests (no imitation of Hetzner or Telegram, decision D10).

* ``server_json`` is a server object in the exact shape of ``GET /v1/servers`` from Hetzner's
  published OpenAPI document (docs.hetzner.cloud, fetched 2026-11-02; every field of the schema's
  ``required`` list is present, values are ours). ``metrics_json`` is the shape of
  ``GET /v1/servers/{id}/metrics`` from the same document. Both are turned into objects by the
  REAL hcloud library classes (``BoundServer``, ``Metrics``) and parsed by our real code, exactly as a
  live answer would be. tests/test_analytics_real.py (marker ``hetzner``) checks them live.
* ``chat_member_update`` builds a Bot API ``Update`` with a ``ChatMemberUpdated`` in the shape of
  core.telegram.org/bots/api#chatmemberupdated (with first name, username and language, so the
  tests can prove none of them is stored). ``INVITE_LINK_RESULT`` is a ``ChatInviteLink``.
* Failures are real: a local port nobody listens on (connection refused).
"""

from __future__ import annotations

import copy
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hcloud import Client
from hcloud.metrics import Metrics
from hcloud.servers import BoundServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.hetzner.client import HetznerReader

TB = 1024**4
CHANNEL_MAIN = -1001234567890
CHANNEL_TEST = -1009876543210
STRANGER_CHANNEL = -1005550001111

ANALYTICS_SQL = (
    "DELETE FROM analytics_member_events",
    "DELETE FROM analytics_tracking_links",
    "DELETE FROM analytics_node_samples",
    "DELETE FROM analytics_channel_samples",
    "DELETE FROM analytics_alerts",
    "DELETE FROM analytics_usage_hourly",
    "DELETE FROM analytics_usage_daily",
    "DELETE FROM analytics_usage_dims",
    "DELETE FROM analytics_channel_stats",
    "DELETE FROM analytics_post_views",
    "DELETE FROM analytics_mtproxybot_stats",
    "DELETE FROM settings WHERE key IN ('analytics', 'analytics.status')",
    "DELETE FROM actions_log WHERE action LIKE 'analytics.%'",
)


async def clean_analytics_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for sql in ANALYTICS_SQL:
            await conn.execute(text(sql))


# --- Hetzner shapes -------------------------------------------------------------------------------


def server_json(
    hetzner_id: int,
    *,
    out_bytes: int | None = 3 * TB,
    in_bytes: int | None = TB // 4,
    included: int | None = 20 * TB,
    labels: dict[str, str] | None = None,
    cores: int = 2,
    location: str = "fsn1",
    server_type: str = "cpx02",
) -> dict[str, Any]:
    """One server of ``GET /v1/servers`` (Hetzner OpenAPI ``Server`` schema, all required fields)."""

    def money(net: str) -> dict[str, str]:
        return {"net": net, "gross": f"{Decimal(net) * Decimal('1.19'):.10f}"}

    return {
        "id": hetzner_id,
        "name": f"ph-node-{hetzner_id}",
        "status": "running",
        "created": "2026-11-01T08:00:00Z",
        "public_net": {
            # Our OWN server's address (documentation range, RFC 5737), never a user's.
            "ipv4": {
                "id": 1000 + hetzner_id,
                "ip": "203.0.113.10",
                "blocked": False,
                "dns_ptr": "static.example",
            },
            "ipv6": None,
            "floating_ips": [],
            "firewalls": [],
        },
        "private_net": [],
        "server_type": {
            "id": 203,
            "name": server_type,
            "description": server_type.upper(),
            "cores": cores,
            "memory": 2.0,
            "disk": 40,
            "prices": [
                {
                    "location": location,
                    "price_hourly": money("0.0104000000"),
                    "price_monthly": money("6.4900000000"),
                    "included_traffic": 20 * TB,
                    "price_per_tb_traffic": money("1.0000000000"),
                }
            ],
            "storage_type": "local",
            "cpu_type": "shared",
            "category": "cost_optimized",
            "architecture": "x86",
            "deprecation": None,
            "locations": [
                {"id": 1, "name": location, "deprecation": None, "recommended": True, "available": True}
            ],
        },
        "location": {
            "id": 1,
            "name": location,
            "description": "Falkenstein DC Park 1",
            "country": "DE",
            "city": "Falkenstein",
            "latitude": 50.47612,
            "longitude": 12.370071,
            "network_zone": "eu-central",
        },
        "image": None,
        "iso": None,
        "rescue_enabled": False,
        "locked": False,
        "backup_window": None,
        "outgoing_traffic": out_bytes,
        "ingoing_traffic": in_bytes,
        "included_traffic": included,
        "protection": {"delete": False, "rebuild": False},
        "labels": labels if labels is not None else {"managed-by": "proxy-hub", "role": "node"},
        "volumes": [],
        "load_balancers": [],
        "primary_disk_size": 40,
        "placement_group": None,
    }


def bound_server(data: dict[str, Any]) -> BoundServer:
    """The real hcloud object a live ``servers.get_all()`` returns, from Hetzner-shaped JSON."""
    client = Client(token="parse-only-never-sent")
    return BoundServer(client.servers, copy.deepcopy(data))


def metrics_json(
    cpu: list[float], out_bps: list[float], in_bps: list[float], *, start: int = 1793800800
) -> dict[str, Any]:
    """``GET /v1/servers/{id}/metrics?type=cpu,network`` (values are strings, timestamps seconds)."""

    def series(values: list[float]) -> dict[str, Any]:
        return {"values": [[start + 60 * i, f"{v}"] for i, v in enumerate(values)]}

    return {
        "metrics": {
            "start": "2026-11-04T10:00:00Z",
            "end": "2026-11-04T10:15:00Z",
            "step": 60,
            "time_series": {
                "cpu": series(cpu),
                "network.0.pps.in": series([10.0] * len(in_bps)),
                "network.0.pps.out": series([12.0] * len(out_bps)),
                "network.0.bandwidth.in": series(in_bps),
                "network.0.bandwidth.out": series(out_bps),
            },
        }
    }


def metrics_object(data: dict[str, Any]) -> Metrics:
    """The real hcloud ``Metrics`` object ``servers.get_metrics`` builds from that JSON."""
    return Metrics(**copy.deepcopy(data["metrics"]))


class ClosedPortReader(HetznerReader):
    """The real reader, pointed at a local port nobody listens on (a real connection refusal)."""

    def __init__(self, token: str, port: int) -> None:
        super().__init__(token)
        self._client = Client(
            token=token, api_endpoint=f"http://127.0.0.1:{port}/v1", timeout=(2.0, 2.0), poll_max_retries=1
        )


# --- Telegram shapes ------------------------------------------------------------------------------


def _user(user_id: int, *, first_name: str, username: str | None, is_bot: bool = False) -> dict[str, Any]:
    user: dict[str, Any] = {"id": user_id, "is_bot": is_bot, "first_name": first_name, "language_code": "fa"}
    if username:
        user["username"] = username
    return user


def chat_member_update(
    update_id: int,
    *,
    chat_id: int,
    user_id: int,
    old: str,
    new: str,
    date: int,
    first_name: str = "Member",
    username: str | None = "member_user",
    invite_link: str | None = None,
    link_name: str | None = None,
    via_folder: bool = False,
    is_bot: bool = False,
    new_is_member: bool | None = None,
) -> dict[str, Any]:
    """A Bot API ``Update`` carrying a ``ChatMemberUpdated`` (core.telegram.org/bots/api)."""
    user = _user(user_id, first_name=first_name, username=username, is_bot=is_bot)
    new_member: dict[str, Any] = {"user": user, "status": new}
    if new == "restricted":
        new_member |= {"is_member": bool(new_is_member), "until_date": 0, "can_send_messages": False}
    body: dict[str, Any] = {
        "chat": {"id": chat_id, "title": "IPOXXO | VPN & Proxy", "username": "ipoxxo", "type": "channel"},
        "from": user,
        "date": date,
        "old_chat_member": {"user": user, "status": old},
        "new_chat_member": new_member,
    }
    if invite_link is not None:
        link: dict[str, Any] = {
            "invite_link": invite_link,
            "creator": _user(8123456789, first_name="Proxy-Hub", username="proxyhub_test_bot", is_bot=True),
            "creates_join_request": False,
            "is_primary": False,
            "is_revoked": False,
        }
        if link_name is not None:
            link["name"] = link_name
        body["invite_link"] = link
    if via_folder:
        body["via_chat_folder_invite_link"] = True
    return {"update_id": update_id, "chat_member": body}


INVITE_LINK_RESULT: dict[str, Any] = {
    "invite_link": "https://t.me/+Qz8VbJ3kL1xYzZmNj",
    "creator": {"id": 8123456789, "is_bot": True, "first_name": "Proxy-Hub", "username": "proxyhub_test_bot"},
    "creates_join_request": False,
    "is_primary": False,
    "is_revoked": False,
    "name": "کانال الف - آبان",
}


def random_link() -> str:
    return f"https://t.me/+{secrets.token_urlsafe(12)}"


# --- rows of our own tables -----------------------------------------------------------------------


async def add_account(engine: AsyncEngine, name: str | None = None, state: str = "active") -> uuid.UUID:
    account_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (:id, :name, :secret, :state)"
            ),
            {
                "id": account_id,
                "name": name or f"acc-{account_id.hex[:8]}",
                "secret": f"hcloud:{account_id}",
                "state": state,
            },
        )
    return account_id


async def add_node(
    engine: AsyncEngine,
    account_id: uuid.UUID,
    hetzner_id: int | None,
    *,
    name: str | None = None,
    state: str = "ready",
    created_at: datetime | None = None,
    deleted_at: datetime | None = None,
    server_type: str = "cpx02",
    location: str = "fsn1",
) -> uuid.UUID:
    node_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nodes (id, account_id, hetzner_id, name, location, server_type, state, is_test, "
                "created_at, deleted_at) "
                "VALUES (:id, :acc, :hid, :name, :loc, :type, :state, false, :created, :deleted)"
            ),
            {
                "id": node_id,
                "acc": account_id,
                "hid": hetzner_id,
                "name": name or f"node-{node_id.hex[:6]}",
                "loc": location,
                "type": server_type,
                "state": state,
                "created": created_at or datetime.now(UTC),
                "deleted": deleted_at,
            },
        )
    return node_id


async def add_offer(
    engine: AsyncEngine, account_id: uuid.UUID, *, hourly: str = "0.0104", monthly: str = "6.49"
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_offers "
                "(account_id, server_type, location, price_hourly_net, price_monthly_net, "
                "included_traffic_bytes, deprecated, available, fetched_at) "
                "VALUES (:acc, 'cpx02', 'fsn1', :h, :m, :inc, false, true, now())"
            ),
            {"acc": account_id, "h": Decimal(hourly), "m": Decimal(monthly), "inc": 20 * TB},
        )
