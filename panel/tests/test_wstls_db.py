"""VLESS + WebSocket + TLS (transport ws_tls) on the real database, the real vault and the real agent.

A node gets the ws_tls inbound only with its own DNS name in Cloudflare, a valid certificate for the
active domain, and an agent that knows the transport; a config is made only where the name leads to
that node and its agent checked its own TLS inbound for the name; links and posts follow; MTProto's
fake-TLS domain becomes the node's name. The Go agent and the pinned Xray accept the document the
panel builds (with PROXYHUB_TEST_XRAY set).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_support import AGENT_DIR, requires_go, run_tool
from test_fleet_support import ADMIN, CSRF
from test_reality_support import World
from test_wstls_support import agent, make_cert, name_record, store_cert, ws_world_fixture, zone  # noqa: F401

from proxyhub.agent_api.desired import build_desired_state, canonical_json
from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.api.deps import require_admin
from proxyhub.cloudflare.service import desired_slot_records
from proxyhub.mtproto.policy import apply_policy
from proxyhub.mtproto.service import list_proxies
from proxyhub.mtproto.settings import MtprotoSettings
from proxyhub.reality.links import CONFIRM_AFTER, provide_links
from proxyhub.reality.policy import create_configs, end_configs, ensure_keys
from proxyhub.reality.settings import RealitySettings, save_reality_settings
from proxyhub.reality.store import xray_section
from proxyhub.reconcilers.reality import RealityReconciler

pytestmark = pytest.mark.db

WS = RealitySettings()  # the default: ws_tls
IP = "203.0.113.10"


async def _section(world: World, node_id: uuid.UUID) -> dict[str, Any] | None:
    async with world.engine.connect() as conn:
        return await xray_section(conn, node_id, world.vault)


async def _create(world: World, now: datetime, settings: RealitySettings = WS) -> list[Any]:
    async with world.engine.begin() as conn:
        await ensure_keys(conn, settings, now)
        return await create_configs(conn, settings, now)


async def _configs(world: World) -> list[Any]:
    return await world.rows(
        "SELECT id, node_id, slot, uuid, host, status, transport, created_at FROM reality_configs ORDER BY id"
    )


async def _serving_slot_node(world: World, slot: int = 3) -> tuple[uuid.UUID, str, str, Any]:
    """A ready slot node whose agent (0.3.0) manages Xray, an active zone with the slot's name
    pointing at it, and a valid wildcard certificate for the zone. Returns (node, zone id, name,
    certificate)."""
    node = await world.node(slot=slot, ipv4=IP)
    zone_id, zone_name = await zone(world, f"ws{slot}")
    name = f"slot{slot}.{zone_name}"
    await name_record(world, zone_id, name, IP, node)
    cert = make_cert(f"*.{zone_name}")
    await store_cert(world, zone_name, zone_id, cert)
    await agent(world, node, "0.3.0")
    await world.report(node, target_ok=None)
    return node, zone_id, name, cert


async def test_the_ws_tls_section_needs_a_name_a_certificate_and_a_new_agent(ws_world: World) -> None:
    world = ws_world
    node = await world.node(slot=3, ipv4=IP)
    await world.report(node, target_ok=None)
    await _create(world, datetime.now(UTC))  # the scope's key (with its WebSocket path)
    [key] = await world.rows("SELECT ws_path FROM reality_keys")
    assert re.fullmatch(r"/[0-9a-f]{16}", key.ws_path)
    assert await _section(world, node) is None  # no name yet
    zone_id, zone_name = await zone(world, "need")
    name = f"slot3.{zone_name}"
    await name_record(world, zone_id, name, IP, node)
    assert await _section(world, node) is None  # no certificate yet
    cert = make_cert(f"*.{zone_name}")
    await store_cert(world, zone_name, zone_id, cert)
    assert await _section(world, node) is None  # an agent that may not know ws_tls
    await agent(world, node, "0.2.0")
    assert await _section(world, node) is None  # 0.2.0 would refuse the whole document
    await agent(world, node, "0.3.0")
    section = await _section(world, node)
    assert section is not None
    assert section == {
        "transport": "ws_tls",
        "port": 443,
        "server_name": name,
        "ws_path": key.ws_path,
        "tls_cert_chain": cert.chain_pem,
        "tls_key": cert.key_pem.reveal(),
        "users": [],
    }
    # An expiring certificate is not handed out any more (the loop renews long before).
    await world.rows("UPDATE acme_certificates SET not_after = now() + interval '10 minutes'")
    assert await _section(world, node) is None
    await world.rows("UPDATE acme_certificates SET not_after = :t", t=cert.not_after)
    # Back to REALITY: the old shape, without any ws_tls field (agents before 0.3.0 know it).
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(transport="reality"))
    reality = await _section(world, node)
    assert reality is not None and set(reality) == {
        "port",
        "private_key",
        "target",
        "server_names",
        "short_ids",
        "users",
    }


async def test_configs_and_links_follow_the_nodes_own_name_and_check(ws_world: World) -> None:
    world = ws_world
    node, _, name, _ = await _serving_slot_node(world)
    now = datetime.now(UTC)
    assert await _create(world, now) == []  # its agent has not checked its own inbound yet
    # A check about another name (or a failed one) does not count.
    await world.report(
        node, now=now, target_ok=True, checked=("slot9.other.example:443", "slot9.other.example")
    )
    assert await _create(world, now) == []
    await world.report(node, now=now, target_ok=False, checked=(f"{name}:443", name))
    assert await _create(world, now) == []
    await world.report(node, now=now, target_ok=True, checked=(f"{name}:443", name))
    [made] = await _create(world, now)
    assert made.host == name
    [c] = await _configs(world)
    assert (c.transport, c.host, c.slot) == ("ws_tls", name, 3)
    section = await _section(world, node)
    assert section is not None and [u["email"] for u in section["users"]] == [f"cfg-{c.id}@proxyhub"]
    async with world.engine.connect() as conn:
        assert await provide_links(conn) == []  # not confirmed by a report after it was made
    await world.report(node, now=c.created_at + CONFIRM_AFTER, target_ok=True, checked=(f"{name}:443", name))
    async with world.engine.connect() as conn:
        [link] = await provide_links(conn)
    [key] = await world.rows("SELECT ws_path FROM reality_keys")
    path = key.ws_path.replace("/", "%2F")
    assert link.url.startswith(
        f"vless://{c.uuid}@{name}:443?encryption=none&security=tls&sni={name}&alpn=http%2F1.1&fp=chrome"
        f"&type=ws&host={name}&path={path}#%40ipoxxo%20"
    )
    assert link.kind == "reality" and link.label.startswith("کانفیگ ") and link.status == "active"
    # The node's own check fails later: the link is not handed to new posts any more.
    await world.report(
        node, now=c.created_at + 2 * CONFIRM_AFTER, target_ok=False, checked=(f"{name}:443", name)
    )
    async with world.engine.connect() as conn:
        assert await provide_links(conn) == []


async def test_a_test_node_gets_its_own_name_and_configs(ws_world: World) -> None:
    world = ws_world
    node = await world.node(slot=None, ipv4="203.0.113.77")
    await world.rows("UPDATE nodes SET is_test = true WHERE id = :n", n=node)
    zone_id, zone_name = await zone(world, "test")
    name = f"t{node.hex[:8]}.{zone_name}"
    async with world.engine.connect() as conn:
        desired = await desired_slot_records(conn, zone_name)
    [rec] = [d for d in desired if d.node_id == node]
    assert (rec.name, rec.content, rec.purpose) == (name, "203.0.113.77", "slot")
    # Not before its first ready, and not once it leaves service.
    await world.rows("UPDATE nodes SET ready_at = NULL WHERE id = :n", n=node)
    async with world.engine.connect() as conn:
        assert [d for d in await desired_slot_records(conn, zone_name) if d.node_id == node] == []
    await world.rows("UPDATE nodes SET ready_at = now(), state = 'retiring' WHERE id = :n", n=node)
    async with world.engine.connect() as conn:
        assert [d for d in await desired_slot_records(conn, zone_name) if d.node_id == node] == []
    await world.rows("UPDATE nodes SET state = 'ready' WHERE id = :n", n=node)

    await name_record(world, zone_id, name, "203.0.113.77", node)
    await store_cert(world, zone_name, zone_id, make_cert(f"*.{zone_name}"))
    await agent(world, node)
    now = datetime.now(UTC)
    await world.report(node, now=now, target_ok=True, checked=(f"{name}:443", name))
    [made] = await _create(world, now)
    assert made.host == name and made.slot is None
    section = await _section(world, node)
    assert section is not None and section["server_name"] == name
    # The domain rotates: the name is no longer the active domain's, so the config is burned.
    await world.rows("UPDATE cf_zones SET pool_state = 'filtered' WHERE id = :z", z=zone_id)
    async with world.engine.begin() as conn:
        assert await end_configs(conn, now, "ws_tls") == {made.config_id: "burned"}


async def test_switching_the_transport_burns_the_other_transports_configs(ws_world: World) -> None:
    world = ws_world
    node, _, name, _ = await _serving_slot_node(world)
    now = datetime.now(UTC)
    await world.report(node, now=now, target_ok=True, checked=(f"{name}:443", name))
    [made] = await _create(world, now)
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(transport="reality"))
    async with world.engine.begin() as conn:
        await RealityReconciler().run_once(conn)
    [c] = await _configs(world)
    assert c.id == made.config_id and c.status == "burned"
    ended = await world.rows(
        "SELECT reasons FROM actions_log WHERE action = 'reality_config_ended' AND target = :t",
        t=f"reality:{c.id}",
    )
    assert ended and "burned" in json.dumps(ended[0].reasons)
    section = await _section(world, node)
    assert section is not None and "transport" not in section and section["users"] == []


async def test_mtproto_fake_tls_domain_follows_the_nodes_name(ws_world: World) -> None:
    world = ws_world
    seal = load_seal_key(world.files.master_key)
    settings = MtprotoSettings()
    node = await world.node(slot=3, ipv4=IP)

    async def policy() -> None:
        async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
            await apply_policy(session, seal_key=seal, settings=settings, now=datetime.now(UTC))
            await session.commit()

    async def domain() -> str:
        [row] = await world.rows("SELECT tls_domain FROM telemt_node_configs WHERE node_id = :n", n=node)
        return str(row.tls_domain)

    await policy()
    assert await domain() == "www.microsoft.com"  # the owner's setting: no name, no certificate yet
    await world.report(node, target_ok=None)
    zone_id, zone_name = await zone(world, "mt")
    name = f"slot3.{zone_name}"
    await name_record(world, zone_id, name, IP, node)
    await store_cert(world, zone_name, zone_id, make_cert(f"*.{zone_name}"))
    await agent(world, node)
    await _create(world, datetime.now(UTC))  # the scope's key: the node now serves ws_tls
    await policy()
    assert await domain() == name
    await world.rows("UPDATE mtproto_proxies SET live_at = now() WHERE node_id = :n", n=node)
    async with world.engine.connect() as conn:
        [proxy] = await list_proxies(conn, seal, datetime.now(UTC))
    assert proxy.host == name and proxy.tls_domain == name and proxy.url is not None
    assert f"server={name}" in proxy.url and name.encode().hex() in proxy.url  # ee<secret><domain hex>
    # The name moves to another server (its record no longer leads here): back to the setting.
    await world.rows("UPDATE cf_records SET content = '203.0.113.99' WHERE name = :n", n=name)
    await policy()
    assert await domain() == "www.microsoft.com"
    # REALITY on 443 is no TLS front for our own name either.
    await world.rows("UPDATE cf_records SET content = :ip WHERE name = :n", ip=IP, n=name)
    await policy()
    assert await domain() == name
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(transport="reality"))
    await policy()
    assert await domain() == "www.microsoft.com"


async def test_the_dashboard_shows_transport_names_and_certificate(ws_world: World) -> None:
    from proxyhub.api.app import app

    world = ws_world
    node, _, name, cert = await _serving_slot_node(world, slot=5)
    await world.report(node, target_ok=True, checked=(f"{name}:443", name))
    await _create(world, datetime.now(UTC))
    app.dependency_overrides[require_admin] = lambda: ADMIN
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
        ) as c:
            body = (await c.get("/api/reality")).json()
            assert body["settings"]["transport"] == "ws_tls" and body["defaults"]["transport"] == "ws_tls"
            [item] = [n for n in body["nodes"] if n["node_id"] == str(node)]
            assert item["dns_name"] == name and item["dns_own"] is True and item["agent_ws_tls"] is True
            assert item["xray"]["target_ok"] is True and item["target_next"] is None
            assert re.fullmatch(r"/[0-9a-f]{16}", item["ws_path"])
            zone_name = name.split(".", 1)[1]
            assert body["certificate"]["domain"] == zone_name and body["certificate"]["valid"] is True
            assert body["certificate"]["names"] == [f"*.{zone_name}"]
            [cfg] = body["configs"]
            assert cfg["transport"] == "ws_tls" and cfg["host"] == name and "type=ws" in cfg["url"]
            text_body = json.dumps(body)
            assert cert.key_pem.reveal().splitlines()[1] not in text_body and "PRIVATE KEY" not in text_body
            put = await c.put(
                "/api/reality/settings", json={**RealitySettings().model_dump(), "transport": "reality"}
            )
            assert put.status_code == 200 and put.json()["transport"] == "reality"
            bad = await c.put(
                "/api/reality/settings", json={**RealitySettings().model_dump(), "transport": "x"}
            )
            assert bad.status_code == 422 and "transport" in bad.json()["fields"]
    finally:
        app.dependency_overrides.pop(require_admin, None)
    [log] = await world.rows("SELECT reasons->'context' AS context FROM actions_log WHERE target = 'reality'")
    assert log.context["changed"]["transport"] == ["ws_tls", "reality"]


@requires_go
async def test_the_go_agent_and_xray_accept_the_panels_ws_tls_document(
    ws_world: World, tmp_path: Path
) -> None:
    world = ws_world
    node, _, name, cert = await _serving_slot_node(world, slot=6)
    now = datetime.now(UTC)
    await world.report(node, now=now, target_ok=True, checked=(f"{name}:443", name))
    assert len(await _create(world, now)) == 1
    seal = load_seal_key(world.files.master_key)
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        body = canonical_json(await build_desired_state(session, seal, node))
    doc = json.loads(body)
    assert doc["xray"]["transport"] == "ws_tls" and doc["xray"]["server_name"] == name
    path = tmp_path / "desired.json"
    path.write_bytes(body)
    path.chmod(0o600)
    result = await asyncio.to_thread(
        run_tool,
        "env",
        f"PROXYHUB_TEST_DESIRED_DOC={path}",
        f"PROXYHUB_TEST_NODE_ID={node}",
        "PROXYHUB_TEST_EXPECT_USERS=1",
        "PROXYHUB_TEST_EXPECT_TRANSPORT=ws_tls",
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
    assert want in result.stdout
    key_line = cert.key_pem.reveal().splitlines()[1]
    assert key_line not in result.stdout + result.stderr


async def test_migration_0018_backfills_paths_and_keeps_old_configs_reality(migrated_db: str) -> None:
    from alembic import command

    from proxyhub.cli import _alembic_config, downgrade, migrate

    engine = create_async_engine(migrated_db)
    try:
        await asyncio.to_thread(downgrade, "0017_merge")
        node, account = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                    "VALUES (:a, 'wstls-mig-acc', 'wstls-mig-token', 'active')"
                ),
                {"a": account},
            )
            await conn.execute(
                text(
                    "INSERT INTO nodes (id, account_id, name, location, server_type, state, is_test) "
                    "VALUES (:n, :a, 'wstls-mig-node', 'fsn1', 'cpx02', 'ready', false)"
                ),
                {"n": node, "a": account},
            )
            for slot in (41, 42):
                await conn.execute(
                    text(
                        "INSERT INTO reality_keys (slot, public_key, short_id, target, server_name, "
                        "secret_name) VALUES (:s, :pk, '0123456789abcdef', 'dl.google.com:443', "
                        "'dl.google.com', :sn)"
                    ),
                    {"s": slot, "pk": "A" * 43, "sn": f"reality:key:slot:{slot}"},
                )
            await conn.execute(
                text(
                    "INSERT INTO reality_configs (node_id, slot, key_id, uuid, host, created_at, expires_at, "
                    "data_cap_bytes, status) SELECT :n, 41, id, :u, 'slot41.example', now(), "
                    "now() + interval '1 day', 1000, 'active' FROM reality_keys WHERE slot = 41"
                ),
                {"n": node, "u": uuid.uuid4()},
            )
        await asyncio.to_thread(migrate, "head")
        async with engine.connect() as conn:
            paths = (
                (await conn.execute(text("SELECT ws_path FROM reality_keys ORDER BY slot"))).scalars().all()
            )
            transport = (await conn.execute(text("SELECT transport FROM reality_configs"))).scalar_one()
        assert len(paths) == 2 and paths[0] != paths[1]
        assert all(re.fullmatch(r"/[0-9a-f]{16}", p) for p in paths) and transport == "reality"
        async with engine.begin() as conn:
            with pytest.raises(Exception, match="ck_reality_keys_ws_path"):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE reality_keys SET ws_path = '/'"))
            await conn.execute(
                text(
                    "INSERT INTO cf_zones (id, name, status, pool_state, seen_at) "
                    "VALUES (:z, 'wstls-mig.example', 'active', 'none', now())"
                ),
                {"z": "e" * 32},
            )
            await conn.execute(
                text(
                    "INSERT INTO cf_records (id, zone_id, name, type, content, ttl, proxied, purpose) "
                    "VALUES (:i, :z, '_acme-challenge.wstls-mig.example', 'TXT', 'v', 60, false, 'acme')"
                ),
                {"i": uuid.uuid4(), "z": "e" * 32},
            )
        await asyncio.to_thread(command.check, _alembic_config())  # models and migrations agree
        # Down again: the acme rows go with the purpose they need; up again works.
        await asyncio.to_thread(downgrade, "0017_merge")
        await asyncio.to_thread(migrate, "head")
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM cf_records WHERE zone_id = :z"), {"z": "e" * 32})
            await conn.execute(text("DELETE FROM cf_zones WHERE id = :z"), {"z": "e" * 32})
            await conn.execute(text("DELETE FROM reality_configs WHERE slot = 41"))
            await conn.execute(text("DELETE FROM reality_keys WHERE slot IN (41, 42)"))
            await conn.execute(text("DELETE FROM nodes WHERE name = 'wstls-mig-node'"))
            await conn.execute(text("DELETE FROM hetzner_accounts WHERE name = 'wstls-mig-acc'"))
        await engine.dispose()
