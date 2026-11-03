"""Test-node executor and /api/nodes on the real database, through every gate refusal.

None of these tests reaches Hetzner: every refusal happens before the first Hetzner call, and the
reader factory fails the test if it is ever used (``NoHetzner``). Creating, adopting and deleting a
real server is in test_live_hetzner.py (marked ``hetzner``). Time moves through the executor's
clock; agent heartbeats are written as the agent API writes them.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from test_fleet_support import CSRF
from test_live_support import LiveWorld, api_client, good_settings, live_world

from proxyhub.api.deps import require_admin
from proxyhub.nodes import executor as ex
from proxyhub.nodes.service import evaluate_create

pytestmark = [pytest.mark.db, pytest.mark.redis]

NODE_PATHS = {
    ("GET", "/api/nodes"),
    ("POST", "/api/nodes/test"),
    ("POST", "/api/nodes/{node_id}/retire"),
    ("GET", "/api/nodes/{node_id}/events"),
}


@pytest.fixture
async def world(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    async with live_world(migrated_db, tmp_path, monkeypatch) as w:
        yield w


async def request_node(world: LiveWorld, **body: Any) -> uuid.UUID:
    await world.settings(good_settings())
    async with api_client(world) as c:
        r = await c.post("/api/nodes/test", json=body or None)
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"node_id"}
    return uuid.UUID(r.json()["node_id"])


async def run_executor(world: LiveWorld, **kw: Any) -> None:
    await world.executor(**kw).run_once(world.engine)


# --- request ---------------------------------------------------------------------------------------


async def test_request_records_a_requested_test_node(world: LiveWorld) -> None:
    node_id = await request_node(world)
    node = await world.node(node_id)
    assert node["state"] == "requested" and node["hetzner_id"] is None
    rows = await world.rows(
        "SELECT name, location, server_type, is_test, slot, account_id FROM nodes WHERE id = :n", n=node_id
    )
    name, location, server_type, is_test, slot, account_id = rows[0]
    assert name == f"ph-test-{node_id.hex[:8]}" and location == "fsn1" and server_type == "cpx02"
    assert is_test is True and slot is None and account_id == world.account_id
    events = await world.events(node_id)
    assert events[0][0] == "requested" and events[0][1]["by"] == "admin:owner"
    log = await world.rows("SELECT actor, action, allowed, dry_run FROM actions_log ORDER BY id")
    assert ("admin:owner", "test_node_request", True, False) in [tuple(r) for r in log]
    assert world.no_hetzner.calls == 0


async def test_request_location_is_respected_and_checked(world: LiveWorld) -> None:
    node_id = await request_node(world, location="hel1")
    assert (await world.rows("SELECT location FROM nodes WHERE id = :n", n=node_id))[0][0] == "hel1"
    await world.drop_node(node_id)
    async with api_client(world) as c:
        r = await c.post("/api/nodes/test", json={"location": "ash"})
        assert r.status_code == 409 and "location_not_allowed" in r.json()["reasons"]
        bad = await c.post("/api/nodes/test", json={"location": "fsn1; rm"})
        assert bad.status_code == 422 and bad.json()["error"] == "invalid_request"


async def test_only_one_live_test_node(world: LiveWorld) -> None:
    first = await request_node(world)
    async with api_client(world) as c:
        r = await c.post("/api/nodes/test")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "test_node_exists" and body["message_fa"]
    # Racing requests: exactly one wins (transaction advisory lock).
    await world.drop_node(first)
    async with api_client(world) as c:
        results = await asyncio.gather(*(c.post("/api/nodes/test") for _ in range(5)))
    codes = sorted(r.status_code for r in results)
    assert codes == [202, 409, 409, 409, 409], [r.text for r in results]
    live = await world.rows("SELECT count(*) FROM nodes WHERE is_test AND state <> 'deleted'")
    assert live == [(1,)]


async def test_executor_also_keeps_one_test_node(world: LiveWorld) -> None:
    """Even rows written around the API: the executor closes every later request."""
    await world.settings(good_settings())
    older = await world.add_node(state="booting", created_at=datetime.now(UTC) - timedelta(minutes=1))
    newer = await world.add_node(state="requested")
    await run_executor(world)
    assert (await world.node(newer))["state"] == "deleted"
    assert ("create_refused", {"reasons": ["test_node_exists"]}) in await world.events(newer)
    assert (await world.node(older))["state"] == "booting"
    assert world.no_hetzner.calls == 0


# --- every creation gate refuses, at the API and again in the executor ---------------------------

Setup = Callable[[LiveWorld], Awaitable[None]]


async def _settings(world: LiveWorld, **kw: Any) -> None:
    await world.settings(good_settings(**kw))


async def _sql(world: LiveWorld, sql: str, **params: Any) -> None:
    await world.rows(sql, **params)


async def _node_cap(world: LiveWorld) -> None:
    await _settings(world, node_cap_total=2)
    for _ in range(2):
        await world.add_node(state="ready", is_test=False)


async def _unpriced_live_node(world: LiveWorld) -> None:
    """A live node in a location without a live price: the month projection is incomplete."""
    node_id = await world.add_node(state="ready", is_test=False)
    await world.rows("UPDATE nodes SET location = 'nbg1' WHERE id = :n", n=node_id)
    await world.rows("DELETE FROM hetzner_offers WHERE location = 'nbg1'")


async def _server_limit(world: LiveWorld) -> None:
    await _sql(
        world, "UPDATE hetzner_accounts SET learned_server_limit = 2 WHERE id = :a", a=world.account_id
    )
    await _sql(
        world,
        "UPDATE hetzner_account_observations SET servers_total = 2 WHERE account_id = :a",
        a=world.account_id,
    )


BAD_SETTINGS = '{"bogus": 1}'

REFUSALS: list[tuple[str, Setup]] = [
    ("kill_switch_on", lambda w: _settings(w, kill_switch=True)),
    ("budget_cap_not_set", lambda w: _settings(w, budget_cap_eur=None)),
    ("node_cap_not_set", lambda w: _settings(w, node_cap_total=None)),
    ("node_cap_reached", _node_cap),
    ("budget_cap_reached", lambda w: _settings(w, budget_cap_eur=Decimal("0.01"))),
    ("creation_rate_cap_reached", lambda w: _settings(w, creations_per_hour_cap=0)),
    ("location_not_allowed", lambda w: _settings(w, locations=["nbg1"])),
    ("no_live_price", lambda w: _settings(w, allowed_server_types=["cpx12"])),
    ("projection_incomplete", _unpriced_live_node),
    ("account_server_limit_reached", _server_limit),
    (
        "account_observation_stale",
        lambda w: _sql(
            w,
            "UPDATE hetzner_account_observations SET last_ok_at = now() - interval '2 hours' "
            "WHERE account_id = :a",
            a=w.account_id,
        ),
    ),
    (
        "account_guard_failed",
        lambda w: _sql(
            w,
            "UPDATE hetzner_account_observations SET guard_ok = false WHERE account_id = :a",
            a=w.account_id,
        ),
    ),
    (
        "prices_stale",
        lambda w: _sql(
            w,
            "UPDATE hetzner_offers SET fetched_at = now() - interval '7 hours' WHERE account_id = :a",
            a=w.account_id,
        ),
    ),
    (
        "account_not_usable",
        lambda w: _sql(w, "UPDATE hetzner_accounts SET state = 'locked' WHERE id = :a", a=w.account_id),
    ),
    (
        "settings_invalid",
        lambda w: _sql(
            w, "UPDATE settings SET value = CAST(:v AS jsonb) WHERE key = 'fleet'", v=BAD_SETTINGS
        ),
    ),
]


async def _prepare(world: LiveWorld, reason: str, setup: Setup) -> None:
    await setup(world)


@pytest.mark.parametrize(("reason", "setup"), REFUSALS, ids=[r for r, _ in REFUSALS])
async def test_api_refuses_with_the_gate_reasons(world: LiveWorld, reason: str, setup: Setup) -> None:
    await world.settings(good_settings())
    await _prepare(world, reason, setup)
    async with api_client(world) as c:
        r = await c.post("/api/nodes/test", json={"location": "fsn1"})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["error"] == "gate_refused" and reason in body["reasons"], body
    assert body["message_fa"] and set(body) == {"error", "message_fa", "reasons"}
    assert await world.rows("SELECT count(*) FROM nodes WHERE is_test") == [(0,)]
    log = await world.rows("SELECT allowed, reasons FROM actions_log WHERE action = 'test_node_request'")
    assert len(log) == 1 and log[0][0] is False and reason in log[0][1]["reasons"]
    assert world.no_hetzner.calls == 0


@pytest.mark.parametrize(("reason", "setup"), REFUSALS, ids=[r for r, _ in REFUSALS])
async def test_executor_refuses_again_before_any_hetzner_call(
    world: LiveWorld, reason: str, setup: Setup
) -> None:
    node_id = await request_node(world)  # allowed when requested ...
    await _prepare(world, reason, setup)  # ... but no longer when the executor picks it up
    await run_executor(world)
    node = await world.node(node_id)
    assert node["state"] == "deleted" and node["hetzner_id"] is None and node["create_attempts"] == 0
    refused = [d for k, d in await world.events(node_id) if k == "create_refused"]
    assert refused and reason in refused[0]["reasons"], refused
    log = await world.rows(
        "SELECT allowed, reasons, dry_run FROM actions_log "
        "WHERE actor = 'node_executor' AND action = 'node_create'"
    )
    assert len(log) == 1 and log[0][0] is False and log[0][2] is False and reason in log[0][1]["reasons"]
    assert world.no_hetzner.calls == 0
    tokens = await world.rows("SELECT count(*) FROM agent_registration_tokens WHERE node_id = :n", n=node_id)
    assert tokens == [(0,)]  # no registration token was ever issued


async def test_agent_domain_is_required(world: LiveWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    from proxyhub.nodes.settings import get_live_settings

    node_id = await request_node(world)
    monkeypatch.delenv("PROXYHUB_AGENT_DOMAIN")
    get_live_settings.cache_clear()
    await run_executor(world)
    refused = [d for k, d in await world.events(node_id) if k == "create_refused"]
    assert refused and "agent_domain_not_set" in refused[0]["reasons"]
    async with api_client(world) as c:
        r = await c.post("/api/nodes/test")
        assert r.status_code == 409 and "agent_domain_not_set" in r.json()["reasons"]
    monkeypatch.setenv("PROXYHUB_AGENT_DOMAIN", "10.1.2.3")  # never an IP: it would get no public cert
    get_live_settings.cache_clear()
    async with api_client(world) as c:
        r = await c.post("/api/nodes/test")
        assert r.status_code == 409 and "agent_domain_not_set" in r.json()["reasons"]


async def test_evaluation_counts_and_cost_are_real(world: LiveWorld) -> None:
    await world.settings(good_settings())
    await world.add_node(state="ready", is_test=False)
    async with world.sessions() as s:
        ev = await evaluate_create(s, datetime.now(UTC))
    assert ev.allowed and ev.server_type == "cpx02" and ev.location == "fsn1"
    assert ev.request.fleet_live_total == 1 and ev.request.creations_last_hour == 1
    assert ev.request.account.servers_in_account == 1  # the DB node counts even if Hetzner said 0
    assert Decimal(0) < ev.request.new_server_rest_of_month_eur <= Decimal("6.49")
    assert ev.request.projected_month_eur > 0


# --- lifecycle after the server exists ------------------------------------------------------------


async def agent_row(
    world: LiveWorld, node_id: uuid.UUID, *, at: datetime, report: bool, etag_ok: bool = True
) -> None:
    etag = '"' + "a" * 64 + '"'
    await world.rows(
        "INSERT INTO agent_nodes (node_id, agent_version, telemt_version, registered_at, last_seen_at, "
        "last_served_etag, last_report_at, last_applied_etag, last_apply_ok) VALUES "
        "(:n, '0.1.0', '3.5.16', :at, :at, :etag, :rep, :applied, :ok) ON CONFLICT (node_id) DO UPDATE SET "
        "last_seen_at = :at, last_report_at = :rep, last_applied_etag = :applied, last_apply_ok = :ok",
        n=node_id,
        at=at,
        etag=etag,
        rep=at if report else None,
        applied=etag if etag_ok else '"' + "b" * 64 + '"',
        ok=report,
    )


async def test_booting_registering_ready_and_heartbeat(world: LiveWorld) -> None:
    await world.settings(good_settings())
    t0 = datetime.now(UTC)
    node_id = await world.add_node(state="booting")
    clock = {"now": t0}
    executor = world.executor(clock=lambda: clock["now"])

    await executor.run_once(world.engine)  # nothing registered yet
    assert (await world.node(node_id))["state"] == "booting"

    # The agent registered (the registration hook would have moved it; the executor reconciles).
    await agent_row(world, node_id, at=t0, report=False)
    await executor.run_once(world.engine)
    assert (await world.node(node_id))["state"] == "registering"

    # A report whose applied ETag differs from the one served: not ready yet.
    await agent_row(world, node_id, at=t0 + timedelta(seconds=30), report=True, etag_ok=False)
    clock["now"] = t0 + timedelta(seconds=40)
    await executor.run_once(world.engine)
    assert (await world.node(node_id))["state"] == "registering"

    await agent_row(world, node_id, at=t0 + timedelta(seconds=60), report=True)
    clock["now"] = t0 + timedelta(seconds=70)
    await executor.run_once(world.engine)
    node = await world.node(node_id)
    assert node["state"] == "ready" and node["ready_at"] is not None

    # The panel changed the desired state; the agent applies it with its next poll and report.
    # That lag is no fault: a ready node stays ready while its reports keep working.
    await world.rows(
        "UPDATE agent_nodes SET last_served_etag = :e WHERE node_id = :n", e='"' + "c" * 64 + '"', n=node_id
    )
    await executor.run_once(world.engine)
    assert (await world.node(node_id))["state"] == "ready"
    await world.rows(
        "UPDATE agent_nodes SET last_served_etag = last_applied_etag WHERE node_id = :n", n=node_id
    )

    # Telemt configured for this node: ready needs Telemt up in the latest report.
    await world.rows(
        "INSERT INTO telemt_node_configs (node_id, ports, tls_domain, mode_classic, mode_secure, mode_tls) "
        "VALUES (:n, ARRAY[443], 'cdn.example.com', false, false, true)",
        n=node_id,
    )
    await world.rows(
        "INSERT INTO agent_node_reports (node_id, at, apply_ok, telemt_up, users_reported) "
        "VALUES (:n, :at, true, false, 0)",
        n=node_id,
        at=t0 + timedelta(seconds=60),
    )
    await executor.run_once(world.engine)
    assert (await world.node(node_id))["state"] == "unhealthy"
    await world.rows(
        "INSERT INTO agent_node_reports (node_id, at, apply_ok, telemt_up, users_reported) "
        "VALUES (:n, :at, true, true, 0)",
        n=node_id,
        at=t0 + timedelta(seconds=65),
    )
    await executor.run_once(world.engine)
    assert (await world.node(node_id))["state"] == "ready"

    # No heartbeat for longer than the timeout -> unhealthy; heartbeats back -> ready.
    clock["now"] = t0 + timedelta(seconds=60) + ex.HEARTBEAT_TIMEOUT + timedelta(seconds=1)
    await executor.run_once(world.engine)
    assert (await world.node(node_id))["state"] == "unhealthy"
    await agent_row(world, node_id, at=clock["now"], report=True)
    await executor.run_once(world.engine)
    assert (await world.node(node_id))["state"] == "ready"

    states = [(d["from"], d["to"], d["reason"]) for k, d in await world.events(node_id) if k == "state"]
    assert states == [
        ("booting", "registering", "agent_registered"),
        ("registering", "ready", "agent_reported"),
        ("ready", "unhealthy", "report_not_good"),
        ("unhealthy", "ready", "agent_recovered"),
        ("ready", "unhealthy", "heartbeat_lost"),
        ("unhealthy", "ready", "agent_recovered"),
    ]
    assert world.no_hetzner.calls == 0


async def test_timeouts_mark_unhealthy_and_a_stuck_test_node_is_retired(world: LiveWorld) -> None:
    await world.settings(good_settings())
    t0 = datetime.now(UTC)
    clock = {"now": t0}
    executor = world.executor(clock=lambda: clock["now"])
    booting = await world.add_node(state="booting")
    await executor.run_once(world.engine)  # records state_since = t0
    clock["now"] = t0 + ex.BOOT_TIMEOUT + timedelta(seconds=1)
    await executor.run_once(world.engine)
    assert (await world.node(booting))["state"] == "unhealthy"

    clock["now"] += ex.UNHEALTHY_TEST_RETIRE_AFTER + timedelta(seconds=1)
    await executor.run_once(world.engine)
    node = await world.node(booting)
    # retired, and (no create call was ever made for this row) closed without touching Hetzner
    assert node["retire_reason"] == "unhealthy_test_node"
    await executor.run_once(world.engine)
    assert (await world.node(booting))["state"] == "deleted"
    assert world.no_hetzner.calls == 0

    registering = await world.add_node(state="registering", created_at=clock["now"])
    await agent_row(world, registering, at=clock["now"], report=False)
    await executor.run_once(world.engine)
    clock["now"] += ex.REGISTER_TIMEOUT + timedelta(seconds=1)
    await executor.run_once(world.engine)
    assert (await world.node(registering))["state"] == "unhealthy"
    reasons = [d["reason"] for k, d in await world.events(registering) if k == "state"]
    assert reasons == ["no_good_report"]


async def test_executor_never_touches_fleet_nodes(world: LiveWorld) -> None:
    await world.settings(good_settings())
    fleet = [
        await world.add_node(state=s, is_test=False, slot=1) for s in ("requested", "booting", "retiring")
    ]
    await run_executor(world)
    for node_id in fleet:
        assert await world.events(node_id) == []
    assert [r[0] for r in await world.rows("SELECT state FROM nodes WHERE NOT is_test ORDER BY state")] == [
        "booting",
        "requested",
        "retiring",
    ]
    executor = world.executor()
    with pytest.raises(ex.FleetNotLive):
        await executor.step(world.sessions, fleet[0])


# --- retire --------------------------------------------------------------------------------------


async def test_retire_a_requested_node_closes_it_without_hetzner(world: LiveWorld) -> None:
    node_id = await request_node(world)
    async with api_client(world) as c:
        r = await c.post(f"/api/nodes/{node_id}/retire")
        assert r.status_code == 202 and r.json() == {}
        again = await c.post(f"/api/nodes/{node_id}/retire")
        assert again.status_code == 409 and again.json()["reasons"] == ["node_deleted"]
        missing = await c.post(f"/api/nodes/{uuid.uuid4()}/retire")
        assert missing.status_code == 404 and missing.json()["error"] == "not_found"
        assert (await c.post("/api/nodes/not-a-uuid/retire")).status_code == 404
        # The test node slot is free again.
        assert (await c.post("/api/nodes/test")).status_code == 202
    assert (await world.node(node_id))["state"] == "deleted"
    assert world.no_hetzner.calls == 0


async def test_retire_refusals(world: LiveWorld) -> None:
    await world.settings(good_settings())
    fleet = await world.add_node(state="ready", is_test=False, slot=2)
    test_node = await world.add_node(state="ready")
    async with api_client(world) as c:
        r = await c.post(f"/api/nodes/{fleet}/retire")
        assert r.status_code == 409 and r.json()["reasons"] == ["fleet_not_live"]
        await world.settings(good_settings(kill_switch=True))
        r = await c.post(f"/api/nodes/{test_node}/retire")
        assert r.status_code == 409 and r.json()["reasons"] == ["kill_switch_on"]
    assert (await world.node(test_node))["state"] == "ready"
    log = await world.rows("SELECT allowed FROM actions_log WHERE action = 'node_retire_request'")
    assert log == [(False,), (False,)]


async def test_retire_without_a_server_and_the_retiring_step(world: LiveWorld) -> None:
    """A ready node that never had a server (e.g. created by hand) closes without any Hetzner call;
    one whose create was attempted is looked up by label first (needs Hetzner: not here)."""
    await world.settings(good_settings())
    node_id = await world.add_node(state="ready")
    async with api_client(world) as c:
        assert (await c.post(f"/api/nodes/{node_id}/retire")).status_code == 202
    assert (await world.node(node_id))["state"] == "retiring"
    await world.settings(good_settings(kill_switch=True))  # no Hetzner call is needed, so not blocked
    await run_executor(world)
    node = await world.node(node_id)
    assert node["state"] == "deleted" and node["retire_reason"] == "owner"
    assert world.no_hetzner.calls == 0
    log = await world.rows("SELECT reasons FROM actions_log WHERE action = 'node_delete'")
    assert log and log[0][0]["reasons"] == ["no_server_requested"]


# --- reading -------------------------------------------------------------------------------------


async def test_list_and_events_follow_the_contract(world: LiveWorld) -> None:
    node_id = await request_node(world)
    gone = await world.add_node(state="deleted", is_test=False)
    await world.rows("UPDATE nodes SET deleted_at = now() - interval '8 days' WHERE id = :n", n=gone)
    await agent_row(world, node_id, at=datetime.now(UTC), report=True)
    for name, conns in (("alice", 7), ("bob", 5)):
        await world.rows(
            "INSERT INTO telemt_users (id, node_id, name, secret_sealed, enabled, last_current_connections, "
            "last_stats_at) VALUES (gen_random_uuid(), :n, :name, decode(repeat('00', 45), 'hex'), true, :c, "
            "now())",
            n=node_id,
            name=name,
            c=conns,
        )
    async with api_client(world) as c:
        nodes = (await c.get("/api/nodes")).json()
        events = (await c.get(f"/api/nodes/{node_id}/events")).json()
        missing = await c.get(f"/api/nodes/{uuid.uuid4()}/events")
    assert [n["id"] for n in nodes] == [str(node_id)]  # deleted over a week ago: not listed
    node = nodes[0]
    assert set(node) == {
        "id", "name", "slot", "location", "server_type", "state", "is_test", "ipv4", "created_at",
        "ready_at", "agent",
    }  # fmt: skip
    assert node["is_test"] is True and node["state"] == "requested" and node["ipv4"] is None
    assert node["created_at"].endswith("Z") and node["ready_at"] is None
    assert node["agent"] == {
        "registered": True,
        "last_seen_at": node["agent"]["last_seen_at"],
        "telemt_version": "3.5.16",
        "connections": 12,
    }
    assert node["agent"]["last_seen_at"].endswith("Z")
    assert events and set(events[0]) == {"at", "kind", "detail"} and events[-1]["kind"] == "requested"
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"


async def test_every_node_route_needs_the_owner(world: LiveWorld) -> None:
    from proxyhub.api import routes_nodes
    from proxyhub.api.app import app

    found = set()
    for route in routes_nodes.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            found.add((method, route.path))
            assert require_admin in {d.call for d in route.dependant.dependencies}, route.path
    assert found == NODE_PATHS
    published = app.openapi()["paths"]
    assert all(method.lower() in published[path] for method, path in NODE_PATHS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://panel.test") as c:
        assert (await c.get("/api/nodes")).status_code == 401
        no_csrf = await c.post("/api/nodes/test")
        assert no_csrf.status_code == 403 and no_csrf.json()["error"] == "csrf"
        for method, path in sorted(NODE_PATHS):
            r = await c.request(method, path.replace("{node_id}", str(uuid.uuid4())), headers=CSRF)
            assert r.status_code == 401, (method, path, r.status_code)


async def test_no_secret_reaches_logs_responses_or_database(
    world: LiveWorld, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    node_id = await request_node(world)
    async with api_client(world) as c:
        texts = [(await c.get("/api/nodes")).text, (await c.get(f"/api/nodes/{node_id}/events")).text]
    from test_fleet_support import assert_absent_from_database

    await assert_absent_from_database(world.engine, world.token)
    for t in [*texts, caplog.text]:
        assert world.token not in t


async def test_migration_matches_models(world: LiveWorld) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import proxyhub.db as db_pkg
    from proxyhub.db.models import Base

    for info in __import__("pkgutil").iter_modules(db_pkg.__path__):
        if info.name.startswith("models_"):
            __import__(f"proxyhub.db.{info.name}")

    def diff(sync_conn: Any) -> list[Any]:
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with world.engine.connect() as conn:
        assert await conn.run_sync(diff) == []
        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'node_operations'"
                )
            )
        ).scalars()
        assert "last_error" in set(cols)
