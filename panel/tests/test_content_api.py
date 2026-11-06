"""/api/content/* over real HTTP (ASGI transport) with a real PostgreSQL and Redis (decision D51)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_content_support import clean_content_tables, png
from test_fleet_support import ADMIN, CSRF

from proxyhub.api.deps import require_admin
from proxyhub.content import candidates, sources
from proxyhub.content.items import FetchedItem, MediaRef
from proxyhub.content.media import check_for
from proxyhub.telegram.store import now_utc

pytestmark = [pytest.mark.db, pytest.mark.redis]

PATHS = {
    ("GET", "/api/content/sources"),
    ("POST", "/api/content/sources"),
    ("PUT", "/api/content/sources/{source_id}"),
    ("POST", "/api/content/sources/{source_id}/fetch"),
    ("DELETE", "/api/content/sources/{source_id}"),
    ("GET", "/api/content/queue"),
    ("PUT", "/api/content/candidates/{candidate_id}"),
    ("POST", "/api/content/candidates/{candidate_id}/status"),
    ("GET", "/api/content/media/{media_id}"),
}
NEWS = (
    "🔻 هشدار عراق\nوزیر خارجه عراق نسبت به ورود گروه‌های مسلح نزدیک به ایران به هرگونه درگیری نظامی "
    "احتمالی میان ایران و آمریکا هشدار داد. https://bbc.in/x @BBCPersian"
)


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_content_tables(eng)
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM actions_log WHERE action LIKE 'content.%'"))
    yield eng
    await clean_content_tables(eng)
    await eng.dispose()


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


async def test_every_content_route_requires_admin(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_content
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_content.api.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods or set():
            found.add((method, route.path))
        assert require_admin in {d.call for d in route.dependant.dependencies}, route.path
    assert found == PATHS
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(PATHS):
                url = (
                    path.replace("{source_id}", "1").replace("{candidate_id}", "1").replace("{media_id}", "1")
                )
                r = await c.request(method, url, json={})
                assert r.status_code == 401, (method, path, r.status_code)


async def test_the_owner_adds_changes_and_removes_sources(client: AsyncClient) -> None:
    r = await client.post("/api/content/sources", json={"kind": "telegram", "ref": "https://t.me/BBCPersian"})
    assert r.status_code == 201, r.text
    source = r.json()
    assert (source["ref"], source["label"], source["link"], source["enabled"]) == (
        "bbcpersian",
        "@bbcpersian",
        "https://t.me/bbcpersian",
        True,
    )
    for body, status, code in (
        ({"kind": "telegram", "ref": "@bbcpersian"}, 409, "source_exists"),
        ({"kind": "telegram", "ref": "https://t.me/joinchat/x"}, 422, "telegram_name_invalid"),
        ({"kind": "rss", "ref": "http://news.example/f"}, 422, "feed_url_invalid"),
        ({"kind": "rss", "ref": "https://169.254.169.254/latest"}, 422, "feed_url_invalid"),
        ({"kind": "instagram", "ref": "x"}, 422, "kind_invalid"),
        ({"kind": "rss", "ref": "https://news.example/f", "label": "x" * 81}, 422, "label_invalid"),
        ({"kind": "rss", "ref": "https://news.example/f", "extra": 1}, 422, "invalid_request"),
        ({"kind": "rss"}, 422, "invalid_request"),
    ):
        r = await client.post("/api/content/sources", json=body)
        assert (r.status_code, r.json()["error"]) == (status, code), body
        assert r.json()["message_fa"]
    r = await client.post(
        "/api/content/sources", json={"kind": "rss", "ref": "https://news.example/feed.xml", "label": "خبر"}
    )
    feed = r.json()
    listed = (await client.get("/api/content/sources")).json()
    assert [s["id"] for s in listed["sources"]] == [source["id"], feed["id"]] and listed[
        "fetch_every_min"
    ] == 5
    r = await client.put(f"/api/content/sources/{feed['id']}", json={"enabled": False, "label": "خبرگزاری"})
    assert (r.json()["enabled"], r.json()["label"]) == (False, "خبرگزاری")
    r = await client.put(f"/api/content/sources/{feed['id']}", json={"enabled": "no"})
    assert r.status_code == 422
    r = await client.post(f"/api/content/sources/{source['id']}/fetch")
    assert r.status_code == 200 and r.json()["next_fetch_at"] is not None
    r = await client.delete(f"/api/content/sources/{feed['id']}")
    assert r.json() == {"removed": True, "dropped_pending": 0}
    r = await client.delete(f"/api/content/sources/{feed['id']}")
    assert (r.status_code, r.json()["error"]) == (404, "source_not_found")


async def test_the_review_queue_shows_the_panels_own_copy_and_records_the_review(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    now = now_utc()
    data = png(40, 30)
    async with engine.begin() as conn:
        source = await sources.create(conn, kind="telegram", ref="@bbcpersian", label="BBC", now=now)
        await candidates.store_items(
            conn,
            source,
            [
                FetchedItem(
                    "7",
                    "https://t.me/bbcpersian/7",
                    now - timedelta(minutes=10),
                    NEWS,
                    views=6790,
                    media=MediaRef("photo", "https://cdn4.telesco.pe/file/secret-token.jpg"),
                )
            ],
            now,
        )
        (task,) = await candidates.media_tasks(conn, 5)
        media_id = await candidates.save_media(conn, task, check_for("photo", data), data, now)
    r = await client.get("/api/content/queue")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "telesco.pe" not in r.text  # the source's media address never reaches the dashboard
    (one,) = body["items"]
    assert body["counts"]["pending"] == 1 and "caption_budget" in body
    assert (one["title"], one["views"], one["url"]) == ("🔻 هشدار عراق", 6790, "https://t.me/bbcpersian/7")
    assert "@BBCPersian" not in one["body"] and "bbc.in" not in one["body"]
    assert one["media"] == {
        "kind": "photo",
        "state": "ready",
        "error": None,
        "error_fa": None,
        "preview_id": media_id,
        "width": 40,
        "height": 30,
        "duration": None,
        "size": len(data),
    }
    r = await client.get(f"/api/content/media/{media_id}")
    assert r.status_code == 200 and r.content == data
    assert r.headers["content-type"] == "image/png" and r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["cache-control"].startswith(
        "private"
    )
    r = await client.get("/api/content/media/999999")
    assert (r.status_code, r.json()["error"]) == (404, "media_not_found")
    r = await client.get("/api/content/queue", params={"status": "duplicate"})
    assert r.status_code == 422

    cid = one["id"]
    r = await client.put(f"/api/content/candidates/{cid}", json={"title": "x" * 121, "body": ""})
    assert (r.status_code, r.json()["error"]) == (422, "caption_invalid")
    r = await client.post(
        f"/api/content/candidates/{cid}/status",
        json={"status": "approved", "title": "🔻 عراق: هشدار درباره تونل تاریک", "body": "متن کوتاه."},
    )
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["title"], r.json()["body"]) == (
        "approved",
        "🔻 عراق: هشدار درباره تونل تاریک",
        "متن کوتاه.",
    )
    r = await client.post(f"/api/content/candidates/{cid}/status", json={"status": "used"})
    assert (r.status_code, r.json()["error"]) == (422, "status_invalid")
    r = await client.post(f"/api/content/candidates/{cid}/status", json={"status": "rejected"})
    assert r.json()["status"] == "rejected"
    r = await client.post("/api/content/candidates/999999/status", json={"status": "rejected"})
    assert (r.status_code, r.json()["error"]) == (404, "candidate_not_found")
    async with engine.connect() as conn:
        actions = (
            (
                await conn.execute(
                    text("SELECT action FROM actions_log WHERE action LIKE 'content.%' ORDER BY id")
                )
            )
            .scalars()
            .all()
        )
    assert actions == ["content.candidate_status", "content.candidate_status"]  # approved, then rejected
