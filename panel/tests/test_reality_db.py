"""Reality on a real PostgreSQL with the real vault: the policy (keys, staggered configs, ends),
usage from agent reports, the desired state's xray section, the agent API over the real app, the
links provider, the dashboard API, and that no private key or user id leaks anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_agent_support import AGENT_DIR, FP_HEADER, PROXY_HEADER, requires_go, run_tool
from test_fleet_support import ADMIN, CSRF, assert_absent_from_database
from test_reality_support import World, world_fixture  # noqa: F401 - the `world` fixture

from proxyhub.agent_api.desired import build_desired_state, canonical_json, etag_for
from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.agent_api.tokens import issue_registration_token
from proxyhub.api.deps import require_admin
from proxyhub.reality.keys import public_from_private
from proxyhub.reality.links import provide_links
from proxyhub.reality.policy import (
    create_configs,
    end_configs,
    ensure_keys,
    forget_gone_node_keys,
    renew_configs,
    retarget_scopes,
)
from proxyhub.reality.settings import GB, RealitySettings, save_reality_settings
from proxyhub.reality.store import store_xray_report, xray_section
from proxyhub.reconcilers.reality import RealityReconciler
from proxyhub.telegram.store import save_channel

pytestmark = pytest.mark.db

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
# The tests were written for a 24-hour config life; F scales those hours to the current default
# (D41: 48 hours), and CAP is the default data cap (D41: 1 TB).
F = RealitySettings().cap_hours / 24
CAP = RealitySettings().cap_bytes


async def _create(world: World, now: datetime, settings: RealitySettings | None = None) -> list[Any]:
    """One policy round's making step: keys for new scopes, then due configs (as the loop does)."""
    async with world.engine.begin() as conn:
        await ensure_keys(conn, settings or RealitySettings(), now)
        return await create_configs(conn, settings or RealitySettings(), now)


async def _retarget(world: World, now: datetime, settings: RealitySettings) -> list[Any]:
    async with world.engine.begin() as conn:
        return await retarget_scopes(conn, settings, now)


async def _keys(world: World) -> list[Any]:
    return await world.rows(
        "SELECT id, slot, node_id, public_key, short_id, target, server_name, secret_name FROM reality_keys "
        "ORDER BY id"
    )


async def _end(world: World, now: datetime) -> dict[int, str]:
    async with world.engine.begin() as conn:
        return dict(await end_configs(conn, now))


async def _configs(world: World) -> list[Any]:
    return await world.rows(
        "SELECT id, node_id, slot, key_id, uuid, host, status, used_bytes, data_cap_bytes, created_at, "
        "expires_at FROM reality_configs ORDER BY id"
    )


async def _section(world: World, node_id: uuid.UUID) -> dict[str, Any] | None:
    async with world.engine.connect() as conn:
        return await xray_section(conn, node_id, world.vault)


# --- policy --------------------------------------------------------------------------------------


async def test_a_ready_node_gets_a_vaulted_key_and_staggered_configs(world: World) -> None:
    node = await world.node(slot=3)
    assert await _create(world, T0) == []  # no report from an agent that manages Xray yet
    await world.report(node, now=T0, enabled=False)
    assert await _create(world, T0) == []  # an agent without Xray
    await world.report(node, now=T0)
    [made] = await _create(world, T0)
    assert made.slot == 3 and made.host == "203.0.113.10"  # no active domain

    [key] = await _keys(world)
    assert (key.slot, key.node_id, key.target, key.server_name) == (
        3,
        None,
        "dl.google.com:443",
        "dl.google.com",
    )
    async with world.engine.connect() as conn:
        secret = await world.vault.get(conn, key.secret_name)
    assert secret is not None
    private = secret.reveal()
    assert public_from_private(private) == key.public_key
    await assert_absent_from_database(world.engine, private)

    [c] = await _configs(world)
    assert (c.status, c.slot, c.node_id, c.data_cap_bytes) == ("active", 3, node, CAP)
    assert c.expires_at - c.created_at == timedelta(hours=24 * F)
    # Two per scope, half a lifetime apart (each round needs a fresh report from the node).
    await world.report(node, now=T0 + timedelta(hours=11 * F))
    assert await _create(world, T0 + timedelta(hours=11 * F)) == []
    await world.report(node, now=T0 + timedelta(hours=12 * F))
    [second] = await _create(world, T0 + timedelta(hours=12 * F))
    assert second.slot == 3 and len(await _keys(world)) == 1  # the same scope key
    await world.report(node, now=T0 + timedelta(hours=23 * F))
    assert await _create(world, T0 + timedelta(hours=23 * F)) == []  # two active already
    await world.report(node, now=T0 + timedelta(hours=24 * F))
    # The first one expires; a fresh one follows once the newest is half a lifetime old.
    assert await _end(world, T0 + timedelta(hours=24 * F)) == {c.id: "expired"}
    assert await _create(world, T0 + timedelta(hours=24 * F)) != []


async def _renew(world: World, now: datetime, settings: RealitySettings | None = None) -> list[int]:
    async with world.engine.begin() as conn:
        return await renew_configs(conn, settings or RealitySettings(), now)


async def test_a_working_config_is_renewed_in_place_before_it_ends(world: World) -> None:
    """D41: a config that still works gets another period an hour before its end: same row, same
    uuid (so the same link), 48 hours more and 1 TB more on top of what it used."""
    node = await world.node(slot=3)
    await world.report(node, now=T0)
    [_made] = await _create(world, T0)
    [c] = await _configs(world)
    await world.report(node, now=T0 + timedelta(hours=40), users={f"cfg-{c.id}@proxyhub": 300 * GB})
    life = timedelta(hours=RealitySettings().cap_hours)
    assert await _renew(world, c.expires_at - timedelta(hours=2)) == []  # not near its end yet
    near = c.expires_at - timedelta(minutes=50)
    await world.report(node, now=near)
    assert await _renew(world, near) == [c.id]
    [after] = await _configs(world)
    assert (after.id, after.uuid, after.host, after.status) == (c.id, c.uuid, c.host, "active")
    assert after.expires_at == c.expires_at + life
    assert after.data_cap_bytes == 300 * GB + CAP
    assert await _end(world, c.expires_at + timedelta(minutes=1)) == {}  # nothing ends at the old time
    assert await _renew(world, near) == []  # once per period
    section = await _section(world, node)
    assert section is not None and [u["expires_at"] for u in section["users"]] == [
        after.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    ]


@pytest.mark.parametrize(
    "case", ["full", "target_failing", "node_retiring", "renew_off", "new_target_pending", "disabled"]
)
async def test_a_config_that_does_not_work_is_not_renewed(world: World, case: str) -> None:
    node = await world.node(slot=3)
    await world.report(node, now=T0)
    await _create(world, T0)
    [c] = await _configs(world)
    near = c.expires_at - timedelta(minutes=30)
    settings = RealitySettings()
    await world.report(node, now=near)
    if case == "full":
        await world.report(node, now=near, users={f"cfg-{c.id}@proxyhub": CAP})
    elif case == "target_failing":
        await world.report(node, now=near, target_ok=False)
    elif case == "node_retiring":
        await world.rows("UPDATE nodes SET state = 'retiring' WHERE id = :n", n=node)
    elif case == "renew_off":
        settings = RealitySettings(renew=False)
    elif case == "new_target_pending":
        settings = RealitySettings(target_host="www.example.org")
    else:
        settings = RealitySettings(enabled=False)
    assert await _renew(world, near, settings) == []
    [after] = await _configs(world)
    assert after.expires_at == c.expires_at


async def test_slot_configs_follow_the_slot_and_burn_with_their_domain(world: World) -> None:
    zone = await world.zone("a")
    old = await world.node(slot=2, ipv4="203.0.113.20", ready_at=T0 - timedelta(days=2))
    await world.report(old, now=T0)
    [made] = await _create(world, T0)
    assert made.host == f"slot2.{zone}"

    # A new server takes slot 2 over: it serves the same key and the same configs.
    new = await world.node(slot=2, ipv4="203.0.113.21", ready_at=T0)
    await world.report(new, now=T0)
    a, b = await _section(world, old), await _section(world, new)
    assert a is not None and a == b and len(a["users"]) == 1
    await world.rows("UPDATE nodes SET state = 'burned' WHERE id = :n", n=old)
    assert await _end(world, T0 + timedelta(hours=1)) == {}  # the slot name still works
    # Fresh configs go to the newest ready node of the slot.
    await world.report(new, now=T0 + timedelta(hours=12 * F))
    [fresh] = await _create(world, T0 + timedelta(hours=12 * F))
    assert fresh.node_id == new and fresh.host == f"slot2.{zone}"

    # The domain is filtered and another one becomes active: the old names no longer work.
    await world.rows("UPDATE cf_zones SET pool_state = 'filtered' WHERE name = :z", z=zone)
    await world.zone("b")
    ended = await _end(world, T0 + timedelta(hours=13 * F))
    assert sorted(ended.values()) == ["burned", "burned"]


async def test_ip_links_burn_with_their_server_and_scopes_without_servers(world: World) -> None:
    test_node = await world.node(slot=None, ipv4="198.51.100.5")
    await world.report(test_node, now=T0)
    [made] = await _create(world, T0)
    assert made.host == "198.51.100.5" and made.slot is None
    [key] = await world.rows("SELECT slot, node_id FROM reality_keys")
    assert key.slot is None and key.node_id == test_node
    await world.rows("UPDATE nodes SET state = 'unhealthy' WHERE id = :n", n=test_node)
    assert await _end(world, T0 + timedelta(minutes=5)) == {}
    await world.rows("UPDATE nodes SET state = 'burned' WHERE id = :n", n=test_node)
    assert await _end(world, T0 + timedelta(minutes=10)) == {made.config_id: "burned"}

    # A slot whose only server is gone: its configs are burned too.
    gone = await world.node(slot=5)
    await world.report(gone, now=T0)
    [slot_made] = await _create(world, T0)
    await world.rows("UPDATE nodes SET state = 'deleted' WHERE id = :n", n=gone)
    assert await _end(world, T0 + timedelta(minutes=1)) == {slot_made.config_id: "burned"}


async def test_the_private_key_of_a_deleted_slotless_node_is_forgotten(world: World) -> None:
    test_node = await world.node(slot=None, ipv4="198.51.100.6")
    slot_node = await world.node(slot=7, ipv4="198.51.100.7")
    idle_node = await world.node(slot=None, ipv4="198.51.100.8")  # in service, key only
    for n in (test_node, slot_node):
        await world.report(n, now=datetime.now(UTC))
    await world.report(idle_node, now=datetime.now(UTC), target_ok=None)
    await _loop(world)
    keys = {k.node_id or k.slot: k for k in await _keys(world)}
    assert set(keys) == {test_node, 7, idle_node}

    async def secrets() -> list[str]:
        return [r.name for r in await world.rows("SELECT name FROM secrets WHERE name LIKE 'reality:%'")]

    # In service (with or without a config), or gone but with an active config: kept.
    await _loop(world)
    assert len(await secrets()) == 3
    await world.rows("UPDATE nodes SET state = 'deleted' WHERE id IN (:a, :b)", a=test_node, b=slot_node)
    async with world.engine.begin() as conn:
        assert await forget_gone_node_keys(conn) == []  # its config is still active
    assert len(await secrets()) == 3
    await _loop(world)  # the configs end as burned first, then the slotless node's key goes
    # A slot keeps its key for its next server; the idle node is still in service.
    assert sorted(await secrets()) == sorted([keys[7].secret_name, keys[idle_node].secret_name])
    assert len(await _keys(world)) == 3 and all(c.status == "burned" for c in await _configs(world))
    [log] = await world.rows("SELECT target FROM actions_log WHERE action = 'reality_key_forgotten'")
    assert log.target == f"node:{test_node}"
    await _loop(world)  # once only
    assert len(await world.rows("SELECT id FROM actions_log WHERE action = 'reality_key_forgotten'")) == 1


async def test_usage_from_reports_fills_configs(world: World) -> None:
    a = await world.node(slot=1, ipv4="203.0.113.31", ready_at=T0 - timedelta(hours=1))
    b = await world.node(slot=1, ipv4="203.0.113.32", ready_at=T0 - timedelta(hours=2))
    other = await world.node(slot=4, ipv4="203.0.113.33")
    for n in (a, b, other):
        await world.report(n, now=T0)
    await _create(world, T0)
    [c1, c4] = sorted(await _configs(world), key=lambda r: r.slot)
    email, foreign = f"cfg-{c1.id}@proxyhub", f"cfg-{c4.id}@proxyhub"

    assert await world.report(a, users={email: 1_000, foreign: 5 * 10**12, "cfg-999999@proxyhub": 7}) == 1
    assert await world.report(b, users={email: 500}) == 1
    assert await world.report(a, users={email: 3_000}) == 1  # +2000
    assert await world.report(a, users={email: 400}) == 1  # the agent started again: +400
    rows = {r.id: r for r in await _configs(world)}
    assert rows[c1.id].used_bytes == 3_400 + 500
    assert rows[c4.id].used_bytes == 0, "a node may only report its own scope's configs"
    # Over the cap: full at the next policy round.
    await world.report(b, users={email: CAP})
    assert await _end(world, T0 + timedelta(minutes=1)) == {c1.id: "full"}


async def test_nodes_that_must_not_get_configs(world: World) -> None:
    cases = {}
    cases["target"] = await world.node(slot=11)
    await world.report(cases["target"], now=T0, target_ok=False)
    cases["error"] = await world.node(slot=12)
    await world.report(cases["error"], now=T0, apply_error="xray_config_refused")
    cases["stale"] = await world.node(slot=13)
    await world.report(cases["stale"], now=T0 - timedelta(minutes=11))
    cases["clash"] = await world.node(slot=14)
    await world.report(cases["clash"], now=T0)
    await world.rows(
        "INSERT INTO telemt_node_configs (node_id, ports, tls_domain, mode_classic, mode_secure, mode_tls) "
        "VALUES (:n, ARRAY[443], 'cdn.example.com', false, false, true)",
        n=cases["clash"],
    )
    cases["booting"] = await world.node(slot=15, state="booting")
    await world.report(cases["booting"], now=T0)
    cases["no_ip"] = await world.node(slot=16, ipv4=None)
    await world.report(cases["no_ip"], now=T0)
    assert await _create(world, T0) == []
    # The owner's switch: off means nothing new anywhere (and on again, the same node gets one).
    ok = await world.node(slot=17)
    await world.report(ok, now=datetime.now(UTC))
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(enabled=False))
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)
    assert await _configs(world) == []
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(enabled=True))
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)
    [made] = await _configs(world)
    assert made.node_id == ok
    # Telemt moved onto 443 (or Xray's API port) on a node that serves Reality: Xray is left out of
    # its desired state at once (the agent would refuse the whole document otherwise).
    assert await _section(world, ok) is not None
    await world.rows(
        "INSERT INTO telemt_node_configs (node_id, ports, tls_domain, mode_classic, mode_secure, mode_tls) "
        "VALUES (:n, ARRAY[8443, 10085], 'cdn.example.com', false, false, true)",
        n=ok,
    )
    assert await _section(world, ok) is None


NEW_TARGET = ("www.example.org:443", "www.example.org")


async def _loop(world: World) -> None:
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)


async def test_a_new_scope_gets_its_key_first_and_a_config_once_the_node_confirmed_the_target(
    world: World,
) -> None:
    node = await world.node(slot=3)
    await world.report(node, now=T0, target_ok=None)  # the agent never had a target to check
    assert await _create(world, T0) == []
    [key] = await _keys(world)
    section = await _section(world, node)
    assert section is not None and section["users"] == [] and section["target"] == key.target
    # A result about another target does not count, failed or not.
    await world.report(node, now=T0, target_ok=True, checked=NEW_TARGET)
    assert await _create(world, T0) == []
    await world.report(node, now=T0, target_ok=False)
    assert await _create(world, T0) == []
    await world.report(node, now=T0, target_ok=True)
    [made] = await _create(world, T0)
    assert made.slot == 3 and len(await _keys(world)) == 1


async def test_a_failing_target_is_replaced_by_the_owners_new_target(world: World) -> None:
    """Review finding: a slot whose target failed could never get a config again, whatever the
    owner set. Now the owner's new target reaches the slot, the node checks it, configs resume."""
    zone = await world.zone("t")
    node = await world.node(slot=3)
    expired_node = await world.node(slot=4, ipv4="203.0.113.44")
    working = await world.node(slot=6, ipv4="203.0.113.46")  # its target keeps working
    now = datetime.now(UTC)
    for n in (node, expired_node, working):
        await world.report(n, now=now)
    await _loop(world)
    configs = {c.slot: c for c in await _configs(world)}
    assert set(configs) == {3, 4, 6}
    [old3, old4, old6] = await _keys(world)
    # The target fails from both nodes; slot 4's config has run out meanwhile (the reviewer's case).
    await world.rows(
        "UPDATE reality_configs SET created_at = created_at - interval '2 days', "
        "expires_at = created_at - interval '1 day' WHERE slot = 4"
    )
    for n in (node, expired_node):
        await world.report(n, now=now, target_ok=False)
    await _loop(world)
    assert {c.slot: c.status for c in await _configs(world)} == {3: "active", 4: "expired", 6: "active"}
    [failing] = await world.rows(
        "SELECT reasons->'context' AS context FROM actions_log WHERE action = 'reality_target_failing'"
    )
    assert sorted(failing.context["nodes"]) == sorted(
        r.name
        for r in await world.rows("SELECT name FROM nodes WHERE id IN (:a, :b)", a=node, b=expired_node)
    )

    # The owner sets another target in the dashboard.
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(target_host="www.example.org"))
    await _loop(world)
    rows = {c.slot: c for c in await _configs(world)}
    assert rows[3].status == "burned" and rows[4].status == "expired"  # old target, no new config yet
    keys = await _keys(world)
    for before, after in zip((old3, old4), keys[:2], strict=True):
        assert (after.target, after.server_name) == NEW_TARGET
        assert after.public_key == before.public_key and after.secret_name == before.secret_name
        assert after.short_id != before.short_id
    # Slot 6's target works and its config is in use: it keeps both until that config ends.
    assert rows[6].status == "active" and keys[2] == old6
    section = await _section(world, node)
    assert section is not None
    assert (section["target"], section["server_names"], section["users"]) == (
        NEW_TARGET[0],
        [NEW_TARGET[1]],
        [],
    )
    assert section["short_ids"] == [keys[0].short_id]
    logs = await world.rows(
        "SELECT action, target, CAST(reasons AS text) AS reasons FROM actions_log "
        "WHERE actor = 'reality' AND action IN ('reality_target_switched', 'reality_config_ended') "
        "ORDER BY id"
    )
    by_action = [(r.action, r.target) for r in logs]
    assert ("reality_config_ended", f"reality:{rows[3].id}") in by_action
    assert {t for a, t in by_action if a == "reality_target_switched"} == {"slot:3", "slot:4"}
    assert ("reality_config_ended", f"reality:{rows[6].id}") not in by_action
    assert any("old_target_failed" in r.reasons for r in logs if r.target == "slot:3")
    assert any("no_active_configs" in r.reasons for r in logs if r.target == "slot:4")
    assert all(str(c.uuid) not in r.reasons for r in logs for c in rows.values())

    # A stale report about the old target changes nothing; the node's check of the new one does.
    await world.report(node, now=datetime.now(UTC), target_ok=False)
    await world.report(working, now=datetime.now(UTC))
    await _loop(world)
    assert [c.slot for c in await _configs(world) if c.status == "active"] == [6]
    for n in (node, expired_node):
        await world.report(n, now=datetime.now(UTC), target_ok=True, checked=NEW_TARGET)
    await _loop(world)
    fresh = [c for c in await _configs(world) if c.status == "active" and c.slot != 6]
    assert sorted(c.slot for c in fresh) == [3, 4]
    assert (await _keys(world))[2] == old6  # still waiting for its config to end
    for c in fresh:
        assert c.host.endswith(zone)
    section = await _section(world, node)
    assert section is not None and [u["id"] for u in section["users"]] == [
        str(c.uuid) for c in fresh if c.slot == 3
    ]
    await world.report(
        node, now=datetime.now(UTC) + timedelta(seconds=30), target_ok=True, checked=NEW_TARGET
    )
    async with world.engine.connect() as conn:
        links = [link for link in await provide_links(conn) if link.status == "active" and link.slot == 3]
    assert (
        len(links) == 1
        and "sni=www.example.org" in links[0].url
        and f"sid={keys[0].short_id}" in links[0].url
    )


async def test_a_working_target_is_switched_once_its_configs_have_ended(world: World) -> None:
    node = await world.node(slot=5)
    await world.report(node, now=T0)
    [c1] = await _create(world, T0)
    await world.report(node, now=T0 + timedelta(hours=12 * F))
    [c2] = await _create(world, T0 + timedelta(hours=12 * F))
    [key] = await _keys(world)
    new = RealitySettings(target_host="www.example.org")

    # The owner changes the target while two configs of the old one are in use (and it works).
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, new)
    for hours in (13, 23):
        now = T0 + timedelta(hours=hours)
        await world.report(node, now=now)
        assert await _retarget(world, now, new) == []
        assert await _create(world, now, new) == []  # no new config on the old target meanwhile
    section = await _section(world, node)
    assert section is not None and section["target"] == "dl.google.com:443" and len(section["users"]) == 2
    async with world.engine.connect() as conn:
        from proxyhub.reality.service import summary

        view = await summary(conn, T0 + timedelta(hours=23 * F))
    [item] = [n for n in view["nodes"] if n["node_id"] == str(node)]
    assert item["target"] == "dl.google.com:443" and item["target_next"] == "www.example.org:443"
    assert item["xray"]["target_ok"] is True

    # The older one ends: still one in use.
    now = T0 + timedelta(hours=24 * F)
    await world.report(node, now=now)
    assert await _end(world, now) == {c1.config_id: "expired"}
    assert await _retarget(world, now, new) == [] and await _create(world, now, new) == []
    # The last one ends: the scope switches (nothing burned), the node checks, configs resume.
    now = T0 + timedelta(hours=36 * F)
    await world.report(node, now=now)
    assert await _end(world, now) == {c2.config_id: "expired"}
    [switched] = await _retarget(world, now, new)
    assert (switched.slot, switched.burned, switched.old_target, switched.new_target) == (
        5,
        (),
        "dl.google.com:443",
        "www.example.org:443",
    )
    [after] = await _keys(world)
    assert (after.id, after.public_key) == (key.id, key.public_key) and after.short_id != key.short_id
    assert await _create(world, now, new) == []  # the node has not checked the new target yet
    async with world.engine.connect() as conn:
        view = await summary(conn, now)
    [item] = [n for n in view["nodes"] if n["node_id"] == str(node)]
    # Its last check (passed) was about the old target: not shown as a result for the new one.
    assert item["target"] == "www.example.org:443" and item["xray"]["target_ok"] is None
    await world.report(node, now=now, target_ok=True, checked=NEW_TARGET)
    [c3] = await _create(world, now, new)
    async with world.engine.connect() as conn:
        view = await summary(conn, now)
    [item] = [n for n in view["nodes"] if n["node_id"] == str(node)]
    assert item["target"] == "www.example.org:443" and item["target_next"] is None
    assert item["xray"]["target_ok"] is True and item["active_configs"] == 1
    assert c3.slot == 5


# --- desired state and the agent API ---------------------------------------------------------------


async def test_desired_state_has_xray_only_for_agents_that_manage_it(world: World) -> None:
    node = await world.node(slot=6)
    seal = load_seal_key(world.files.master_key)
    maker = async_sessionmaker(world.engine, expire_on_commit=False)

    async def doc() -> dict[str, Any]:
        async with maker() as session:
            return await build_desired_state(session, seal, node)

    assert "xray" not in await doc()  # an older agent would refuse the key
    await world.report(node, now=T0)
    assert "xray" not in await doc()  # no key for the scope yet
    await _create(world, T0)
    first = await doc()
    x = first["xray"]
    [c] = await _configs(world)
    [key] = await world.rows("SELECT public_key, short_id FROM reality_keys")
    assert x["port"] == 443 and x["target"] == "dl.google.com:443" and x["server_names"] == ["dl.google.com"]
    assert public_from_private(x["private_key"]) == key.public_key and x["short_ids"] == [key.short_id]
    assert x["users"] == [
        {
            "id": str(c.uuid),
            "email": f"cfg-{c.id}@proxyhub",
            "expires_at": c.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_cap_bytes": CAP,
        }
    ]
    assert first["telemt"] is None  # the Telemt part is untouched
    # A new config changes the ETag; an ended one leaves the users.
    await world.report(node, now=T0 + timedelta(hours=12 * F))
    await _create(world, T0 + timedelta(hours=12 * F))
    second = await doc()
    assert len(second["xray"]["users"]) == 2
    assert etag_for(canonical_json(first)) != etag_for(canonical_json(second))
    await _end(world, T0 + timedelta(hours=24 * F))
    assert len((await doc())["xray"]["users"]) == 1
    # An agent that says it does not manage Xray (no Xray on its image) never gets the key.
    await world.report(node, now=T0 + timedelta(hours=24 * F), enabled=False)
    assert "xray" not in await doc()
    await world.report(node, now=T0 + timedelta(hours=24 * F))
    await world.rows("UPDATE nodes SET state = 'retiring' WHERE id = :n", n=node)
    assert "xray" not in await doc()  # a leaving server stops Xray


async def test_an_unusable_vault_key_leaves_xray_out_but_never_fails_the_document(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    """The desired state also carries Telemt and ad tags: a broken Reality key must not turn the
    whole document into an error (the agent would keep an old Telemt state)."""
    node = await world.node(slot=6)
    await world.report(node, now=T0)
    await _create(world, T0)
    seal = load_seal_key(world.files.master_key)
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    [key] = await _keys(world)
    async with world.engine.connect() as conn:
        private = (await world.vault.get(conn, key.secret_name)).reveal()  # type: ignore[union-attr]

    async def doc() -> dict[str, Any]:
        async with maker() as session:
            return await build_desired_state(session, seal, node)

    assert "xray" in await doc()
    caplog.set_level(logging.ERROR)
    # The stored value no longer authenticates (copied from another row, or tampered with).
    await world.rows(
        "UPDATE secrets SET ciphertext = decode(repeat('00', octet_length(ciphertext)), 'hex') "
        "WHERE name = :n",
        n=key.secret_name,
    )
    broken = await doc()
    assert "xray" not in broken and broken["node_id"] == str(node)
    assert any("VaultDecryptError" in r.getMessage() for r in caplog.records)
    # Gone altogether.
    await world.rows("DELETE FROM secrets WHERE name = :n", n=key.secret_name)
    assert "xray" not in await doc()
    assert any("(missing)" in r.getMessage() for r in caplog.records)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert private not in logged and key.secret_name not in logged


def _csr(node_id: uuid.UUID) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(node_id))]))
        .sign(key, hashes.SHA256())
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def _report(xray: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": 1,
        "agent_version": "0.2.0",
        "applied_etag": None,
        "apply_ok": True,
        "apply_error": None,
        "telemt": {
            "up": False,
            "version": None,
            "uptime_s": None,
            "connections_total": None,
            "connections_bad_total": None,
            "handshake_timeouts_total": None,
            "users": [],
        },
        "system": {
            "uptime_s": 1.0,
            "load1": 0.1,
            "mem_total_bytes": 1,
            "mem_available_bytes": 1,
            "cpu_count": 1,
        },
    }
    if xray is not None:
        body["xray"] = xray
    return body


async def test_agent_api_round_trip(world: World, caplog: pytest.LogCaptureFixture) -> None:
    from proxyhub.api.app import app

    caplog.set_level(logging.DEBUG)
    node = await world.node(slot=7, state="registering")
    maker = async_sessionmaker(world.engine, expire_on_commit=False)
    async with maker() as session:
        token = await issue_registration_token(session, node)
        await session.commit()
    proxy = {PROXY_HEADER: world.files.proxy_secret}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://agent.test") as client:
        reg = await client.post(
            "/agent/v1/register",
            json={"token": token, "csr_pem": _csr(node), "agent_version": "0.2.0", "telemt_version": None},
            headers=proxy,
        )
        assert reg.status_code == 200, reg.text
        cert = x509.load_pem_x509_certificate(reg.json()["cert_pem"].encode())
        auth = {**proxy, FP_HEADER: hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()}
        await world.rows("UPDATE nodes SET state = 'ready' WHERE id = :n", n=node)

        xray = {
            "enabled": True,
            "up": False,
            "version": "26.10.31",
            "uptime_s": None,
            "target_ok": None,
            "checked_target": None,
            "checked_server_name": None,
            "apply_error": None,
            "users": [],
        }
        assert (await client.post("/agent/v1/report", json=_report(xray), headers=auth)).status_code == 204
        [st] = await world.rows(
            "SELECT enabled, up, version FROM reality_node_status WHERE node_id = :n", n=node
        )
        assert (st.enabled, st.up, st.version) == (True, False, "26.10.31")
        # An older agent's report (no xray key) is still accepted.
        assert (await client.post("/agent/v1/report", json=_report(None), headers=auth)).status_code == 204
        bad = {**xray, "users": [{"email": "cfg-1@10.1.2.3", "total_octets": 1}]}
        resp = await client.post("/agent/v1/report", json=_report(bad), headers=auth)
        assert resp.status_code == 422 and resp.json() == {"error": "ip_like_value"}
        # A target check comes with what it was about, which is never an address.
        for wrong in (
            {**xray, "target_ok": True},
            {**xray, "target_ok": True, "checked_target": "dl.google.com:443"},
            {
                **xray,
                "target_ok": False,
                "checked_target": "dl.google.com",
                "checked_server_name": "dl.google.com",
            },
            {
                **xray,
                "target_ok": False,
                "checked_target": "dl.google.com:0",
                "checked_server_name": "dl.google.com",
            },
            {**xray, "checked_target": "dl.google.com:443", "checked_server_name": "dl.google.com"},
        ):
            resp = await client.post("/agent/v1/report", json=_report(wrong), headers=auth)
            assert resp.status_code == 422, wrong
        ip_target = {
            **xray,
            "target_ok": True,
            "checked_target": "142.250.1.1:443",
            "checked_server_name": "x.com",
        }
        resp = await client.post("/agent/v1/report", json=_report(ip_target), headers=auth)
        assert resp.status_code == 422 and resp.json() == {"error": "ip_like_value"}

        # The first round makes the scope's key only: the node has not checked the target yet.
        async with world.engine.begin() as conn:
            await RealityReconciler().run_once(conn)
        assert await _configs(world) == []
        desired = await client.get("/agent/v1/desired", headers=auth)
        assert desired.status_code == 200 and desired.headers["cache-control"] == "no-store"
        x = desired.json()["xray"]
        assert x["users"] == [] and x["target"] == "dl.google.com:443"
        # The agent checks the target it was given and says so; the next round makes the config.
        checked = {
            **xray,
            "up": True,
            "uptime_s": 20.0,
            "target_ok": True,
            "checked_target": x["target"],
            "checked_server_name": x["server_names"][0],
        }
        assert (await client.post("/agent/v1/report", json=_report(checked), headers=auth)).status_code == 204
        [st] = await world.rows(
            "SELECT target_ok, checked_target, checked_server_name FROM reality_node_status "
            "WHERE node_id = :n",
            n=node,
        )
        assert (st.target_ok, st.checked_target, st.checked_server_name) == (
            True,
            "dl.google.com:443",
            "dl.google.com",
        )
        async with world.engine.begin() as conn:
            await RealityReconciler().run_once(conn)
        x = (await client.get("/agent/v1/desired", headers=auth)).json()["xray"]
        [c] = await _configs(world)
        assert [u["id"] for u in x["users"]] == [str(c.uuid)]
        email = x["users"][0]["email"]
        used = {
            **checked,
            "uptime_s": 30.0,
            "users": [{"email": email, "total_octets": 1234}],
        }
        assert (await client.post("/agent/v1/report", json=_report(used), headers=auth)).status_code == 204
        [c] = await _configs(world)
        assert c.used_bytes == 1234
    # The private key and the user id only ever went to the node's own agent: no log line has them.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert x["private_key"] not in logged and str(c.uuid) not in logged


# --- links, dashboard API, the loop --------------------------------------------------------------


async def test_links_provider(world: World) -> None:
    node = await world.node(slot=8, location="hel1")
    now = datetime.now(UTC)
    await world.report(node, now=now - timedelta(minutes=1))
    assert len(await _create(world, now)) == 1
    async with world.engine.connect() as conn:
        assert await provide_links(conn) == []  # not confirmed by the node yet
    # A report after creation confirms only with Xray up and without an apply error.
    for broken in ({"up": False}, {"apply_error": "xray_config_refused"}):
        await world.report(node, now=now + timedelta(seconds=30), **broken)
        async with world.engine.connect() as conn:
            assert await provide_links(conn) == [], broken
    await world.report(node, now=now + timedelta(seconds=10))  # too soon after creation
    async with world.engine.connect() as conn:
        assert await provide_links(conn) == []
    await world.report(node, now=now + timedelta(seconds=30))
    async with world.engine.begin() as conn:
        await save_channel(conn, "main", {"username": "ipoxxo_main", "title": "IPOXXO"})
    async with world.engine.connect() as conn:
        [link] = await provide_links(conn)
    [c] = await _configs(world)
    assert (
        link.kind == "reality"
        and link.key == f"reality:{c.id}"
        and link.slot == 8
        and link.status == "active"
    )
    assert link.url.startswith(
        f"vless://{c.uuid}@203.0.113.10:443?encryption=none&security=reality&sni=dl.google.com"
    )
    assert link.url.endswith("#%40ipoxxo_main%20%F0%9F%87%AB%F0%9F%87%AE%20" + str(c.id))
    assert link.label.startswith("ریلیتی ") and link.expires_at == c.expires_at
    # An ended config stays listed (so its post can be marked), with its status.
    await world.rows("UPDATE reality_configs SET status = 'full', ended_at = now()")
    async with world.engine.connect() as conn:
        [ended] = await provide_links(conn)
    assert ended.status == "full" and ended.key == link.key
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(link_name="My Channel"))
    async with world.engine.connect() as conn:
        assert "#My%20Channel%20" in (await provide_links(conn))[0].url


async def test_dashboard_api(world: World) -> None:
    from proxyhub.api.app import app

    node = await world.node(slot=9)
    await world.report(node, now=datetime.now(UTC))
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)
    [key] = await world.rows("SELECT secret_name FROM reality_keys")
    async with world.engine.connect() as conn:
        private = (await world.vault.get(conn, key.secret_name)).reveal()  # type: ignore[union-attr]
    app.dependency_overrides[require_admin] = lambda: ADMIN
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
        ) as c:
            body = (await c.get("/api/reality")).json()
            assert body["settings"]["cap_hours"] == 48 and body["settings"]["port"] == 443
            assert body["totals"]["active"] == 1 and body["totals"]["nodes_enabled"] == 1
            [cfg] = body["configs"]
            assert (
                cfg["status"] == "active"
                and cfg["url"].startswith("vless://")
                and cfg["remaining_bytes"] == CAP
            )
            assert 0 < cfg["remaining_s"] <= 24 * 3600 * F
            [item] = [n for n in body["nodes"] if n["node_id"] == str(node)]
            assert item["xray"]["enabled"] and item["xray"]["target_ok"] is True
            assert item["target"] == "dl.google.com:443" and item["target_next"] is None
            assert private not in json.dumps(body)
            # An ended config shows no link any more (its UUID leaves the page) and nothing left.
            await world.rows("UPDATE reality_configs SET status = 'expired', ended_at = now()")
            [ended] = (await c.get("/api/reality")).json()["configs"]
            assert ended["status"] == "expired" and ended["url"] is None
            assert ended["remaining_s"] == 0 and ended["remaining_bytes"] == 0
            [row] = await _configs(world)
            assert str(row.uuid) not in json.dumps((await c.get("/api/reality")).json())

            put = await c.put("/api/reality/settings", json={**RealitySettings().model_dump(), "cap_gb": 80})
            assert put.status_code == 200 and put.json()["cap_gb"] == 80
            bad = await c.put("/api/reality/settings", json={"cap_gb": 0})
            assert (
                bad.status_code == 422
                and bad.json()["error"] == "invalid_settings"
                and "cap_gb" in bad.json()["fields"]
            )
            assert "message_fa" in bad.json()
    finally:
        app.dependency_overrides.pop(require_admin, None)
    [log] = await world.rows(
        "SELECT action, reasons->'context' AS context FROM actions_log WHERE target = 'reality'"
    )
    assert log.action == "settings_update" and log.context["changed"]["cap_gb"] == [1000, 80]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF) as c:
        assert (await c.get("/api/reality")).status_code == 401


async def test_loop_logs_without_user_ids_and_alerts_once(world: World) -> None:
    node = await world.node(slot=10)
    await world.report(node, now=datetime.now(UTC))
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)
    [c] = await _configs(world)
    logs = await world.rows(
        "SELECT action, target, CAST(reasons AS text) AS reasons FROM actions_log "
        "WHERE actor = 'reality' ORDER BY id"
    )
    assert [r.action for r in logs] == ["reality_key_created", "reality_config_created"]
    assert logs[1].target == f"reality:{c.id}" and str(c.uuid) not in "".join(r.reasons for r in logs)

    # A failed check of some other target (not this slot's) is no reason to alert the owner.
    await world.report(node, now=datetime.now(UTC), target_ok=False, checked=NEW_TARGET)
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)
    assert await world.rows("SELECT id FROM actions_log WHERE action = 'reality_target_failing'") == []
    # A check result never comes without what it was about.
    async with world.engine.begin() as conn:
        with pytest.raises(ValueError, match="target it was about"):
            await store_xray_report(
                conn, node, enabled=True, up=True, version=None, uptime_s=None, target_ok=True,
                apply_error=None, users=[], now=datetime.now(UTC),
            )  # fmt: skip
        with pytest.raises(ValueError, match="target it was about"):
            await store_xray_report(
                conn, node, enabled=True, up=True, version=None, uptime_s=None, target_ok=None,
                apply_error=None, users=[], now=datetime.now(UTC), checked=NEW_TARGET,
            )  # fmt: skip

    # The target fails from the node: one owner alert a day, and no new config there.
    await world.report(node, now=datetime.now(UTC), target_ok=False)
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)
    failing = await world.rows(
        "SELECT reasons->'context' AS context FROM actions_log WHERE action = 'reality_target_failing'"
    )
    assert len(failing) == 1


# --- the real agent on the panel's document -------------------------------------------------------


def _go_panel_document(node: uuid.UUID, body: bytes, path: Path, users: int) -> None:
    """Run the Go agent's TestPanelDocument on a document the real panel built."""
    path.write_bytes(body)
    path.chmod(0o600)
    # PROXYHUB_TEST_XRAY (when set) passes through: then the pinned binary checks the config too.
    result = run_tool(
        "env",
        f"PROXYHUB_TEST_DESIRED_DOC={path}",
        f"PROXYHUB_TEST_NODE_ID={node}",
        f"PROXYHUB_TEST_EXPECT_USERS={users}",
        "go",
        "-C",
        AGENT_DIR,
        "test",
        "-count=1",
        "-run",
        "^TestPanelDocument$",
        "-v",
        "./internal/xray/",
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    want = "accepted by the pinned Xray" if os.environ.get("PROXYHUB_TEST_XRAY") else "accepted and rendered"
    assert want in result.stdout and "--- PASS: TestPanelDocument" in result.stdout
    private = json.loads(body)["xray"]["private_key"]
    assert private not in result.stdout + result.stderr
    assert len(base64.urlsafe_b64decode(private + "=")) == 32


@requires_go
async def test_the_go_agent_accepts_and_renders_the_panels_document(world: World, tmp_path: Path) -> None:
    node = await world.node(slot=20)
    seal = load_seal_key(world.files.master_key)

    async def body() -> bytes:
        async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
            return canonical_json(await build_desired_state(session, seal, node))

    # A new scope: the key without any user yet, so the node can check the target first.
    await world.report(node, now=T0, target_ok=None)
    await _create(world, T0)
    _go_panel_document(node, await body(), tmp_path / "key-only.json", 0)
    # Then two configs.
    await world.report(node, now=T0)
    await _create(world, T0)
    await world.report(node, now=T0 + timedelta(hours=12 * F))
    await _create(world, T0 + timedelta(hours=12 * F))
    _go_panel_document(node, await body(), tmp_path / "desired.json", 2)
