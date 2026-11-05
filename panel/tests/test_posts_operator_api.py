"""/api/posts/operator over real HTTP (ASGI transport) with a real PostgreSQL and Redis (decision D38).

Telegram is never imitated (D10): "check now" goes to a local port nobody listens on (a real
refusal); the reads with Telegram's answers are tests/test_posts_operator_checks.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF
from test_operator_poster_support import EMOJI_SET, TEST_NAME, clean_operator, setup_operator
from test_posts_support import clean_posts_tables, fleet_kill_switch, rows, vault_at
from test_telegram_support import free_closed_port

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_mtproxy_account import get_client_options, get_lock_wait_s, get_vault
from proxyhub.mtproxy import store as mtp_store
from proxyhub.mtproxy.userclient import ClientOptions, Endpoint
from proxyhub.posts import operator
from proxyhub.reconcilers.base import lock_key
from proxyhub.vault import Vault

pytestmark = [pytest.mark.db, pytest.mark.redis]

BASE = "/api/posts/operator"
PATHS = {("GET", BASE), ("PUT", BASE), ("POST", f"{BASE}/check")}


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_posts_tables(eng)
    await clean_operator(eng)
    yield eng
    await clean_posts_tables(eng)
    await clean_operator(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return vault_at(tmp_path)


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    port = free_closed_port()
    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    app.dependency_overrides[get_client_options] = lambda: ClientOptions(
        endpoint=Endpoint("127.0.0.1", port), deadline_s=3
    )
    app.dependency_overrides[get_lock_wait_s] = lambda: 0.5
    try:
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        for dep in (require_admin, get_vault, get_client_options, get_lock_wait_s):
            app.dependency_overrides.pop(dep, None)


def assert_error(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    assert body["error"] == code and body["message_fa"], body
    return body


async def test_every_route_requires_admin_and_csrf_and_none_deletes(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_posts_operator
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_posts_operator.router.routes:
        assert isinstance(route, APIRoute)
        assert "DELETE" not in (route.methods or set())
        for method in route.methods or set():
            found.add((method, route.path))
        assert require_admin in {d.call for d in route.dependant.dependencies}, route.path
    assert found == PATHS
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(PATHS):
                r = await c.request(method, path, json={})
                assert r.status_code == 401 and r.json()["error"] == "unauthenticated", (method, path)
                if method != "GET":
                    r = await c.request(method, path, json={}, headers={"X-Requested-With": "nope"})
                    assert r.status_code == 403 and r.json()["error"] == "csrf", path


async def test_the_bot_is_the_default_and_the_owner_switches(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    got = (await client.get(BASE)).json()
    assert (got["poster"], got["emoji_pack"], got["pack_link"], got["pack"]) == ("bot", None, None, None)
    assert got["account"] == {"state": "none", "first_name": None, "kill_switch": True}
    assert got["channels"] == {} and got["fallbacks"] == [] and got["hold_until"] is None
    put = await client.put(
        BASE, json={"poster": "operator", "pack_link": f"https://t.me/addemoji/{EMOJI_SET}"}
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert (body["poster"], body["emoji_pack"]) == ("operator", EMOJI_SET)
    assert body["pack_link"] == f"https://t.me/addemoji/{EMOJI_SET}"
    [action] = await rows(
        engine, "SELECT actor, reasons FROM actions_log WHERE action = 'posts.operator_set'"
    )
    assert action.actor == "admin:owner" and action.reasons["reasons"] == ["poster_operator"]
    back = await client.put(BASE, json={"poster": "bot"})
    assert back.json()["poster"] == "bot" and back.json()["emoji_pack"] == EMOJI_SET  # the pack stays
    same = await client.put(BASE, json={"poster": "bot"})
    assert same.status_code == 200
    assert len(await rows(engine, "SELECT id FROM actions_log WHERE action = 'posts.operator_set'")) == 2


@pytest.mark.parametrize(
    ("link", "pack"),
    [
        ("https://t.me/addemoji/Proxy_Hub_1", "Proxy_Hub_1"),
        ("  t.me/addemoji/abc/  ", "abc"),
        ("http://telegram.me/addemoji/abc", "abc"),
        ("tg://addemoji?set=abc_by_bot", "abc_by_bot"),
        ("", None),
        (None, None),
    ],
)
async def test_pack_links_that_are_taken(client: AsyncClient, link: str | None, pack: str | None) -> None:
    await client.put(BASE, json={"pack_link": "https://t.me/addemoji/previous"})
    r = await client.put(BASE, json={"pack_link": link})
    assert r.status_code == 200, r.text
    assert r.json()["emoji_pack"] == pack


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"pack_link": "https://t.me/addstickers/abc"}, "pack_link_stickers"),
        ({"pack_link": "https://evil.example/addemoji/abc"}, "pack_link_invalid"),
        ({"pack_link": "https://t.me/addemoji/a-b"}, "pack_link_invalid"),
        ({"pack_link": "https://t.me/addemoji/"}, "pack_link_invalid"),
        ({"pack_link": "https://t.me/addemoji/abc?x=1"}, "pack_link_invalid"),
        ({"pack_link": 5}, "pack_link_invalid"),
        ({"pack_link": "x" * 400}, "pack_link_invalid"),
        ({"poster": "someone"}, "poster_invalid"),
        ({"poster": None}, "poster_invalid"),
        ({}, "invalid_request"),
        ({"poster": "bot", "delete": True}, "invalid_request"),
        ([], "invalid_request"),
    ],
)
async def test_bad_input_is_refused_in_persian(client: AsyncClient, body: Any, code: str) -> None:
    r = await client.put(BASE, json=body)
    out = assert_error(r, 422, code)
    assert "abc" not in out["message_fa"] and "evil" not in out["message_fa"]  # no input echoed


async def test_check_now_needs_the_account_and_never_waits_for_a_busy_one(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    nothing = await client.post(f"{BASE}/check")  # no pack, no channel: nothing to read
    assert nothing.status_code == 200 and nothing.json()["outcome"] == "nothing_to_check"
    await setup_operator(engine, vault, connected=False)
    assert_error(await client.post(f"{BASE}/check"), 409, "operator_not_connected")
    await setup_operator(engine, vault, kill_switch=True)
    assert_error(await client.post(f"{BASE}/check"), 409, "operator_kill_switch")
    await fleet_kill_switch(engine, on=False)
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key(mtp_store.LOCK_NAME)})
        await holder.commit()
        assert_error(await client.post(f"{BASE}/check"), 409, "mtproxy_busy")
        await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(mtp_store.LOCK_NAME)})
        await holder.commit()
    # Telegram unreachable (a closed local port): the failure is shown, nothing else changes.
    body = assert_error(await client.post(f"{BASE}/check"), 502, "operator_unavailable")
    assert body["telegram_error"] == "telegram_unavailable"
    got = (await client.get(BASE)).json()
    assert got["pack"]["error"] == "telegram_unavailable" and got["pack"]["short_name"] == EMOJI_SET
    test = got["channels"]["test"]
    assert test["username"] == f"@{TEST_NAME}" and test["public"] is True and test["ready"] is None
    assert test["rights"]["status"] == "error"
    assert got["account"]["state"] == "connected" and got["account"]["kill_switch"] is False
    async with engine.connect() as conn:
        assert (await operator.load_config(conn)).poster == "operator"
