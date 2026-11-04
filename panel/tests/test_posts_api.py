"""/api/posts/* over real HTTP (ASGI transport) with a real PostgreSQL and Redis."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF
from test_posts_support import (
    PROVIDERS,
    clean_posts_tables,
    deliver,
    jobs,
    make_node,
    node_secret,
    node_vless_id,
    rows,
    set_node,
    setup_telegram,
    vault_at,
)

from proxyhub.api.deps import require_admin
from proxyhub.links import registry
from proxyhub.posts import links, publisher
from proxyhub.vault import Vault

pytestmark = [pytest.mark.db, pytest.mark.redis]

POSTS_PATHS = {
    ("GET", "/api/posts/status"),
    ("GET", "/api/posts/preview"),
    ("GET", "/api/posts/recent"),
    ("GET", "/api/posts/settings"),
    ("PUT", "/api/posts/settings"),
    ("GET", "/api/posts/content"),
    ("POST", "/api/posts/content"),
    ("PUT", "/api/posts/content/{item_id}"),
    ("POST", "/api/posts/content/{item_id}/status"),
}


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_posts_tables(eng)
    yield eng
    await clean_posts_tables(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return vault_at(tmp_path)


@pytest.fixture
def providers() -> Iterator[None]:
    """The registry the integrator fills, filled for the test with the test providers."""
    saved = list(registry.PROVIDERS)
    registry.PROVIDERS[:] = PROVIDERS
    yield
    registry.PROVIDERS[:] = saved


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


SETTINGS = {
    "enabled": True,
    "main_enabled": False,
    "posts_per_day": 10,
    "day_start": "08:00",
    "day_end": "00:00",
    "max_mtproto": 12,
    "max_reality": 3,
    "edit_window_hours": 48,
    "recycle_after_days": 14,
    "status_texts": {
        "expired": "⌛️ زمان این کانفیگ تمام شد",
        "full": "🔴 ظرفیت این کانفیگ پر شد",
        "burned": "🚫 این کانفیگ از دسترس خارج شد",
    },
}


# --- protection ----------------------------------------------------------------------------------


async def test_every_posts_route_requires_admin_and_none_deletes(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_posts
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_posts.api.routes:
        assert isinstance(route, APIRoute)
        assert "DELETE" not in route.methods
        for method in route.methods:
            found.add((method, route.path))
        assert require_admin in {d.call for d in route.dependant.dependencies}, route.path
    assert found == POSTS_PATHS
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(POSTS_PATHS):
                url = path.replace("{item_id}", "1")
                r = await c.request(method, url, json={})
                assert r.status_code == 401 and r.json()["error"] == "unauthenticated", (method, path)
                if method != "GET":  # require_admin checks CSRF first
                    r = await c.request(method, url, json={}, headers={"X-Requested-With": "nope"})
                    assert r.status_code == 403 and r.json()["error"] == "csrf", path


# --- settings ------------------------------------------------------------------------------------


async def test_settings_defaults_save_and_validation(client: AsyncClient, engine: AsyncEngine) -> None:
    got = await client.get("/api/posts/settings")
    assert got.status_code == 200 and got.json() == SETTINGS
    new = {**SETTINGS, "posts_per_day": 8, "day_start": "09:30", "status_texts": {**SETTINGS["status_texts"],
           "expired": "⌛️ تمام شد"}}  # fmt: skip
    put = await client.put("/api/posts/settings", json=new)
    assert put.status_code == 200 and put.json() == new
    assert (await client.get("/api/posts/settings")).json() == new
    [action] = await rows(engine, "SELECT action, reasons::text AS r FROM actions_log")
    assert action.action == "posts.settings_set" and "posts_per_day" in action.r
    bad = await client.put("/api/posts/settings", json={**SETTINGS, "posts_per_day": 99, "day_end": "25:00"})
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_settings"
    assert bad.json()["fields"] == ["day_end", "posts_per_day"] and "99" not in bad.text
    assert (await client.put("/api/posts/settings", content=b"{not json")).json()[
        "error"
    ] == "invalid_request"
    assert (await client.put("/api/posts/settings", json={**SETTINGS, "x": 1})).status_code == 422


async def test_main_posting_needs_the_main_channel(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    on = {**SETTINGS, "main_enabled": True}
    refused = await client.put("/api/posts/settings", json=on)
    assert refused.status_code == 409 and refused.json()["error"] == "main_channel_not_set"
    assert "کانال اصلی" in refused.json()["message_fa"]
    await setup_telegram(engine, vault, main=True)
    ok = await client.put("/api/posts/settings", json=on)
    assert ok.status_code == 200 and ok.json()["main_enabled"] is True
    actions = await rows(
        engine, "SELECT reasons::text AS r FROM actions_log WHERE action = 'posts.settings_set'"
    )
    assert "main_enabled" in actions[-1].r


# --- content library -----------------------------------------------------------------------------


async def test_seeded_library_is_approved_and_well_formed(client: AsyncClient) -> None:
    from proxyhub.posts.content import check_input
    from proxyhub.telegram.templates import compose_content, fits, render_post

    body = (await client.get("/api/posts/content")).json()
    seeds = [i for i in body["items"] if i["origin"] == "seed"]
    assert len(seeds) >= 30 and body["counts"]["approved"] >= 30
    assert all(i["status"] == "approved" and i["tags"] for i in seeds)
    assert len({i["title"] for i in seeds}) == len(seeds)
    for item in seeds:
        checked = check_input(item["title"], item["body"], item["tags"])  # passes the owner's own checks
        assert checked.body == item["body"]
        assert fits(render_post(compose_content(item["title"], item["body"]), [], "@ipoxxo").text)
        assert not any(w in item["body"] for w in ("http://", "https://", "<a "))


async def test_content_create_edit_approve_retire(client: AsyncClient, engine: AsyncEngine) -> None:
    created = await client.post(
        "/api/posts/content",
        json={"title": " نکته‌ی تازه ", "body": "متن <b>پررنگ</b><script>x</script>", "tags": ["ترفند"]},
    )
    assert created.status_code == 201
    item = created.json()
    assert (item["title"], item["body"], item["status"], item["origin"]) == (
        "نکته‌ی تازه",
        "متن <b>پررنگ</b>x",
        "draft",
        "owner",
    )
    edited = await client.put(
        f"/api/posts/content/{item['id']}", json={"title": "عنوان دوم", "body": "متن دوم"}
    )
    assert edited.status_code == 200 and edited.json()["title"] == "عنوان دوم" and edited.json()["tags"] == []
    approved = await client.post(f"/api/posts/content/{item['id']}/status", json={"status": "approved"})
    assert approved.json()["status"] == "approved"
    listed = (await client.get("/api/posts/content", params={"status": "approved"})).json()["items"]
    assert listed[0]["id"] == item["id"]  # the owner's approved item is next in line
    retired = await client.post(f"/api/posts/content/{item['id']}/status", json={"status": "retired"})
    assert retired.json()["status"] == "retired"
    assert (
        await client.post(f"/api/posts/content/{item['id']}/status", json={"status": "used"})
    ).status_code == 422
    actions = [r.action for r in await rows(engine, "SELECT action FROM actions_log ORDER BY id")]
    assert actions == [
        "posts.content_created",
        "posts.content_updated",
        "posts.content_status",
        "posts.content_status",
    ]
    assert (await client.delete(f"/api/posts/content/{item['id']}")).status_code in (404, 405)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"title": "", "body": "متن"}, "title_invalid"),
        ({"title": "عنوان", "body": "ی" * 1501}, "body_invalid"),
        ({"title": "عنوان", "body": "متن", "tags": ["دو کلمه"]}, "tags_invalid"),
        ({"title": "عنوان", "body": "متن", "status": "used"}, "status_invalid"),
        ({"title": "عنوان"}, "invalid_request"),
        ({"title": "عنوان", "body": "متن", "extra": 1}, "invalid_request"),
    ],
)
async def test_content_validation(client: AsyncClient, payload: dict[str, Any], code: str) -> None:
    res = await client.post("/api/posts/content", json=payload)
    assert res.status_code == 422 and res.json()["error"] == code and res.json()["message_fa"]


async def test_content_not_found(client: AsyncClient) -> None:
    assert (
        await client.put("/api/posts/content/999999", json={"title": "a", "body": "b"})
    ).status_code == 404
    res = await client.post("/api/posts/content/999999/status", json={"status": "approved"})
    assert res.status_code == 404 and res.json()["error"] == "content_not_found"


# --- preview, status, recent ---------------------------------------------------------------------


async def test_preview_status_and_recent(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, providers: None
) -> None:
    empty = (await client.get("/api/posts/preview")).json()
    assert empty["blocked"] == "no_bot" and empty["links"] == {"mtproto": 0, "reality": 0}
    nodes = [await make_node(engine, slot=i, ipv4=f"203.0.113.{20 + i}") for i in (1, 2)]
    await setup_telegram(engine, vault)
    preview = (await client.get("/api/posts/preview")).json()
    assert preview["blocked"] is None and preview["target"] == "test"
    assert preview["links"] == {"mtproto": 2, "reality": 2} and preview["content_item"]["recycled"] is False
    assert node_secret(nodes[0]) in preview["text"] and preview["text"].endswith("🔗 \u200e@ipoxxo")
    assert 0 < preview["chars"] <= 4096
    status = await client.get("/api/posts/status")
    s = status.json()
    assert s["target"] == "test" and s["blocked"] is None and s["links"]["mtproto"] == 2
    assert len(s["today"]["planned"]) == 10 and s["content"]["approved"] >= 30
    # A real post through the publisher, then recorded as sent by the real outbox code.
    now = datetime.now(UTC)
    slot = datetime.fromisoformat(s["today"]["planned"][0]["at"]) + timedelta(seconds=10)
    async with engine.begin() as conn:

        async def harvest() -> links.Harvest:
            return await links.harvest(conn, PROVIDERS)

        assert await publisher.publish_due(conn, slot, harvest) == "queued"
    [job] = await jobs(engine, "send_post")
    recent = (await client.get("/api/posts/recent")).json()["posts"]
    assert recent[0]["state"] == "queued" and recent[0]["message_url"] is None
    await deliver(engine, job.id, message_id=77, at=now)
    await set_node(engine, nodes[0], state="burned")
    recent = await client.get("/api/posts/recent")
    post = recent.json()["posts"][0]
    assert post["state"] == "sent" and post["title"] and len(post["links"]) == 4
    assert {x["status"] for x in post["links"]} == {"active"}
    for n in nodes:  # rule 3: only the preview carries links
        for value in (node_secret(n), node_vless_id(n)):
            assert value not in status.text and value not in recent.text
    # A public main channel gives a link to the message.
    await setup_telegram(engine, vault, main=True)
    await rows(engine, "SELECT 1")
    async with engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(text("UPDATE posts SET role = 'main'"))
        await conn.execute(text("UPDATE channel_posts SET role = 'main'"))
    post = (await client.get("/api/posts/recent")).json()["posts"][0]
    assert post["message_url"] == "https://t.me/ipoxxo/77"
