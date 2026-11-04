"""The node executor with SLOT nodes of the live fleet, on the real database.

Nothing imitates Hetzner (D10). Either the executor stops before its first Hetzner call
(``NoHetzner`` fails the test if it is reached), or a REAL hcloud client talks to a local port
nothing listens on, which is exactly how an unreachable API fails. Creating and deleting a real
slot server is in tests/test_live_fleet_hetzner.py (marker ``hetzner``, panel server only).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_fleet_live_support import (
    IP_A,
    IP_A_SAME_24,
    IP_B,
    add_slot_node,
    ago,
    closed_port_reader,
    live_settings,
    run_fleet,
)
from test_live_support import LiveWorld, live_world

from proxyhub.fleet.ip_history import mark_burned, public_ipv4
from proxyhub.nodes import executor as ex

pytestmark = [pytest.mark.db, pytest.mark.redis]


@pytest.fixture
async def world(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    async with live_world(migrated_db, tmp_path, monkeypatch) as w:
        yield w


async def burn(world: LiveWorld, ip: str, location: str = "fsn1") -> uuid.UUID:
    """An older slot node of ours whose IP burned (blocked from Iran), long deleted."""
    old = await add_slot_node(
        world, 1, state="deleted", ipv4=ip, location=location, created_at=ago(days=5), deleted_at=ago(days=4)
    )
    async with world.sessions() as s:
        address = public_ipv4(ip)
        assert address is not None
        await mark_burned(
            s, node_id=old, account_id=world.account_id, location=location, ip=address, now=ago(days=4)
        )
        await s.commit()
    return old


async def node_log(world: LiveWorld, action: str) -> list[tuple[bool, list[str]]]:
    rows = await world.rows(
        "SELECT allowed, reasons FROM actions_log WHERE actor = 'node_executor' AND action = :a ORDER BY id",
        a=action,
    )
    return [(r.allowed, r.reasons["reasons"]) for r in rows]


# --- requests --------------------------------------------------------------------------------------


async def test_a_slot_request_passes_the_gates_and_stops_at_an_unreachable_hetzner(world: LiveWorld) -> None:
    await world.settings(live_settings(target_slots=1))
    await run_fleet(world)  # the live fleet loop requests slot 1
    (node_id,) = [r.id for r in await world.rows("SELECT id FROM nodes WHERE slot = 1")]
    t0 = datetime.now(UTC)
    clock = {"now": t0}
    executor = world.executor(clock=lambda: clock["now"], reader_factory=closed_port_reader)
    await executor.run_once(world.engine)
    node = await world.node(node_id)
    # Every gate passed on stored data; the live look at the account (rule 4) could not be taken.
    assert node["state"] == "requested" and node["create_attempts"] == 0
    assert node["last_error"] == "hetzner_unreachable:ConnectionError"
    obs = await world.rows(
        "SELECT ok, error FROM hetzner_account_observations WHERE account_id = :a", a=world.account_id
    )
    assert obs[0].ok is False

    clock["now"] = t0 + ex.REQUEST_RETRY_WINDOW + timedelta(seconds=1)
    await executor.run_once(world.engine)
    node = await world.node(node_id)
    assert node["state"] == "deleted" and node["hetzner_id"] is None and node["create_attempts"] == 0
    assert await node_log(world, "node_create") == [(False, ["hetzner_unreachable"])]
    tokens = await world.rows("SELECT count(*) FROM agent_registration_tokens WHERE node_id = :n", n=node_id)
    assert tokens == [(0,)]  # no registration token was ever issued


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [({"dry_run": True}, "fleet_dry_run"), ({"kill_switch": True}, "kill_switch_on")],
    ids=["dry_run", "kill_switch"],
)
async def test_a_slot_request_is_closed_when_the_fleet_stops_being_live(
    world: LiveWorld, overrides: dict[str, bool], reason: str
) -> None:
    await world.settings(live_settings(target_slots=1))
    await run_fleet(world)
    (node_id,) = [r.id for r in await world.rows("SELECT id FROM nodes WHERE slot = 1")]
    await world.settings(live_settings(target_slots=1, **overrides))
    await world.executor().run_once(world.engine)
    node = await world.node(node_id)
    assert node["state"] == "deleted" and node["create_attempts"] == 0
    refused = [d["reasons"] for k, d in await world.events(node_id) if k == "create_refused"]
    assert refused and reason in refused[0]
    assert world.no_hetzner.calls == 0


# --- burned IPs ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("ip", "match"), [(IP_A, "ip"), (IP_A_SAME_24, "prefix24")])
async def test_a_new_slot_server_with_a_burned_ip_is_retired_at_once(
    world: LiveWorld, ip: str, match: str
) -> None:
    await world.settings(live_settings())
    await burn(world, IP_A)
    fresh = await add_slot_node(
        world, 1, state="booting", location="fsn1", ipv4=ip, hetzner_id=9001, created_at=ago(minutes=2)
    )
    await world.executor().run_once(world.engine)
    node = await world.node(fresh)
    assert node["state"] == "retiring" and node["retire_reason"] == "burned_ip"
    burned = [d for k, d in await world.events(fresh) if k == "burned_ip"]
    assert burned == [{"ipv4": ip, "match": match}]
    assert await node_log(world, "node_burned_ip") == [(True, [f"burned_{match}"])]
    recorded = await world.rows("SELECT ip, burned_at FROM ip_history WHERE node_id = :n", n=fresh)
    assert [(str(r.ip), r.burned_at) for r in recorded] == [(ip, None)]  # every IP we got is kept
    assert world.no_hetzner.calls == 0

    # The fleet retries the slot elsewhere: the next request avoids fsn1.
    await run_fleet(world)
    new = await world.rows("SELECT location, state FROM nodes WHERE slot = 1 AND state = 'requested'")
    assert len(new) == 1 and new[0].location != "fsn1"


async def test_the_delete_of_a_slot_server_waits_for_a_live_fleet(world: LiveWorld) -> None:
    await world.settings(live_settings(kill_switch=True))
    retiring = await add_slot_node(
        world,
        1,
        state="retiring",
        hetzner_id=9002,
        ipv4=IP_B,
        retire_reason="burned",
        created_at=ago(hours=2),
    )
    await world.executor().run_once(world.engine)
    assert (await world.node(retiring))["state"] == "retiring"
    await world.settings(live_settings(dry_run=True))
    await world.executor().run_once(world.engine)
    assert (await world.node(retiring))["state"] == "retiring"
    refusals = [d["reasons"] for k, d in await world.events(retiring) if k == "delete_refused"]
    assert refusals == [["kill_switch_on"], ["fleet_dry_run"]]
    assert world.no_hetzner.calls == 0


async def test_a_test_node_keeps_a_burned_ip(world: LiveWorld) -> None:
    """The owner's test node checks the pipeline, not reachability: it is never refused for its IP."""
    await world.settings(live_settings())
    await burn(world, IP_A)
    test_node = await world.add_node(state="booting", hetzner_id=9003)
    await world.rows("UPDATE nodes SET ipv4 = CAST(:ip AS inet) WHERE id = :n", ip=IP_A, n=test_node)
    await world.executor().run_once(world.engine)
    assert (await world.node(test_node))["state"] == "booting"
    assert await world.rows("SELECT count(*) FROM ip_history WHERE node_id = :n", n=test_node) == [(1,)]


# --- health ---------------------------------------------------------------------------------------


async def test_the_executor_never_retires_an_unhealthy_slot_node_by_itself(world: LiveWorld) -> None:
    """The fleet loop decides (repair window, then create-before-delete); the executor only reports."""
    await world.settings(live_settings())
    node_id = await add_slot_node(world, 3, state="ready", hetzner_id=9004, ipv4=IP_B)
    t0 = datetime.now(UTC)
    clock = {"now": t0}
    executor = world.executor(clock=lambda: clock["now"])
    await executor.run_once(world.engine)
    assert (await world.node(node_id))["state"] == "unhealthy"  # no agent ever reported
    clock["now"] = t0 + ex.UNHEALTHY_TEST_RETIRE_AFTER + timedelta(hours=1)
    await executor.run_once(world.engine)
    node = await world.node(node_id)
    assert node["state"] == "unhealthy" and node["retire_reason"] is None
    assert world.no_hetzner.calls == 0
