"""/api/mtproto over real HTTP (ASGI transport) with a real PostgreSQL (and Redis for the app's
lifespan). Owner-only routes, the response contract, settings validation, and rule 3: the proxy
secret leaves the panel only inside the public link of the GET response, never in an error, a log
line or a database row.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_agent_support import reset_config_caches, use_secret_files, write_secret_files
from test_fleet_support import ADMIN, CSRF
from test_mtproto_support import (
    ZONE_NAME,
    actions,
    agent_reports,
    all_table_text,
    clean,
    current_etag,
    make_active_zone,
    make_node,
    make_slot_record,
    register_agent,
    rows,
    session_maker,
)
from test_telegram_support import all_logs

from proxyhub.agent_api.desired import reveal_secret
from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.api.deps import require_admin
from proxyhub.mtproto.urls import link_secret, proxy_key, proxy_url, user_name_for
from proxyhub.reconcilers.mtproto import MtprotoReconciler

pytestmark = [pytest.mark.db, pytest.mark.redis]

BASE = "/api/mtproto"
PATHS = {("GET", BASE), ("PUT", f"{BASE}/settings")}
DEFAULTS = {
    "port": 8443,
    "tls_domain": "www.microsoft.com",
    "mode_tls": True,
    "mode_secure": False,
    "mode_classic": False,
    "link_mode": "tls",
}


@pytest.fixture
async def engine(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncEngine]:
    use_secret_files(monkeypatch, write_secret_files(tmp_path / "secrets"))
    eng = create_async_engine(migrated_db)
    await clean(eng)
    try:
        yield eng
    finally:
        await clean(eng)
        await eng.dispose()
        reset_config_caches()


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    try:
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        app.dependency_overrides.pop(require_admin, None)


def assert_error(r: Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    body = r.json()
    assert body["error"] == code and body["message_fa"]
    assert r.headers["cache-control"] == "no-store"
    return dict(body)


async def seal_key() -> bytes:
    from proxyhub.config import get_config

    return load_seal_key(get_config().master_key_file)


async def live_node(engine: AsyncEngine, **kw: Any) -> uuid.UUID:
    node = await make_node(engine, **kw)
    await MtprotoReconciler().try_run_once(engine)
    await register_agent(engine, node)
    await agent_reports(
        engine,
        node,
        applied_etag=await current_etag(engine, await seal_key(), node),
        users=[{"name": user_name_for(node), "current_connections": 17, "total_octets": 123456}],
    )
    await MtprotoReconciler().try_run_once(engine)
    return node


async def secret_of(engine: AsyncEngine, node: uuid.UUID) -> str:
    async with session_maker(engine)() as s:
        return await reveal_secret(s, await seal_key(), node, user_name_for(node))


async def test_every_route_requires_the_owner(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_mtproto
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_mtproto.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            found.add((method, route.path))
            assert require_admin in {d.call for d in route.dependant.dependencies}, route.path
    assert found == PATHS
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(PATHS):
                assert_error(await c.request(method, path, json={}), 401, "unauthenticated")
                if method != "GET":  # require_admin checks CSRF first
                    r = await c.request(method, path, json={}, headers={"X-Requested-With": "nope"})
                    assert_error(r, 403, "csrf")


async def test_fresh_panel_shows_defaults_and_no_proxy(client: AsyncClient) -> None:
    r = await client.get(BASE)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "settings": DEFAULTS,
        "settings_corrupt": False,
        "defaults": DEFAULTS,
        "active_domain": None,
        "proxies": [],
    }


async def test_proxies_list_contract(client: AsyncClient, engine: AsyncEngine) -> None:
    await make_active_zone(engine)
    one = await live_node(engine, slot=1, ipv4="203.0.113.21")
    await make_slot_record(engine, one, 1, "203.0.113.21")
    pending = await make_node(engine, slot=2, ipv4="203.0.113.22")
    await MtprotoReconciler().try_run_once(engine)
    r = await client.get(BASE)
    assert r.status_code == 200
    body = r.json()
    assert body["active_domain"] == ZONE_NAME
    first, second = body["proxies"]
    secret = await secret_of(engine, one)
    assert first == {
        "key": proxy_key(one, user_name_for(one)),
        "node_id": str(one),
        "node_name": f"mtp-test-{one.hex[:8]}",
        "node_state": "ready",
        "is_test": False,
        "slot": 1,
        "label": "پروکسی ۱",
        "host": f"slot1.{ZONE_NAME}",
        "host_kind": "domain",
        "port": 8443,
        "mode": "tls",
        "tls_domain": "www.microsoft.com",
        "link": proxy_url(f"slot1.{ZONE_NAME}", 8443, secret, "tls", "www.microsoft.com"),
        "status": "active",
        "connections": 17,
        "total_octets": 123456,
        "stats_at": first["stats_at"],
        "live_at": first["live_at"],
        "sponsor_tag_set": False,
    }
    assert first["stats_at"].endswith("Z") and first["live_at"].endswith("Z")
    assert second["node_id"] == str(pending) and second["status"] == "pending"
    assert second["live_at"] is None and second["connections"] is None
    assert second["host"] == "203.0.113.22" and second["host_kind"] == "ip"


async def test_put_settings_saves_logs_and_the_loop_applies_them(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    node = await live_node(engine)
    secret = await secret_of(engine, node)
    new = {
        "port": 9443,
        "tls_domain": "WWW.Apple.com",
        "mode_tls": False,
        "mode_secure": True,
        "mode_classic": True,
    }
    r = await client.put(f"{BASE}/settings", json=new)
    assert r.status_code == 200, r.text
    want = {**new, "tls_domain": "www.apple.com", "link_mode": "secure"}
    assert r.json() == {"settings": want}
    assert (await client.get(BASE)).json()["settings"] == want
    logged = [a for a in await actions(engine) if a["action"] == "settings_update"]
    assert len(logged) == 1 and logged[0]["actor"] == "admin:owner" and logged[0]["target"] == "mtproto"
    assert logged[0]["reasons"]["context"]["changed"]["port"] == [8443, 9443]
    await MtprotoReconciler().try_run_once(engine)
    proxy = (await client.get(BASE)).json()["proxies"][0]
    assert (proxy["port"], proxy["mode"]) == (9443, "secure")
    assert proxy["link"] == f"https://t.me/proxy?server=203.0.113.10&port=9443&secret=dd{secret}"
    assert link_secret(secret, "secure", "x") == f"dd{secret}"


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"port": 443}, ["port"]),
        ({"port": 22}, ["port"]),
        ({"port": "8443"}, ["port"]),
        ({"port": 70000}, ["port"]),
        ({"tls_domain": "203.0.113.9"}, ["tls_domain"]),
        ({"tls_domain": "no spaces.com"}, ["tls_domain"]),
        ({"mode_tls": False}, ["modes"]),
        ({"mode_tls": 1}, ["mode_tls"]),
        ({"secret": "0" * 32}, ["unknown"]),
    ],
)
async def test_put_settings_refuses_bad_values_without_echoing_them(
    client: AsyncClient, engine: AsyncEngine, body: dict[str, Any], fields: list[str]
) -> None:
    full = {**{k: v for k, v in DEFAULTS.items() if k != "link_mode"}, **body}
    r = await client.put(f"{BASE}/settings", json=full)
    err = assert_error(r, 422, "invalid_settings")
    assert err["fields"] == fields
    for value in body.values():
        if isinstance(value, str):
            assert value not in r.text
    assert await rows(engine, "SELECT 1 FROM settings WHERE key = 'mtproto'") == []
    assert (await client.get(BASE)).json()["settings"] == DEFAULTS


@pytest.mark.parametrize("payload", [[1, 2], "x", None, 5])
async def test_put_settings_refuses_a_non_object(client: AsyncClient, payload: Any) -> None:
    # A JSON null is "no body" to FastAPI: the app-wide contract error for a malformed request.
    code = "invalid_request" if payload is None else "invalid_settings"
    assert_error(await client.put(f"{BASE}/settings", json=payload), 422, code)


async def test_corrupt_settings_are_shown_as_such_and_a_valid_put_repairs_them(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('mtproto', CAST(:v AS jsonb))"),
            {"v": '{"port": 22}'},
        )
    body = (await client.get(BASE)).json()
    assert body["settings"] is None and body["settings_corrupt"] is True
    full = {k: v for k, v in DEFAULTS.items() if k != "link_mode"}
    assert (await client.put(f"{BASE}/settings", json=full)).status_code == 200
    assert (await client.get(BASE)).json()["settings_corrupt"] is False
    logged = [a for a in await actions(engine) if a["action"] == "settings_update"]
    assert logged[-1]["reasons"]["context"]["changed"] == {"*": ["corrupt", "replaced"]}


async def test_without_the_master_key_the_list_is_unavailable(
    client: AsyncClient, engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROXYHUB_MASTER_KEY_FILE", str(tmp_path / "missing"))
    reset_config_caches()
    assert_error(await client.get(BASE), 503, "mtproto_key_unavailable")


async def test_the_secret_leaves_only_inside_the_published_link(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    with all_logs() as logs:
        node = await live_node(engine, slot=3, ipv4="203.0.113.23")
        r = await client.get(BASE)
        bad = await client.put(f"{BASE}/settings", json={"port": 443})
        await client.put(f"{BASE}/settings", json={**{k: v for k, v in DEFAULTS.items() if k != "link_mode"}})
        await MtprotoReconciler().try_run_once(engine)
        logging.getLogger("proxyhub").info("end")
    secret = await secret_of(engine, node)
    proxy = r.json()["proxies"][0]
    assert secret in proxy["link"]
    without_link = {**r.json(), "proxies": [{k: v for k, v in proxy.items() if k != "link"}]}
    assert secret not in str(without_link)
    assert secret not in bad.text
    assert secret not in logs.getvalue()
    stored = await all_table_text(engine)
    assert secret not in stored and secret.upper() not in stored
