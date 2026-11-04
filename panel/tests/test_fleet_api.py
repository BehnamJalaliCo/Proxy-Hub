"""Fleet/accounts/settings API over real HTTP (ASGI transport), real PostgreSQL and Redis.

``require_admin`` is overridden with an AdminIdentity (the auth team's real implementation is
merged separately); one test checks that every route is protected without the override.
Account creation that needs a real Hetzner token is in tests/test_fleet_hetzner_real.py.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_fleet_support import ADMIN, CSRF, clean_fleet_tables, live_offers, make_vault, new_token

from proxyhub.accounts import service
from proxyhub.accounts.hetzner_io import Verification, summarize_servers
from proxyhub.api.deps import require_admin
from proxyhub.api.routes_accounts import get_vault
from proxyhub.reconcilers.base import Heartbeat
from proxyhub.reconcilers.fleet import FleetReconciler
from proxyhub.vault import Vault

pytestmark = [pytest.mark.db, pytest.mark.redis]

FLEET_PATHS = {
    ("GET", "/api/accounts"),
    ("POST", "/api/accounts"),
    ("POST", "/api/accounts/{account_id}/drain"),
    ("POST", "/api/accounts/{account_id}/retire"),
    ("GET", "/api/settings/fleet"),
    ("PUT", "/api/settings/fleet"),
    ("POST", "/api/kill-switch"),
    ("GET", "/api/overview"),
    ("GET", "/api/fleet/plan"),
    ("GET", "/api/events"),
}


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_fleet_tables(eng)
    yield eng
    await clean_fleet_tables(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    try:
        async with lifespan(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://panel.test", headers=CSRF) as c:
                yield c
    finally:
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(get_vault, None)


async def add_account(engine: AsyncEngine, vault: Vault, name: str) -> str:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        acc = await service.persist_verified_account(
            session,
            vault,
            name=name,
            token=service.clean_token(new_token()),
            result=Verification(servers=summarize_servers([]), access="read_write", offers=live_offers()),
            actor="admin:owner",
            now=datetime.now(UTC),
        )
    return str(acc["id"])


# --- protection and CSRF ----------------------------------------------------------------------


async def test_every_fleet_route_requires_admin(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_accounts, routes_overview, routes_settings
    from proxyhub.api.app import app, lifespan

    found = set()
    for module in (routes_accounts, routes_overview, routes_settings):
        for route in module.router.routes:
            assert isinstance(route, APIRoute)
            for method in route.methods:
                found.add((method, route.path))
                calls = {d.call for d in route.dependant.dependencies}
                assert require_admin in calls, f"{method} {route.path} is not protected"
    assert found == FLEET_PATHS
    published = app.openapi()["paths"]
    assert all(method.lower() in published[path] for method, path in FLEET_PATHS)

    app.dependency_overrides.pop(require_admin, None)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            some_id = uuid.uuid4()
            for method, path in sorted(FLEET_PATHS):
                r = await c.request(method, path.replace("{account_id}", str(some_id)), json={})
                assert r.status_code in (401, 403, 503), (method, path, r.status_code)


async def test_csrf_header_required_for_writes(client: AsyncClient) -> None:
    for method, path, body in (
        ("POST", "/api/kill-switch", {"on": True}),
        ("PUT", "/api/settings/fleet", {}),
        ("POST", "/api/accounts", {"name": "x", "token": "y"}),
        ("POST", f"/api/accounts/{uuid.uuid4()}/drain", None),
    ):
        r = await client.request(method, path, json=body, headers={"X-Requested-With": "other"})
        assert r.status_code == 403 and r.json()["error"] == "csrf", path
        assert r.json()["message_fa"]
    assert (await client.get("/api/settings/fleet", headers={"X-Requested-With": ""})).status_code == 200


# --- settings and kill switch -----------------------------------------------------------------


async def test_settings_defaults_have_no_caps(client: AsyncClient) -> None:
    r = await client.get("/api/settings/fleet")
    assert r.status_code == 200
    assert r.json() == {
        "kill_switch": True,
        "dry_run": True,
        "budget_cap_eur": None,
        "node_cap_total": None,
        "creations_per_hour_cap": 3,
        "target_slots": 9,
        "allowed_server_types": ["cpx02"],
        "locations": ["fsn1", "nbg1", "hel1"],
    }


async def test_settings_update_validates_and_logs(client: AsyncClient, engine: AsyncEngine) -> None:
    body = {
        "kill_switch": False,
        "dry_run": True,
        "budget_cap_eur": "150.5",
        "node_cap_total": 10,
        "creations_per_hour_cap": 2,
        "target_slots": 9,
        "allowed_server_types": ["CPX02", "cpx12", "cpx02"],
        "locations": ["fsn1", "hel1"],
    }
    r = await client.put("/api/settings/fleet", json=body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["kill_switch"] is True  # only POST /api/kill-switch changes it
    assert saved["budget_cap_eur"] == "150.50" and isinstance(saved["budget_cap_eur"], str)
    assert saved["allowed_server_types"] == ["cpx02", "cpx12"]
    assert (await client.get("/api/settings/fleet")).json() == saved
    async with engine.connect() as conn:
        changed = (
            await conn.execute(text("SELECT reasons FROM actions_log WHERE action = 'settings_update'"))
        ).scalar_one()
    assert {"budget_cap_eur", "locations"} <= set(changed["reasons"])
    assert "kill_switch" not in changed["reasons"]

    for bad in (
        {**body, "budget_cap_eur": "1.234"},
        {**body, "budget_cap_eur": "-5"},
        {**body, "budget_cap_eur": "Infinity"},
        {**body, "node_cap_total": -1},
        {**body, "locations": []},
        {**body, "locations": ["mars9"]},
        {**body, "allowed_server_types": ["cpx02; drop"]},
        {**body, "surprise": 1},
        ["not", "an", "object"],
    ):
        r = await client.put("/api/settings/fleet", json=bad)
        assert r.status_code == 422, bad
        payload = r.json()
        assert payload["error"] == "invalid_settings" and payload["message_fa"] and payload["fields"]
        assert "input" not in str(payload["fields"])
    assert (await client.get("/api/settings/fleet")).json() == saved  # nothing changed

    r = await client.put("/api/settings/fleet", json={**body, "dry_run": False})
    assert r.status_code == 422 and r.json()["error"] == "live_mode_locked"


async def test_kill_switch(client: AsyncClient, engine: AsyncEngine) -> None:
    r = await client.post("/api/kill-switch", json={"on": False})
    assert r.status_code == 200 and r.json() == {"kill_switch": False}
    assert (await client.get("/api/settings/fleet")).json()["kill_switch"] is False
    assert (await client.get("/api/overview")).json()["kill_switch"] is False
    r = await client.post("/api/kill-switch", json={"on": True})
    assert r.json() == {"kill_switch": True}
    assert (await client.get("/api/settings/fleet")).json()["budget_cap_eur"] is None  # nothing else changed
    r = await client.post("/api/kill-switch", json={"on": "yes"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_input"
    async with engine.connect() as conn:
        actions = (
            await conn.execute(
                text("SELECT action, actor FROM actions_log WHERE target = 'fleet' ORDER BY id")
            )
        ).all()
    assert [tuple(a) for a in actions] == [
        ("kill_switch_off", "admin:owner"),
        ("kill_switch_on", "admin:owner"),
    ]


async def test_kill_switch_on_works_even_with_corrupt_settings(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("""INSERT INTO settings (key, value) VALUES ('fleet', '{"kill_switch": "nope"}')""")
        )
    r = await client.get("/api/settings/fleet")
    assert r.status_code == 500 and r.json()["error"] == "settings_corrupt"
    assert (await client.post("/api/kill-switch", json={"on": False})).status_code == 500
    r = await client.post("/api/kill-switch", json={"on": True})
    assert r.status_code == 200 and r.json() == {"kill_switch": True}
    assert (await client.get("/api/settings/fleet")).json()["kill_switch"] is True


# --- overview, plan, events -------------------------------------------------------------------


async def test_overview_shape_and_numbers(client: AsyncClient, engine: AsyncEngine, vault: Vault) -> None:
    r = await client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "kill_switch",
        "dry_run",
        "budget_cap_eur",
        "node_cap_total",
        "target_slots",
        "nodes_live",
        "month_projected_eur",
        "accounts",
        "last_fleet_run_at",
        "worker_heartbeat_age_s",
    }
    assert body["dry_run"] is True and body["kill_switch"] is True
    assert body["nodes_live"] == 0 and body["month_projected_eur"] == "0.00"
    assert body["accounts"] == {"active": 0, "locked": 0, "total": 0}
    assert body["last_fleet_run_at"] is None

    account_id = await add_account(engine, vault, "overview-acc")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nodes (id, account_id, name, location, server_type, state, is_test, created_at) "
                "VALUES (gen_random_uuid(), :a, 'n1', 'fsn1', 'cpx02', 'ready', false, :c)"
            ),
            {"a": account_id, "c": datetime.now(UTC) - timedelta(days=40)},
        )
    assert await FleetReconciler().try_run_once(engine)
    assert await Heartbeat().try_run_once(engine)
    body = (await client.get("/api/overview")).json()
    assert body["nodes_live"] == 1
    assert body["month_projected_eur"] == "6.49"  # a whole month of CPX02 at the live price
    assert body["accounts"] == {"active": 1, "locked": 0, "total": 1}
    assert body["last_fleet_run_at"].endswith("Z")
    assert isinstance(body["worker_heartbeat_age_s"], int | float) and body["worker_heartbeat_age_s"] < 60


async def test_plan_and_events(client: AsyncClient, engine: AsyncEngine) -> None:
    assert (await client.get("/api/fleet/plan")).json() == {
        "generated_at": None,
        "dry_run": True,
        "actions": [],
    }
    assert await FleetReconciler().try_run_once(engine)
    plan = (await client.get("/api/fleet/plan")).json()
    assert plan["dry_run"] is True and plan["generated_at"].endswith("Z")
    assert len(plan["actions"]) == 9
    for a in plan["actions"]:
        assert set(a) == {"action", "target", "allowed", "reasons"}
        assert a["action"] == "create" and a["allowed"] is False and "kill_switch_on" in a["reasons"]

    events = (await client.get("/api/events")).json()
    assert len(events) == 9
    assert set(events[0]) == {
        "id",
        "at",
        "actor",
        "action",
        "target",
        "dry_run",
        "allowed",
        "reasons",
        "result",
    }
    ids = [e["id"] for e in events]
    assert ids == sorted(ids, reverse=True)
    assert all(
        e["dry_run"] is True and e["actor"] == "fleet" and e["action"] == "node_create" for e in events
    )
    assert all(isinstance(e["reasons"], list) and e["result"].startswith("dry-run:") for e in events)

    page1 = (await client.get("/api/events", params={"limit": 4})).json()
    page2 = (await client.get("/api/events", params={"limit": 4, "before_id": page1[-1]["id"]})).json()
    assert [e["id"] for e in page1 + page2] == ids[:8]
    for bad in ({"limit": 0}, {"limit": 501}, {"before_id": "x"}):
        r = await client.get("/api/events", params=bad)
        assert r.status_code == 422 and r.json()["error"] == "invalid_input"


# --- accounts ---------------------------------------------------------------------------------


async def test_accounts_list_drain_retire(client: AsyncClient, engine: AsyncEngine, vault: Vault) -> None:
    assert (await client.get("/api/accounts")).json() == []
    account_id = await add_account(engine, vault, "acc-api")
    listed = (await client.get("/api/accounts")).json()
    assert len(listed) == 1
    acc = listed[0]
    assert set(acc) == {
        "id",
        "name",
        "state",
        "state_reason",
        "servers_in_account",
        "learned_server_limit",
        "created_at",
    }
    assert acc["id"] == account_id and acc["state"] == "active" and acc["servers_in_account"] == 0

    r = await client.post(f"/api/accounts/{account_id}/retire")
    assert r.status_code == 409 and r.json()["error"] == "illegal_transition"
    r = await client.post(f"/api/accounts/{account_id}/drain")
    assert r.status_code == 200 and r.json()["state"] == "draining"
    r = await client.post(f"/api/accounts/{account_id}/retire")
    assert r.status_code == 200 and r.json()["state"] == "retired"
    r = await client.post(f"/api/accounts/{uuid.uuid4()}/drain")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = await client.post("/api/accounts/not-a-uuid/drain")
    assert r.status_code == 422 and r.json()["error"] == "invalid_input"
    overview = (await client.get("/api/overview")).json()
    assert overview["accounts"] == {"active": 0, "locked": 0, "total": 1}


async def test_add_account_rejects_bad_input_before_calling_hetzner(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    token = new_token()
    await add_account(engine, vault, "taken-name")
    cases = [
        ({"name": "fine", "token": "short"}, 400, "token_invalid"),
        ({"name": "fine", "token": token + " " + token}, 400, "token_invalid"),
        ({"name": "Taken-Name", "token": token}, 400, "name_taken"),
        ({"name": "<bad>", "token": token}, 400, "invalid_name"),
        ({"name": 5, "token": token}, 422, "invalid_input"),
        ({"name": "x", "token": token, "extra": token}, 422, "invalid_input"),
        ({"token": token}, 422, "invalid_input"),
    ]
    for body, status, code in cases:
        r = await client.post("/api/accounts", json=body)
        assert r.status_code == status and r.json()["error"] == code, (body, r.text)
        assert token not in r.text  # never echoed back (rule 3)
        assert r.json()["message_fa"]
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM hetzner_accounts"))).scalar_one()
    assert count == 1


async def test_add_account_refused_when_vault_key_missing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from proxyhub.api.app import app
    from proxyhub.config import get_config

    app.dependency_overrides.pop(get_vault, None)
    monkeypatch.setenv("PROXYHUB_MASTER_KEY_FILE", str(tmp_path / "missing"))
    get_config.cache_clear()
    try:
        r = await client.post("/api/accounts", json={"name": "no-vault", "token": new_token()})
    finally:
        monkeypatch.delenv("PROXYHUB_MASTER_KEY_FILE")
        get_config.cache_clear()
    assert r.status_code == 503 and r.json()["error"] == "vault_unavailable"
