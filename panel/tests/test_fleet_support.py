"""Shared data and helpers for the fleet team's tests.

No imitation of Hetzner or Telegram anywhere (decision D10). ``SERVER_TYPES_JSON`` is data in the
exact shape of ``GET /v1/server_types`` (docs.hetzner.cloud); it is turned into offers by the REAL
hcloud library objects and our real parser, exactly as a live response would be. Prices: CPX02 and
CPX12 monthly prices from hetzner.com (2026-11-03, docs/10 H1); the hourly prices of the other
types and the gross values are derived (monthly / 624, net x 1.19) and only illustrative. On the
panel server, tests/test_fleet_hetzner_real.py checks this shape against the live API.
"""

from __future__ import annotations

import copy
import os
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Any

from hcloud import Client
from hcloud.server_types import BoundServerType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.accounts.hetzner_io import LiveOffer, offers_from_server_types
from proxyhub.api.deps import AdminIdentity
from proxyhub.safety.gates import ServerView
from proxyhub.vault import Vault

ADMIN = AdminIdentity(user_id="test-admin-id", username="owner", session_id="test-session")
CSRF = {"X-Requested-With": "proxyhub"}
TB20 = 20 * 1024**4
LOCATIONS = ("fsn1", "nbg1", "hel1")


def _price(location: str, hourly: str, monthly: str) -> dict[str, Any]:
    def money(net: str) -> dict[str, str]:
        return {"net": f"{Decimal(net):.10f}", "gross": f"{Decimal(net) * Decimal('1.19'):.16f}"}

    return {
        "location": location,
        "price_hourly": money(hourly),
        "price_monthly": money(monthly),
        "included_traffic": TB20,
        "price_per_tb_traffic": money("1"),
    }


def _locations(available: bool, deprecation: dict[str, str] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "id": i + 1,
            "name": loc,
            "deprecation": deprecation,
            "available": available,
            "recommended": available,
        }
        for i, loc in enumerate(LOCATIONS)
    ]


_GONE = {"announced": "2025-10-16T00:00:00+00:00", "unavailable_after": "2026-01-01T00:00:00+00:00"}


def _server_type(
    id_: int,
    name: str,
    hourly: str,
    monthly: str,
    *,
    available: bool = True,
    deprecation: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "name": name,
        "description": name.upper(),
        "category": "regular_purpose",
        "cores": 1,
        "memory": 1.0,
        "disk": 20,
        "deprecated": deprecation is not None,
        "deprecation": deprecation,
        "prices": [_price(loc, hourly, monthly) for loc in LOCATIONS],
        "storage_type": "local",
        "cpu_type": "shared",
        "architecture": "x86",
        "locations": _locations(available, deprecation),
    }


SERVER_TYPES_JSON: dict[str, Any] = {
    "server_types": [
        # Cheapest on paper, but "not available" on hetzner.com since 2026 (H1): must never be chosen.
        _server_type(201, "cx23", "0.0056", "3.49", available=False),
        # Not orderable in Europe since 2026-01-01 (H2): deprecated per location.
        _server_type(202, "cpx11", "0.0080", "4.99", deprecation=_GONE),
        _server_type(203, "cpx02", "0.0104", "6.49"),
        _server_type(204, "cpx12", "0.0192", "11.99"),
    ],
    "meta": {"pagination": {"page": 1, "per_page": 50, "total_entries": 4}},
}


def live_offers(payload: dict[str, Any] | None = None) -> list[LiveOffer]:
    """Hetzner-shaped JSON -> hcloud's own BoundServerType objects -> our parser. No network."""
    client = Client(token="parse-only-never-sent")
    data = copy.deepcopy(payload or SERVER_TYPES_JSON)
    return offers_from_server_types(BoundServerType(client.server_types, st) for st in data["server_types"])


def server(
    hetzner_id: int, *, managed: bool = True, role: str = "node", protected: bool = False
) -> ServerView:
    labels = {"managed-by": "proxy-hub", "role": role} if managed else {"owner": "someone-else"}
    return ServerView(
        hetzner_id=hetzner_id, name=f"srv-{hetzner_id}", labels=labels, delete_protected=protected
    )


def new_token() -> str:
    """A token-shaped random value, generated at run time (never a literal in the repo)."""
    return secrets.token_hex(32)


def write_master_key(directory: Path) -> Path:
    path = directory / "master_key"
    path.write_text(secrets.token_hex(32) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def make_vault(directory: Path) -> Vault:
    return Vault.from_key_file(write_master_key(directory))


def db_url() -> str:
    url = os.environ.get("PROXYHUB_DATABASE_URL")
    assert url, "PROXYHUB_DATABASE_URL must be set for db tests"
    return url


async def clean_fleet_tables(engine: AsyncEngine) -> None:
    """Remove rows this team's tests create (shared session database)."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM node_events"))
        await conn.execute(text("DELETE FROM cost_ledger"))
        await conn.execute(text("DELETE FROM nodes"))
        await conn.execute(text("DELETE FROM hetzner_accounts"))  # observations/servers/offers cascade
        await conn.execute(text("DELETE FROM fleet_plans"))
        await conn.execute(text("DELETE FROM secrets WHERE name LIKE 'hcloud:%' OR name LIKE 'test:%'"))
        await conn.execute(text("DELETE FROM settings WHERE key = 'fleet'"))
        await conn.execute(text("DELETE FROM actions_log"))


async def assert_absent_from_database(engine: AsyncEngine, needle: str) -> None:
    """Rule 3: the plaintext (or its hex form) appears in no row of any table."""
    hexed = needle.encode().hex()
    async with engine.connect() as conn:
        tables = (
            await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        ).scalars()
        for table in list(tables):
            rows = (await conn.execute(text(f'SELECT CAST(t AS text) FROM "{table}" t'))).scalars()  # noqa: S608
            for row in rows:
                assert needle not in row, f"plaintext secret found in table {table}"
                assert hexed not in row, f"hex-encoded secret found in table {table}"


def test_fixture_parses_through_real_hcloud_objects() -> None:
    offers = {(o.server_type, o.location): o for o in live_offers()}
    assert len(offers) == 4 * len(LOCATIONS)
    cpx02 = offers[("cpx02", "fsn1")]
    assert str(cpx02.price.monthly) == "6.4900000000" and str(cpx02.price.hourly) == "0.0104000000"
    assert cpx02.orderable and cpx02.included_traffic_bytes == TB20
    assert not offers[("cx23", "nbg1")].orderable and offers[("cx23", "nbg1")].available is False
    assert offers[("cpx11", "hel1")].deprecated and not offers[("cpx11", "hel1")].orderable
