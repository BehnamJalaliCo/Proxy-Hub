"""Shared helpers for the live team's tests (no tests in this module).

Everything is real: PostgreSQL, the panel app (ASGI or uvicorn), the Go agent, Caddy in Docker,
the cloud-init that Ubuntu 24.04 ships. Nothing imitates Hetzner (decision D10): tests that need
Hetzner are marked ``hetzner``; the others prove that Hetzner is never reached at all, with a
reader factory that fails the test if it is ever called.
"""

from __future__ import annotations

import os
import secrets
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_agent_support import REPO, run_tool
from test_fleet_support import ADMIN, CSRF, make_vault

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_nodes import get_reader_factory, get_vault_factory
from proxyhub.fleet.settings_store import save_fleet_settings
from proxyhub.hetzner.client import HetznerReader
from proxyhub.nodes.executor import NodeExecutor
from proxyhub.nodes.settings import get_live_settings
from proxyhub.settings_schema import FleetSettings
from proxyhub.vault import Vault, hcloud_secret_name

NODE_IMAGE = REPO / "node-image"
DEPLOY = REPO / "deploy"
AGENT_DOMAIN = "agent.proxyhub-test.example"
LOCATIONS = ("fsn1", "nbg1", "hel1")
# CPX02 prices read from the live API on 2026-11-03 (docs/10 finding H1): real shape and values.
CPX02_HOURLY = Decimal("0.0104000000")
CPX02_MONTHLY = Decimal("6.4900000000")

requires_node_image = pytest.mark.skipif(
    not NODE_IMAGE.is_dir(), reason="node-image/ is not in this checkout (panel test container)"
)


def good_settings(**overrides: Any) -> FleetSettings:
    values: dict[str, Any] = {
        "kill_switch": False,
        "dry_run": True,  # the 9-slot fleet stays dry; test nodes do not depend on it
        "budget_cap_eur": Decimal("50.00"),
        "node_cap_total": 3,
        "creations_per_hour_cap": 3,
        "target_slots": 9,
        "allowed_server_types": ["cpx02"],
        "locations": list(LOCATIONS),
    }
    values.update(overrides)
    return FleetSettings.model_validate(values)


class NoHetzner:
    """A reader factory for tests that must never reach Hetzner: any call fails the test."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, token: str) -> HetznerReader:
        self.calls += 1
        raise AssertionError("this test must not reach Hetzner")


async def clean_live_tables(engine: AsyncEngine) -> None:
    """Rows this team's tests create (shared session database)."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM node_events"))
        await conn.execute(text("DELETE FROM cost_ledger"))
        await conn.execute(text("DELETE FROM nodes"))  # node_operations and agent rows cascade
        await conn.execute(text("DELETE FROM hetzner_accounts"))  # observations/servers/offers cascade
        await conn.execute(text("DELETE FROM secrets WHERE name LIKE 'hcloud:%'"))
        await conn.execute(text("DELETE FROM settings WHERE key = 'fleet'"))
        await conn.execute(text("DELETE FROM actions_log"))


@dataclass
class LiveWorld:
    engine: AsyncEngine
    vault: Vault
    tmp: Path
    account_id: uuid.UUID
    token: str
    no_hetzner: NoHetzner

    @property
    def sessions(self) -> async_sessionmaker:  # type: ignore[type-arg]
        return async_sessionmaker(self.engine, expire_on_commit=False)

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []

    async def settings(self, s: FleetSettings) -> None:
        async with self.sessions() as session:
            await save_fleet_settings(session, s)
            await session.commit()

    def executor(self, *, clock: Any = None, reader_factory: Any = None) -> NodeExecutor:
        kwargs: dict[str, Any] = {
            "vault_factory": lambda: self.vault,
            "reader_factory": reader_factory or self.no_hetzner,
        }
        if clock is not None:
            kwargs["clock"] = clock
        return NodeExecutor(**kwargs)

    async def node(self, node_id: uuid.UUID | str) -> dict[str, Any]:
        rows = await self.rows(
            "SELECT n.state, n.hetzner_id, n.ready_at, n.deleted_at, o.last_error, o.retire_reason, "
            "o.create_attempts FROM nodes n LEFT JOIN node_operations o ON o.node_id = n.id WHERE n.id = :n",
            n=uuid.UUID(str(node_id)),
        )
        assert rows, f"node {node_id} not found"
        return dict(rows[0]._mapping)

    async def drop_node(self, node_id: uuid.UUID) -> None:
        await self.rows("DELETE FROM node_events WHERE node_id = :n", n=node_id)
        await self.rows("DELETE FROM nodes WHERE id = :n", n=node_id)

    async def events(self, node_id: uuid.UUID | str) -> list[tuple[str, dict[str, Any]]]:
        rows = await self.rows(
            "SELECT kind, detail FROM node_events WHERE node_id = :n ORDER BY at, id",
            n=uuid.UUID(str(node_id)),
        )
        return [(r.kind, r.detail) for r in rows]

    async def add_node(
        self,
        *,
        state: str,
        is_test: bool = True,
        hetzner_id: int | None = None,
        created_at: datetime | None = None,
        slot: int | None = None,
    ) -> uuid.UUID:
        node_id = uuid.uuid4()
        await self.rows(
            "INSERT INTO nodes (id, account_id, name, slot, location, server_type, state, is_test, "
            "hetzner_id, created_at, updated_at) VALUES (:id, :acc, :name, :slot, 'fsn1', 'cpx02', :state, "
            ":t, :h, :c, :c)",
            id=node_id,
            acc=self.account_id,
            name=f"live-test-{node_id.hex[:8]}",
            slot=slot,
            state=state,
            t=is_test,
            h=hetzner_id,
            c=created_at or datetime.now(UTC),
        )
        return node_id


async def seed_account(
    engine: AsyncEngine,
    vault: Vault,
    *,
    token: str,
    name: str = "live-node-account",
    state: str = "active",
    observed_at: datetime | None = None,
    guard_ok: bool = True,
    servers_total: int = 0,
    offers_at: datetime | None = None,
) -> uuid.UUID:
    """A node account as the round-1 account flow leaves it: token in the vault, a live observation
    and live prices (here written directly, with the real API's values)."""
    account_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state, created_at, updated_at) "
                "VALUES (:id, :name, :secret, :state, :now, :now)"
            ),
            {
                "id": account_id,
                "name": name,
                "secret": hcloud_secret_name(account_id),
                "state": state,
                "now": now,
            },
        )
        seen = observed_at or now
        await conn.execute(
            text(
                "INSERT INTO hetzner_account_observations (account_id, checked_at, ok, guard_ok, "
                "servers_total, managed_servers, foreign_servers, last_ok_at) "
                "VALUES (:id, :at, true, :g, :n, :n, 0, :at)"
            ),
            {"id": account_id, "at": seen, "g": guard_ok, "n": servers_total},
        )
        for loc in LOCATIONS:
            await conn.execute(
                text(
                    "INSERT INTO hetzner_offers (account_id, server_type, location, price_hourly_net, "
                    "price_monthly_net, included_traffic_bytes, deprecated, available, fetched_at) "
                    "VALUES (:id, 'cpx02', :loc, :h, :m, :tb, false, true, :at)"
                ),
                {
                    "id": account_id,
                    "loc": loc,
                    "h": CPX02_HOURLY,
                    "m": CPX02_MONTHLY,
                    "tb": 20 * 1024**4,
                    "at": offers_at or now,
                },
            )
        await vault.put(conn, hcloud_secret_name(account_id), token)
    return account_id


@asynccontextmanager
async def live_world(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    """A clean live world: agent domain configured, one active node account with a fresh guard-green
    observation, live CPX02 prices and its token in the vault. Each test module wraps this in its
    own ``world`` fixture."""
    monkeypatch.setenv("PROXYHUB_AGENT_DOMAIN", AGENT_DOMAIN)
    get_live_settings.cache_clear()
    engine = create_async_engine(migrated_db)
    await clean_live_tables(engine)
    (tmp_path / "vault").mkdir(exist_ok=True)
    vault = make_vault(tmp_path / "vault")
    token = secrets.token_hex(32)  # a Hetzner-token-shaped value made at run time
    account_id = await seed_account(engine, vault, token=token)
    try:
        yield LiveWorld(engine, vault, tmp_path, account_id, token, NoHetzner())
    finally:
        await clean_live_tables(engine)
        await engine.dispose()
        get_live_settings.cache_clear()


@asynccontextmanager
async def api_client(world: LiveWorld, reader_factory: Any = None) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault_factory] = lambda: lambda: world.vault
    app.dependency_overrides[get_reader_factory] = lambda: reader_factory or world.no_hetzner
    try:
        async with lifespan(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://panel.test", headers=CSRF) as c:
                yield c
    finally:
        for dep in (require_admin, get_vault_factory, get_reader_factory):
            app.dependency_overrides.pop(dep, None)


# --- the cloud-init that Ubuntu 24.04 ships ---------------------------------------------------------

# Runs with the system Python: cloud-init is a system package (it is NOT a panel dependency).
CLOUD_INIT_CHECK = r"""
import json, sys
extra = sys.argv[1]
if extra:
    sys.path.insert(0, extra)
import yaml
from cloudinit.config.schema import get_schema, validate_cloudconfig_schema
from cloudinit.config.cc_write_files import canonicalize_extraction, decode_perms, extract_contents
text = open(sys.argv[2], encoding="utf-8").read()
assert text.startswith("#cloud-config\n"), "not a cloud-config document"
config = yaml.safe_load(text)
validate_cloudconfig_schema(config, schema=get_schema(), strict=True)
files = {}
for item in config["write_files"]:
    data = extract_contents(item.get("content", ""), canonicalize_extraction(item.get("encoding")))
    files[item["path"]] = {"mode": decode_perms(item.get("permissions"), 0o644), "owner": item.get("owner"),
                           "hex": data.hex() if isinstance(data, bytes) else data.encode().hex()}
print(json.dumps({"files": files, "runcmd": config.get("runcmd")}))
"""


def cloud_init_python() -> tuple[str, str] | None:
    """(python, extra sys.path) able to import cloud-init: the system's own (panel server, Ubuntu)
    or the Ubuntu 24.04 package unpacked at PROXYHUB_CLOUD_INIT_PYTHONPATH (developer machines)."""
    python = shutil.which("python3") and "/usr/bin/python3"
    if not python or not Path(python).exists():
        return None
    extra = os.environ.get("PROXYHUB_CLOUD_INIT_PYTHONPATH", "")
    probe = run_tool(
        python, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import cloudinit", extra
    )
    return (python, extra) if probe.returncode == 0 else None


@contextmanager
def env(**values: str) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def iso_age(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)
