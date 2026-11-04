"""Rules 3 and 7 for the live fleet: the Hetzner token never reaches a log, a response or a row;
the only addresses stored or shown are our own servers' (rule 7 is about users)."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from test_fleet_live_support import (
    add_slot_node,
    closed_port_reader,
    full_fleet,
    live_settings,
    run_fleet,
    set_verdict,
)
from test_fleet_support import assert_absent_from_database
from test_live_support import LiveWorld, api_client, live_world

pytestmark = [pytest.mark.db, pytest.mark.redis]


@pytest.fixture
async def world(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    async with live_world(migrated_db, tmp_path, monkeypatch) as w:
        yield w


async def test_no_token_anywhere_after_a_live_fleet_pass(
    world: LiveWorld, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await world.settings(live_settings())
    nodes = await full_fleet(world, skip=(9,))
    await set_verdict(world, nodes[1], "blocked")
    await run_fleet(world)  # burns slot 1, requests slot 1's replacement and slot 9
    executor = world.executor(reader_factory=closed_port_reader)
    await executor.run_once(world.engine)  # the requests reach the (unreachable) live account look
    async with api_client(world) as c:
        texts = [(await c.get(p)).text for p in ("/api/fleet/slots", "/api/nodes", "/api/events?limit=200")]
    await assert_absent_from_database(world.engine, world.token)
    for t in [*texts, caplog.text]:
        assert world.token not in t
        assert "#cloud-config" not in t


async def test_only_our_server_addresses_are_kept(world: LiveWorld) -> None:
    """ip_history, node rows and the slots view hold global unicast IPv4 of our servers only."""
    await world.settings(live_settings(target_slots=1))
    node_id = await add_slot_node(world, 1, state="ready", ipv4="49.13.10.20", hetzner_id=8101)
    await set_verdict(world, node_id, "blocked")
    await run_fleet(world)
    rows = await world.rows("SELECT ip, prefix24, node_id FROM ip_history")
    assert [(str(r.ip), str(r.prefix24), r.node_id) for r in rows] == [
        ("49.13.10.20", "49.13.10.0/24", node_id)
    ]
    for r in rows:
        assert ipaddress.ip_interface(str(r.ip)).ip.is_global
    async with world.engine.connect() as conn:
        from sqlalchemy import text

        cols = (
            await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name IN "
                    "('ip_history', 'fleet_alerts', 'domain_delegation_checks', 'domain_delegation_state')"
                )
            )
        ).scalars()
        names = set(cols)
    # Nothing that could describe a user: no client address, user agent, port or traffic column.
    assert not {"client_ip", "user_ip", "remote_addr", "user_agent", "src_port", "bytes"} & names
