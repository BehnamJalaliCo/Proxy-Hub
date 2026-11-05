"""XHTTP on a real PostgreSQL with the real vault: VLESS + XHTTP + REALITY next to every REALITY
inbound (proxyhub/reality/settings.py ``xhttp_enabled``). The desired state asks only an agent that
knows it (0.4.0 on), only with transport ``reality`` and when Telemt leaves the port free; a config
gets its XHTTP link once its node reports the inbound; posts carry both links of a config as one;
the dashboard shows both; the Go agent accepts and renders the panel's document (and the pinned
Xray accepts the result when PROXYHUB_TEST_XRAY is set).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_agent_support import AGENT_DIR, requires_go, run_tool
from test_fleet_support import ADMIN, CSRF
from test_reality_support import World, world_fixture  # noqa: F401 - the `world` fixture
from test_wstls_support import agent

from proxyhub.agent_api.desired import build_desired_state, canonical_json
from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.api.deps import require_admin
from proxyhub.links.contract import ProxyLink
from proxyhub.posts import links as post_links
from proxyhub.reality.links import provide_links
from proxyhub.reality.policy import create_configs, ensure_keys
from proxyhub.reality.settings import XHTTP_PORT, RealitySettings, save_reality_settings
from proxyhub.reality.store import xray_section

pytestmark = pytest.mark.db

PATH_RE = re.compile(r"^/[0-9a-f]{16}$")


def settings(**kw: Any) -> RealitySettings:
    return RealitySettings(**{"transport": "reality", **kw})


async def _save(world: World, s: RealitySettings) -> None:
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, s)


async def _ready_node(world: World, *, slot: int, version: str = "0.4.0") -> uuid.UUID:
    """A node whose agent (``version``) reported, with its scope's key and one config."""
    node = await world.node(slot=slot, location="hel1")
    await agent(world, node, version)
    now = datetime.now(UTC)
    await world.report(node, now=now - timedelta(minutes=1))
    async with world.engine.begin() as conn:
        await ensure_keys(conn, settings(), now)
        made = await create_configs(conn, settings(), now)
    assert len(made) == 1
    return node


async def _section(world: World, node: uuid.UUID) -> dict[str, Any] | None:
    async with world.engine.connect() as conn:
        return await xray_section(conn, node, world.vault)


async def _links(world: World) -> list[ProxyLink]:
    async with world.engine.connect() as conn:
        return await provide_links(conn)


async def test_every_key_gets_its_own_xhttp_path_from_the_database(world: World) -> None:
    a = await _ready_node(world, slot=31)
    b = await _ready_node(world, slot=32)
    rows = await world.rows("SELECT slot, ws_path, xhttp_path FROM reality_keys ORDER BY slot")
    assert [r.slot for r in rows] == [31, 32]
    assert all(PATH_RE.fullmatch(r.xhttp_path) for r in rows)
    assert rows[0].xhttp_path != rows[1].xhttp_path  # random per key
    assert all(r.xhttp_path != r.ws_path for r in rows)  # not the WebSocket path
    with pytest.raises(Exception, match="xhttp_path"):  # the check constraint
        await world.rows("UPDATE reality_keys SET xhttp_path = '/' WHERE slot = 31")
    assert a != b


async def test_the_xhttp_part_only_for_agents_that_know_it(world: World) -> None:
    old = await _ready_node(world, slot=33, version="0.3.0")
    new = await _ready_node(world, slot=34)
    [old_section, new_section] = [await _section(world, n) for n in (old, new)]
    assert old_section is not None and "xhttp" not in old_section  # 0.3.0 would refuse the key
    assert new_section is not None
    [key] = await world.rows("SELECT xhttp_path FROM reality_keys WHERE slot = 34")
    assert new_section["xhttp"] == {"port": XHTTP_PORT, "path": key.xhttp_path} and XHTTP_PORT == 2083
    assert new_section["port"] == 443 and "transport" not in new_section  # the REALITY shape
    # The owner's switch.
    await _save(world, settings(xhttp_enabled=False))
    section = await _section(world, new)
    assert section is not None and "xhttp" not in section
    await _save(world, settings())
    # Telemt on the XHTTP port: only the XHTTP part goes (the agent would refuse the clash).
    await world.rows(
        "INSERT INTO telemt_node_configs (node_id, ports, tls_domain, mode_classic, mode_secure, mode_tls) "
        "VALUES (:n, ARRAY[2083], 'cdn.example.com', false, false, true)",
        n=new,
    )
    section = await _section(world, new)
    assert section is not None and "xhttp" not in section and section["users"]


async def test_xhttp_links_follow_the_nodes_report(world: World) -> None:
    node = await _ready_node(world, slot=35)
    now = datetime.now(UTC)
    await world.report(node, now=now + timedelta(seconds=30))
    [tcp] = await _links(world)  # the node does not run the XHTTP inbound yet
    [c] = await world.rows(
        "SELECT c.id, c.uuid, c.host, k.public_key, k.short_id, k.xhttp_path FROM reality_configs c "
        "JOIN reality_keys k ON k.id = c.key_id"
    )
    assert tcp.key == f"reality:{c.id}" and tcp.label == f"کانفیگ {fa(c.id)}" and tcp.companion_of is None

    await world.report(node, now=now + timedelta(seconds=40), xhttp_port=XHTTP_PORT)
    first, second = await _links(world)
    assert first.key == tcp.key and first.url == tcp.url and first.label == f"کانفیگ {fa(c.id)} (TCP)"
    assert second.key == f"reality:{c.id}:xhttp" and second.companion_of == first.key
    assert second.label == f"کانفیگ {fa(c.id)} (XHTTP)"
    assert (second.slot, second.expires_at, second.status) == (first.slot, first.expires_at, "active")
    parts = urlsplit(second.url)
    assert parts.scheme == "vless" and parts.username == str(c.uuid) and parts.port == XHTTP_PORT
    assert parts.hostname == c.host
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "encryption": "none",
        "security": "reality",
        "sni": "dl.google.com",
        "fp": "chrome",
        "pbk": c.public_key,
        "sid": c.short_id,
        "type": "xhttp",
        "path": c.xhttp_path,
        "mode": "auto",
    }
    assert "flow" not in q and parts.fragment.endswith(f"%20{c.id}%20XHTTP")
    # The links pass the posts' checks, and a new post counts the config once, both links together.
    harvest = post_links.Harvest(links={x.key: x for x in [first, second]})
    assert post_links.checked(second) == second
    chosen = post_links.for_new_post(harvest, now, max_mtproto=0, max_reality=1)
    assert [x.key for x in chosen] == [first.key, second.key]

    # A report without the inbound (an apply error, or Xray down) takes the XHTTP link away.
    await world.report(node, now=now + timedelta(seconds=50), xhttp_port=XHTTP_PORT, up=False)
    assert await _links(world) == []
    await world.report(node, now=now + timedelta(seconds=60))
    assert [x.key for x in await _links(world)] == [tcp.key]
    # The owner turns XHTTP off: only the TCP link, with its plain label.
    await world.report(node, now=now + timedelta(seconds=70), xhttp_port=XHTTP_PORT)
    await _save(world, settings(xhttp_enabled=False))
    [only] = await _links(world)
    assert only.key == tcp.key and only.label == tcp.label
    # An ended config lists both links with its status, so posts carrying either get marked.
    await world.rows("UPDATE reality_configs SET status = 'full', ended_at = now()")
    ended = await _links(world)
    assert [(x.key, x.status) for x in ended] == [(tcp.key, "full"), (second.key, "full")]


async def test_dashboard_shows_both_links(world: World) -> None:
    from proxyhub.api.app import app

    node = await _ready_node(world, slot=36)
    await world.report(node, now=datetime.now(UTC) + timedelta(seconds=30), xhttp_port=XHTTP_PORT)
    app.dependency_overrides[require_admin] = lambda: ADMIN
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
        ) as c:
            body = (await c.get("/api/reality")).json()
            assert body["settings"]["xhttp_enabled"] is True and body["settings"]["xhttp_port"] == 2083
            [cfg] = body["configs"]
            assert cfg["listed"] is True and cfg["url"].startswith("vless://")
            x = cfg["xhttp"]
            assert x["key"] == f"{cfg['key']}:xhttp" and x["listed"] is True
            assert ":2083?" in x["url"] and "type=xhttp" in x["url"]
            [item] = [n for n in body["nodes"] if n["node_id"] == str(node)]
            assert item["agent_xhttp"] is True and item["xray"]["xhttp_port"] == 2083
            put = await c.put(
                "/api/reality/settings", json={**settings().model_dump(), "xhttp_enabled": False}
            )
            assert put.status_code == 200 and put.json()["xhttp_enabled"] is False
            [cfg] = (await c.get("/api/reality")).json()["configs"]
            assert cfg["xhttp"] is None
            # An old dashboard that does not know the field keeps working (default on).
            legacy = {k: v for k, v in settings().model_dump().items() if k != "xhttp_enabled"}
            assert (await c.put("/api/reality/settings", json=legacy)).json()["xhttp_enabled"] is True
            # Ended: no link (its UUID leaves the page), the XHTTP entry stays for the record.
            await world.rows("UPDATE reality_configs SET status = 'expired', ended_at = now()")
            [ended] = (await c.get("/api/reality")).json()["configs"]
            assert ended["url"] is None and ended["xhttp"]["url"] is None
            [row] = await world.rows("SELECT uuid FROM reality_configs")
            assert str(row.uuid) not in json.dumps((await c.get("/api/reality")).json())
    finally:
        app.dependency_overrides.pop(require_admin, None)


@requires_go
async def test_the_go_agent_renders_the_panels_xhttp_document(world: World, tmp_path: Path) -> None:
    node = await _ready_node(world, slot=37)
    seal = load_seal_key(world.files.master_key)
    async with async_sessionmaker(world.engine, expire_on_commit=False)() as session:
        body = canonical_json(await build_desired_state(session, seal, node))
    assert json.loads(body)["xray"]["xhttp"]["port"] == XHTTP_PORT
    path = tmp_path / "desired.json"
    path.write_bytes(body)
    path.chmod(0o600)
    result = run_tool(
        "env",
        f"PROXYHUB_TEST_DESIRED_DOC={path}",
        f"PROXYHUB_TEST_NODE_ID={node}",
        "PROXYHUB_TEST_EXPECT_USERS=1",
        "PROXYHUB_TEST_EXPECT_XHTTP=1",
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
    assert json.loads(body)["xray"]["private_key"] not in result.stdout + result.stderr


def fa(n: int) -> str:
    return str(n).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
