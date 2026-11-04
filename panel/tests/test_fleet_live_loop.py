"""The live fleet loop on the real database (decision D14: 9 slots + 1 spare).

The fleet loop never calls Hetzner: in live mode it marks burned nodes, requests slot nodes and
retires nodes as ROWS; the node executor does the Hetzner part through the gates again
(tests/test_live_fleet_executor.py). Every test here also proves that no Hetzner call happened.
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_fleet_live_support import (
    IP_A,
    add_slot_node,
    ago,
    full_fleet,
    live_settings,
    run_fleet,
    set_verdict,
    slot_rows,
)
from test_live_support import CPX02_HOURLY, CPX02_MONTHLY, LiveWorld, live_world

from proxyhub.fleet.planner import REPAIR_WINDOW, REPLACEMENT_SETTLE
from proxyhub.safety.budget import Price, rest_of_month_cost
from proxyhub.telegram.store import set_owner_chat

pytestmark = [pytest.mark.db, pytest.mark.redis]


@pytest.fixture
async def world(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    async with live_world(migrated_db, tmp_path, monkeypatch) as w:
        yield w


async def fleet_log(world: LiveWorld, action: str) -> list[tuple[str, bool, list[str]]]:
    rows = await world.rows(
        "SELECT target, allowed, reasons FROM actions_log WHERE actor = 'fleet' AND action = :a ORDER BY id",
        a=action,
    )
    return [(r.target, r.allowed, r.reasons["reasons"]) for r in rows]


async def last_plan(world: LiveWorld) -> tuple[bool, dict[str, dict[str, object]]]:
    rows = await world.rows("SELECT dry_run, actions FROM fleet_plans ORDER BY id DESC LIMIT 1")
    dry, actions = rows[0]
    return dry, {a["target"]: a for a in actions}


# --- nothing happens unless the owner turned the fleet live ------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [{"dry_run": True}, {"kill_switch": True}, {"dry_run": True, "kill_switch": True}],
    ids=["dry_run", "kill_switch", "both"],
)
async def test_nothing_changes_while_dry_run_or_the_kill_switch_is_on(
    world: LiveWorld, overrides: dict[str, bool]
) -> None:
    await world.settings(live_settings(**overrides))
    nodes = await full_fleet(world, skip=(9,))  # slot 9 empty: a creation is needed
    await set_verdict(world, nodes[1], "blocked")  # slot 1 blocked from Iran
    await world.rows("UPDATE nodes SET ready_at = now() - interval '1 hour' WHERE id = :n", n=nodes[2])
    before = [tuple(r) for r in await slot_rows(world)]
    for _ in range(2):
        await run_fleet(world)
    assert [tuple(r) for r in await slot_rows(world)] == before  # no row changed, none added
    assert await world.rows("SELECT count(*) FROM ip_history") == [(0,)]
    dry, plan = await last_plan(world)
    assert dry is True
    assert plan["slot-9"]["action"] == "create" and plan["slot-1"]["action"] == "create"  # would replace
    assert await world.rows("SELECT bool_and(dry_run) FROM actions_log WHERE actor = 'fleet'") == [(True,)]
    assert await fleet_log(world, "node_request") == [] and await fleet_log(world, "node_burned") == []
    assert world.no_hetzner.calls == 0


# --- filling the slots ------------------------------------------------------------------------------


async def test_live_fills_empty_slots_only_within_the_hourly_creation_cap(world: LiveWorld) -> None:
    await world.settings(live_settings())
    await run_fleet(world)
    rows = await slot_rows(world)
    assert [(r.slot, r.location, r.state, r.requested_by) for r in rows] == [
        (1, "fsn1", "requested", "fleet"),
        (2, "nbg1", "requested", "fleet"),
        (3, "hel1", "requested", "fleet"),
    ]
    assert all(r.name == f"ph-s{r.slot}-{r.id.hex[:8]}" for r in rows)
    dry, plan = await last_plan(world)
    assert dry is False
    for slot in range(4, 10):
        assert "creation_rate_cap_reached" in plan[f"slot-{slot}"]["reasons"]
    requested = await fleet_log(world, "node_request")
    assert [(t.split("-")[1], ok) for t, ok, _ in requested] == [("s1", True), ("s2", True), ("s3", True)]
    events = await world.rows("SELECT kind, detail FROM node_events WHERE node_id = :n", n=rows[0].id)
    assert events[0].kind == "requested" and events[0].detail["by"] == "fleet"
    assert events[0].detail["need"] == "fill" and events[0].detail["slot"] == 1

    await run_fleet(world)  # the next minute: the three are on their way, the cap is used up
    assert len(await slot_rows(world)) == 3
    dry, plan = await last_plan(world)
    assert plan["slot-1"]["reasons"] == ["slot_starting"]
    assert world.no_hetzner.calls == 0


async def drop_fleet_nodes(world: LiveWorld) -> None:
    await world.rows("DELETE FROM node_events WHERE node_id IN (SELECT id FROM nodes WHERE NOT is_test)")
    await world.rows("DELETE FROM nodes WHERE NOT is_test")


async def test_live_never_passes_the_budget_or_the_node_cap(world: LiveWorld) -> None:
    one = rest_of_month_cost(Price(hourly=CPX02_HOURLY, monthly=CPX02_MONTHLY), datetime.now(UTC))
    cap = (one * Decimal("1.5")).quantize(Decimal("0.01"))  # one server to the month's end fits, two do not
    await world.settings(live_settings(budget_cap_eur=cap, creations_per_hour_cap=20))
    await run_fleet(world)
    assert len(await slot_rows(world)) == 1
    _, plan = await last_plan(world)
    assert "budget_cap_reached" in plan["slot-2"]["reasons"]

    await drop_fleet_nodes(world)
    await world.settings(live_settings(node_cap_total=2, creations_per_hour_cap=20))
    await run_fleet(world)
    assert len(await slot_rows(world)) == 2
    _, plan = await last_plan(world)
    assert "node_cap_reached" in plan["slot-3"]["reasons"]
    await world.rows("UPDATE settings SET value = CAST('{\"bogus\": 1}' AS jsonb) WHERE key = 'fleet'")
    await drop_fleet_nodes(world)
    await run_fleet(world)  # corrupt settings: the loop falls back to the safe defaults (dry)
    assert await slot_rows(world) == []
    assert world.no_hetzner.calls == 0


async def test_a_refused_request_backs_off_and_counts_as_no_creation(world: LiveWorld) -> None:
    await world.settings(live_settings(target_slots=2, creations_per_hour_cap=1))
    refused = await add_slot_node(
        world,
        1,
        state="deleted",
        created_at=ago(minutes=3),
        deleted_at=ago(minutes=2),
        create_attempted=False,
    )
    await run_fleet(world)
    _, plan = await last_plan(world)
    assert plan["slot-1"]["reasons"] == ["request_backoff"]  # the executor said no a moment ago
    # The refused request made no server: the hourly cap of 1 is still free for slot 2.
    assert [(r.slot, r.state) for r in await slot_rows(world) if r.id != refused] == [(2, "requested")]


# --- burned nodes: create first, then retire ------------------------------------------------------


async def test_a_blocked_node_burns_and_is_replaced_create_before_delete(world: LiveWorld) -> None:
    await world.settings(live_settings())
    nodes = await full_fleet(world)
    await world.rows("UPDATE nodes SET ipv4 = CAST(:ip AS inet) WHERE id = :n", ip=IP_A, n=nodes[1])
    await set_verdict(world, nodes[1], "blocked")
    await set_verdict(world, nodes[2], "blocked", at=ago(hours=2))  # too old: decides nothing
    await run_fleet(world)

    rows = {r.id: r for r in await slot_rows(world)}
    assert rows[nodes[1]].state == "burned" and rows[nodes[2]].state == "ready"
    burned = await world.rows("SELECT ip, prefix24, burned_reason, location FROM ip_history")
    assert [(str(r.ip), str(r.prefix24), r.burned_reason, r.location) for r in burned] == [
        (IP_A, "49.13.10.0/24", "probe_blocked", "fsn1")
    ]
    new = [r for r in rows.values() if r.slot == 1 and r.id != nodes[1]]
    assert len(new) == 1 and new[0].state == "requested"
    assert new[0].location != "fsn1"  # D14: another location than the burned server's
    assert await fleet_log(world, "node_retire") == []  # the burned node stays until replaced
    assert [(t, ok) for t, ok, _ in await fleet_log(world, "node_burned")] == [(rows[nodes[1]].name, True)]

    # The executor brings the replacement up; right after it is ready the old one still stays.
    await world.rows(
        "UPDATE nodes SET state = 'ready', ready_at = now(), hetzner_id = 6001, ipv4 = '5.75.1.2' "
        "WHERE id = :n",
        n=new[0].id,
    )
    await run_fleet(world)
    _, plan = await last_plan(world)
    assert plan["slot-1"]["reasons"] == ["replacement_settling"]
    assert (await world.node(nodes[1]))["state"] == "burned"

    settled = datetime.now(UTC) - REPLACEMENT_SETTLE - timedelta(seconds=5)
    await world.rows("UPDATE nodes SET ready_at = :t WHERE id = :n", t=settled, n=new[0].id)
    await run_fleet(world)
    old = await world.node(nodes[1])
    assert old["state"] == "retiring" and old["retire_reason"] == "burned"
    assert [(ok, why) for _, ok, why in await fleet_log(world, "node_retire")] == [(True, ["burned"])]
    assert (await world.node(new[0].id))["state"] == "ready"
    assert world.no_hetzner.calls == 0


async def test_a_blocked_verdict_on_a_closed_port_burns_nothing(world: LiveWorld) -> None:
    # A refused port fails from every Iranian network exactly like a block. Unless the node's own
    # agent says Telemt (the probed service) is up, the node and its /24 are never burned.
    await world.settings(live_settings())
    nodes = await full_fleet(world)
    await world.rows("UPDATE nodes SET ipv4 = CAST(:ip AS inet) WHERE id = :n", ip=IP_A, n=nodes[1])
    await set_verdict(world, nodes[1], "blocked", serving=False)
    await run_fleet(world)
    assert (await world.node(nodes[1]))["state"] == "ready"
    assert await world.rows("SELECT ip FROM ip_history") == []
    assert await fleet_log(world, "node_burned") == []
    # Once the agent reports Telemt up again, the same verdict does burn it.
    await set_verdict(world, nodes[1], "blocked")
    await run_fleet(world)
    assert (await world.node(nodes[1]))["state"] == "burned"


async def test_a_second_burn_waits_for_the_single_spare(world: LiveWorld) -> None:
    await world.settings(live_settings(node_cap_total=11))
    nodes = await full_fleet(world)
    for slot in (1, 2):
        await set_verdict(world, nodes[slot], "blocked")
    await run_fleet(world)
    rows = await slot_rows(world)
    assert sum(1 for r in rows if r.state == "burned") == 2
    assert [r.slot for r in rows if r.state == "requested"] == [1]  # 9 + 1 spare at most
    _, plan = await last_plan(world)
    assert "spare_in_use" in plan["slot-2"]["reasons"]


# --- unhealthy nodes: a repair window, then a replacement ------------------------------------------


async def test_an_unhealthy_node_gets_its_repair_window_then_a_replacement(world: LiveWorld) -> None:
    await world.settings(live_settings())
    nodes = await full_fleet(world)
    await world.rows("UPDATE nodes SET state = 'unhealthy' WHERE id = :n", n=nodes[4])
    await world.rows(
        "UPDATE node_operations SET state_since = now() - interval '5 minutes' WHERE node_id = :n", n=nodes[4]
    )
    await run_fleet(world)
    _, plan = await last_plan(world)
    assert plan["slot-4"]["reasons"] == ["slot_unhealthy"]
    assert len(await slot_rows(world)) == 9

    since = datetime.now(UTC) - REPAIR_WINDOW - timedelta(minutes=1)
    await world.rows("UPDATE node_operations SET state_since = :t WHERE node_id = :n", t=since, n=nodes[4])
    await run_fleet(world)
    new = [r for r in await slot_rows(world) if r.slot == 4 and r.id != nodes[4]]
    assert len(new) == 1 and new[0].location != "fsn1"  # slot 4 lives in fsn1
    assert (await world.node(nodes[4]))["state"] == "unhealthy"  # kept: it may still recover


async def test_a_node_that_never_served_is_retired_with_its_replacement(world: LiveWorld) -> None:
    await world.settings(live_settings(target_slots=1))
    broken = await add_slot_node(
        world, 1, state="unhealthy", hetzner_id=7001, created_at=ago(minutes=40), state_since=ago(minutes=20)
    )
    await world.rows("UPDATE nodes SET ready_at = NULL WHERE id = :n", n=broken)
    await run_fleet(world)
    node = await world.node(broken)
    assert node["state"] == "retiring" and node["retire_reason"] == "unhealthy"
    assert [r.state for r in await slot_rows(world) if r.id != broken] == ["requested"]


async def test_lowering_the_target_retires_the_excess_slots(world: LiveWorld) -> None:
    await world.settings(live_settings(target_slots=7))
    nodes = await full_fleet(world)
    requested = await add_slot_node(world, 9, state="requested", created_at=ago(minutes=1))
    await world.rows("DELETE FROM nodes WHERE id = :n", n=nodes[9])
    await run_fleet(world)
    assert (await world.node(nodes[8]))["retire_reason"] == "excess"
    assert (await world.node(requested))["state"] == "deleted"  # never reached Hetzner: withdrawn
    for slot in range(1, 8):
        assert (await world.node(nodes[slot]))["state"] == "ready"


# --- owner alerts ----------------------------------------------------------------------------------


async def test_a_cap_that_blocks_a_needed_server_alerts_the_owner_once(world: LiveWorld) -> None:
    async with world.sessions() as s:
        await set_owner_chat(s, 424242, datetime.now(UTC))
        await s.commit()
    await world.settings(live_settings(budget_cap_eur=Decimal("0.01")))  # below one server-hour
    await run_fleet(world)
    rows = await world.rows("SELECT code, active, last_sent_at FROM fleet_alerts")
    assert [(r.code, r.active, r.last_sent_at) for r in rows] == [("budget_cap_reached", True, None)]
    assert await world.rows("SELECT count(*) FROM outbox WHERE kind = 'alert'") == [(0,)]  # not yet: 5 min

    await world.rows("UPDATE fleet_alerts SET since = now() - interval '6 minutes'")
    await run_fleet(world)
    await run_fleet(world)  # once, not every minute
    outbox = await world.rows("SELECT payload FROM outbox WHERE kind = 'alert'")
    assert len(outbox) == 1
    text = outbox[0].payload["text"]
    assert "سقف بودجه" in text and "جایگاه‌های ۱، ۲، ۳" in text
    log = await fleet_log(world, "fleet_alert")
    assert [(t, ok) for t, ok, _ in log] == [("budget_cap_reached", False)]


async def test_no_alert_and_no_change_on_the_kill_switch(world: LiveWorld) -> None:
    async with world.sessions() as s:
        await set_owner_chat(s, 424242, datetime.now(UTC))
        await s.commit()
    await world.settings(live_settings(budget_cap_eur=Decimal("0.01"), kill_switch=True))
    await run_fleet(world)
    assert await world.rows("SELECT count(*) FROM fleet_alerts") == [(0,)]
    assert await world.rows("SELECT count(*) FROM outbox") == [(0,)]


def test_ip_history_addresses_are_ours_only() -> None:
    """The fleet records only global unicast IPv4 (what Hetzner gives a server)."""
    from proxyhub.fleet.ip_history import prefix24, public_ipv4

    assert str(public_ipv4(IP_A)) == IP_A and str(prefix24(ipaddress.IPv4Address(IP_A))) == "49.13.10.0/24"
    for bad in ("10.0.0.1", "127.0.0.1", "2a01:4f8::1", "not-an-ip", None, "192.168.1.5/32"):
        assert public_ipv4(bad) is None
