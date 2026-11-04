"""GET /api/fleet/slots: the nine slots + spare for the dashboard's nodes page (real database)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from test_fleet_live_support import (
    IP_A,
    add_slot_node,
    ago,
    full_fleet,
    live_settings,
    run_fleet,
    set_verdict,
)
from test_live_support import LiveWorld, api_client, live_world

from proxyhub.api.deps import require_admin

pytestmark = [pytest.mark.db, pytest.mark.redis]

ZONE = "e1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
SLOT_KEYS = {"slot", "dns_name", "status", "node", "replacement", "others", "leaving", "plan"}
NODE_KEYS = {
    "id", "name", "state", "location", "server_type", "ipv4", "verdict", "verdict_at", "blocked",
    "created_at", "ready_at", "state_since", "retire_reason",
}  # fmt: skip


@pytest.fixture
async def world(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    async with live_world(migrated_db, tmp_path, monkeypatch) as w:
        await w.rows("DELETE FROM cf_zones WHERE id = :z", z=ZONE)
        try:
            yield w
        finally:
            await w.rows("DELETE FROM cf_zones WHERE id = :z", z=ZONE)


async def slots(world: LiveWorld) -> dict[str, object]:
    async with api_client(world) as c:
        r = await c.get("/api/fleet/slots")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)
    return body


async def test_fresh_panel_shows_nine_empty_slots_stopped(world: LiveWorld) -> None:
    body = await slots(world)
    assert body["mode"] == "stopped"  # no settings: the kill switch is on
    assert (body["target_slots"], body["spare_slots"], body["node_cap_total"]) == (9, 1, 10)
    assert body["active_domain"] is None and body["plan_at"] is None
    assert body["spare"] == {"in_use": False, "slots": []}
    assert body["burned_ips"] == 0 and body["burned_prefixes"] == 0 and body["alerts"] == []
    rows = body["slots"]
    assert isinstance(rows, list) and [s["slot"] for s in rows] == list(range(1, 10))
    assert all(set(s) == SLOT_KEYS and s["status"] == "empty" and s["dns_name"] is None for s in rows)


async def test_slots_show_state_location_ip_verdict_and_the_replacement(world: LiveWorld) -> None:
    await world.settings(live_settings())
    await world.rows(
        "INSERT INTO cf_zones (id, name, status, pool_state, dns_edit, seen_at, pool_changed_at) "
        "VALUES (:z, 'proxy-example.com', 'active', 'active', true, now(), now())",
        z=ZONE,
    )
    nodes = await full_fleet(world, skip=(3,))
    await world.rows("UPDATE nodes SET ipv4 = CAST(:ip AS inet) WHERE id = :n", ip=IP_A, n=nodes[1])
    await set_verdict(world, nodes[1], "blocked")
    await world.rows("UPDATE nodes SET state = 'unhealthy' WHERE id = :n", n=nodes[2])
    await world.rows("UPDATE node_operations SET state_since = now() WHERE node_id = :n", n=nodes[2])
    await set_verdict(world, nodes[4], "ok")
    await run_fleet(world)  # live: slot 1 burns and gets a replacement; slots 3 gets a request too
    body = await slots(world)
    assert body["mode"] == "live" and body["active_domain"] == "proxy-example.com"
    by_slot = {s["slot"]: s for s in body["slots"]}  # type: ignore[union-attr]

    one = by_slot[1]
    assert one["status"] == "replacing" and one["dns_name"] == "slot1.proxy-example.com"
    assert one["node"]["state"] == "burned" and one["node"]["blocked"] is True and one["node"]["ipv4"] == IP_A
    assert set(one["node"]) == NODE_KEYS
    assert one["replacement"]["state"] == "requested" and one["replacement"]["location"] != "fsn1"
    # The loop's latest decision for the slot: the creation it just made (allowed by every gate).
    assert one["plan"] == {"action": "create", "allowed": True, "reasons": []}

    assert by_slot[2]["status"] == "unhealthy" and by_slot[2]["plan"]["reasons"] == ["slot_unhealthy"]
    assert by_slot[3]["status"] == "starting" and by_slot[3]["node"] is None
    assert by_slot[3]["replacement"]["state"] == "requested"
    four = by_slot[4]
    assert (
        four["status"] == "healthy" and four["node"]["verdict"] == "ok" and four["node"]["location"] == "fsn1"
    )
    assert body["spare"] == {"in_use": True, "slots": [1]}
    assert body["burned_ips"] == 1 and body["burned_prefixes"] == 1


async def test_a_settled_replacement_shows_the_old_node_leaving(world: LiveWorld) -> None:
    await world.settings(live_settings(target_slots=1))
    old = await add_slot_node(world, 1, state="burned", hetzner_id=7101, ipv4=IP_A)
    new = await add_slot_node(
        world, 1, state="ready", location="nbg1", hetzner_id=7102, ready_at=ago(minutes=10)
    )
    await run_fleet(world)
    (one,) = (await slots(world))["slots"]  # type: ignore[misc]
    assert one["status"] == "healthy" and one["node"]["id"] == str(new)
    assert [n["id"] for n in one["leaving"]] == [str(old)] and one["leaving"][0]["retire_reason"] == "burned"


async def test_mode_follows_the_owners_switches(world: LiveWorld) -> None:
    await world.settings(live_settings(dry_run=True))
    assert (await slots(world))["mode"] == "dry_run"
    await world.settings(live_settings(kill_switch=True))
    assert (await slots(world))["mode"] == "stopped"
    await world.settings(live_settings())
    assert (await slots(world))["mode"] == "live"


async def test_the_route_needs_the_owner(world: LiveWorld) -> None:
    from proxyhub.api import routes_fleet_live
    from proxyhub.api.app import app

    for route in routes_fleet_live.router.routes:
        assert isinstance(route, APIRoute)
        assert require_admin in {d.call for d in route.dependant.dependencies}
    assert {
        (m, r.path) for r in routes_fleet_live.router.routes if isinstance(r, APIRoute) for m in r.methods
    } == {("GET", "/api/fleet/slots")}  # read-only
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://panel.test") as c:
        assert (await c.get("/api/fleet/slots")).status_code == 401
