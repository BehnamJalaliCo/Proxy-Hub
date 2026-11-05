"""/api/cleanup/* over real HTTP (ASGI transport) with a real PostgreSQL and Redis.

Owner only, CSRF on every write, Persian errors, counts only in answers and in actions_log. Telegram
is never imitated (D10): a refused local port and the real api.telegram.org with a random token.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_cleanup_ended import EXPECTED_REMOVED, expected_kept, seed
from test_cleanup_support import World, wipe
from test_fleet_support import ADMIN, CSRF, make_vault
from test_telegram_support import closed_port_bot, real_bot, telegram_reachable

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_cleanup import get_bot_factory, get_vault
from proxyhub.cleanup import ended
from proxyhub.vault import Vault

pytestmark = [pytest.mark.db, pytest.mark.redis]

PATHS = {
    ("POST", "/api/cleanup/ended/preview"),
    ("POST", "/api/cleanup/ended"),
    ("GET", "/api/cleanup/test-channel"),
    ("POST", "/api/cleanup/test-channel"),
}


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await wipe(eng)
    yield eng
    await wipe(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    (tmp_path / "v").mkdir()
    return make_vault(tmp_path / "v")


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    app.dependency_overrides[get_bot_factory] = closed_port_bot  # no accidental real call
    try:
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        for dep in (require_admin, get_vault, get_bot_factory):
            app.dependency_overrides.pop(dep, None)


async def test_every_cleanup_route_requires_the_owner_and_csrf(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_cleanup
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_cleanup.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
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


async def test_preview_then_purge_over_http(client: AsyncClient, engine: AsyncEngine, tmp_path: Path) -> None:
    s = await seed(engine, tmp_path)
    before = await s.w.counts(ended.ALLOWED_TABLES)
    r = await client.post("/api/cleanup/ended/preview", json={"min_age_hours": 1})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert (
        body["dry_run"] is True and body["removed"] == EXPECTED_REMOVED and body["kept"] == expected_kept(s)
    )
    assert await s.w.counts(ended.ALLOWED_TABLES) == before
    assert await s.w.one("SELECT count(*) FROM actions_log WHERE action LIKE 'cleanup.%'") == 0

    r = await client.post("/api/cleanup/ended", json={"min_age_hours": 1})
    assert r.status_code == 200
    done = r.json()
    assert done["dry_run"] is False and done["removed"] == EXPECTED_REMOVED
    assert done["total"] == sum(EXPECTED_REMOVED.values())
    assert set(done) == {
        "dry_run",
        "min_age_hours",
        "cutoff",
        "removed",
        "total",
        "kept",
        "ip_history_unlinked",
    }
    log = await s.w.rows("SELECT actor, action, target, allowed, reasons, result FROM actions_log")
    assert len(log) == 2  # the seeded row and this one
    row = next(x for x in log if x.action == "cleanup.ended_purged")
    assert (row.actor, row.target, row.allowed) == ("admin:owner", "database", True)
    assert row.reasons["context"]["removed"] == {k: v for k, v in EXPECTED_REMOVED.items() if v}
    assert row.reasons["context"]["total"] == done["total"]
    assert row.result == f"{done['total']} ended row(s) removed"
    # Nothing but counts and names of tables: no id, address, link or text of any removed row.
    assert all(isinstance(v, int) for v in done["removed"].values())
    assert str(s.ids["gone"]) not in r.text and str(s.ids["gone"]) not in json.dumps(row.reasons)

    again = (await client.post("/api/cleanup/ended", json={"min_age_hours": 1})).json()
    assert again["total"] == 0


@pytest.mark.parametrize(
    "payload", [{"min_age_hours": 0}, {"min_age_hours": 721}, {"min_age_hours": "1"}, {"x": 1}, []]
)
async def test_a_wrong_minimum_age_is_refused(client: AsyncClient, payload: Any) -> None:
    for path in ("/api/cleanup/ended/preview", "/api/cleanup/ended"):
        r = await client.post(path, json=payload)
        assert r.status_code == 422 and r.json()["error"] == "invalid_request", (path, payload)
        assert r.json()["message_fa"]


async def test_default_minimum_age_is_one_hour(client: AsyncClient) -> None:
    r = await client.post("/api/cleanup/ended/preview", json={})
    assert r.status_code == 200 and r.json()["min_age_hours"] == ended.DEFAULT_MIN_AGE_HOURS == 1


async def test_test_channel_preview_and_refusals(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    w = World(engine)
    r = await client.get("/api/cleanup/test-channel")
    assert r.status_code == 200 and r.json()["blocked"] == "bot_not_set"
    token = await w.telegram(vault, test=False, main=True)
    preview = (await client.get("/api/cleanup/test-channel")).json()
    assert preview["blocked"] == "test_channel_not_set" and preview["channel"] is None
    # Only a main channel exists: the API refuses before any request (a request would be a 502).
    r = await client.post("/api/cleanup/test-channel", json={"confirm": "پاک کن"})
    assert r.status_code == 409 and r.json()["error"] == "test_channel_not_set"
    assert "هرگز به کانال اصلی دست نمیزند" in r.json()["message_fa"]
    # The same chat stored as both: refused, the main channel is safe.
    await w.telegram(vault, test=True, main=True, same=True)
    r = await client.post("/api/cleanup/test-channel", json={"confirm": "پاک کن"})
    assert r.status_code == 409 and r.json()["error"] == "test_is_main"
    # The typed confirmation is checked first.
    await w.telegram(vault)
    for wrong in ("", "پاک", "delete", "پاک کن پاک کن"):
        r = await client.post("/api/cleanup/test-channel", json={"confirm": wrong})
        assert r.status_code == 422 and r.json()["error"] == "confirm_required", wrong
    r = await client.post("/api/cleanup/test-channel", json={})
    assert r.status_code == 422
    # Telegram not reachable (a refused port): nothing starts.
    r = await client.post("/api/cleanup/test-channel", json={"confirm": "پاک‌کن"})
    assert r.status_code == 502 and r.json()["error"] == "telegram_unavailable"
    assert await w.one("SELECT count(*) FROM telegram_pacing") == 0
    assert await w.one("SELECT count(*) FROM actions_log") == 0
    for text_ in (r.text, json.dumps((await client.get("/api/cleanup/test-channel")).json())):
        assert token not in text_ and token.split(":", 1)[1] not in text_


async def test_test_channel_cleaning_gets_telegrams_real_401(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    from proxyhub.api.app import app

    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    app.dependency_overrides[get_bot_factory] = lambda: real_bot
    w = World(engine)
    token = await w.telegram(vault)
    r = await client.post("/api/cleanup/test-channel", json={"confirm": "پاک کن"})
    assert r.status_code == 400 and r.json()["error"] == "bot_token_invalid"
    preview = await client.get("/api/cleanup/test-channel")
    assert preview.json()["blocked"] == "bot_token_invalid"
    assert token not in r.text + preview.text
