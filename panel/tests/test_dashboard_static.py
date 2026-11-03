"""The dashboard SPA route (proxyhub/api/routes_zz_static.py) over real HTTP: the full app through
ASGITransport, plus a real uvicorn server for raw request paths that an HTTP client would normalise."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from proxyhub.api import routes_zz_static as static
from proxyhub.api.registry import include_all

MARKER = "<!-- proxyhub-test-index -->"
JS = "export const answer = 42;\n"


@pytest.fixture()
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A small built dashboard plus things that must never be served."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        f"<!doctype html><html lang='fa' dir='rtl'>{MARKER}</html>", encoding="utf-8"
    )
    (root / "assets" / "app-abc123.js").write_text(JS, encoding="utf-8")
    (root / "assets" / "font-x1.woff2").write_bytes(b"wOF2\x00\x01")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    (root / ".hidden").write_text("hidden", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("TOP-SECRET-OUTSIDE", encoding="utf-8")
    (root / "assets" / "evil.js").symlink_to(outside)
    monkeypatch.setenv(static.ENV_DIR, str(root))
    yield root


@pytest.fixture()
async def client(dist: Path) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://panel.test") as c:
        yield c


SPA_PATHS = [
    "/",
    "/login",
    "/setup",
    "/accounts",
    "/settings",
    "/events",
    "/security",
    "/deep/link/x",
    "/index.html",
]


@pytest.mark.parametrize("path", SPA_PATHS)
async def test_spa_paths_get_index_with_strict_headers(client: AsyncClient, path: str) -> None:
    r = await client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert MARKER in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert "unsafe-eval" not in csp and "http:" not in csp and "https:" not in csp
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_hashed_assets_are_immutable_and_typed(client: AsyncClient) -> None:
    r = await client.get("/assets/app-abc123.js")
    assert r.status_code == 200
    assert r.text == JS
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == static.IMMUTABLE
    assert r.headers["x-content-type-options"] == "nosniff"

    font = await client.get("/assets/font-x1.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"

    icon = await client.get("/favicon.svg")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/svg+xml"
    assert icon.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "path", ["/assets/missing.js", "/assets/sub/none.css", "/missing.png", "/robots.txt", "/.hidden"]
)
async def test_missing_files_are_404_never_html(client: AsyncClient, path: str) -> None:
    r = await client.get(path)
    assert r.status_code == 404
    assert MARKER not in r.text and "<html" not in r.text


async def test_symlink_out_of_dist_is_not_served(client: AsyncClient) -> None:
    r = await client.get("/assets/evil.js")
    assert r.status_code == 404
    assert "TOP-SECRET-OUTSIDE" not in r.text


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "HEAD"])
@pytest.mark.parametrize(
    "path",
    ["/api", "/api/", "/api/does-not-exist", "/agent/v1/x", "/internal/nope", "/api/assets/app-abc123.js"],
)
async def test_api_agent_internal_are_never_shadowed(client: AsyncClient, method: str, path: str) -> None:
    r = await client.request(method, path)
    assert r.status_code == 404, (method, path, r.status_code)
    assert MARKER not in r.text
    if method != "HEAD":
        assert r.headers["content-type"].startswith("application/json")


async def test_existing_backend_routes_still_answer(client: AsyncClient) -> None:
    live = await client.get("/livez")
    assert live.status_code == 200 and live.json() == {"status": "alive"}
    metrics = await client.get("/internal/metrics")
    assert metrics.status_code == 200 and metrics.headers["content-type"].startswith("text/plain")
    assert MARKER not in metrics.text


async def test_head_is_answered_without_body(client: AsyncClient) -> None:
    r = await client.head("/accounts")
    assert r.status_code == 200 and r.content == b""
    assert r.headers["content-type"].startswith("text/html")


async def test_not_built_dashboard_is_503_but_api_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from proxyhub.api.app import app

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(static.ENV_DIR, str(empty))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://panel.test") as c:
        r = await c.get("/settings")
        assert r.status_code == 503
        assert "داشبورد هنوز ساخته نشده" in r.text
        assert "default-src 'self'" in r.headers["content-security-policy"]
        api = await c.get("/api/nothing-here")
        assert api.status_code == 404 and api.headers["content-type"].startswith("application/json")
        assert (await c.get("/livez")).status_code == 200


def test_reserved_paths() -> None:
    for p in [
        "api",
        "api/",
        "api/x/y",
        "/api/overview",
        "agent",
        "agent/v1/desired",
        "internal/metrics",
        "healthz",
        "livez",
        "healthz/",
    ]:
        assert static.is_reserved(p), p
    for p in [
        "",
        "accounts",
        "apiary",
        "agents",
        "internals",
        "healthzz",
        "livez2",
        "assets/api/x.js",
        "settings/api",
    ]:
        assert not static.is_reserved(p), p


def test_safe_file_refuses_escapes(dist: Path) -> None:
    assert static.safe_file(dist, "assets/app-abc123.js") == (dist / "assets" / "app-abc123.js").resolve()
    for bad in [
        "../outside-secret.txt",
        "assets/../../outside-secret.txt",
        "assets/../index.html",
        "./index.html",
        ".hidden",
        "assets/.x",
        "assets//app-abc123.js",
        "assets\\app-abc123.js",
        "index.html\x00.js",
        "assets/evil.js",
        "assets",
        "",
    ]:
        assert static.safe_file(dist, bad) is None, repr(bad)


def test_static_router_is_included_last() -> None:
    app = FastAPI()
    names = include_all(app)
    assert names[-1] == "routes_zz_static"
    last = app.routes[-1]
    # FastAPI >= 0.16x wraps included routers; older versions copy the route itself.
    assert (
        getattr(last, "original_router", None) is static.router
        or getattr(last, "path", None) == "/{path:proxyhub_spa}"
    )


async def _raw_get(port: int, target: str) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: panel.test\r\nConnection: close\r\n\r\n".encode("latin-1"))
    await writer.drain()
    data = await reader.read()
    writer.close()
    await writer.wait_closed()
    text = data.decode("utf-8", "replace")
    m = re.match(r"HTTP/1\.1 (\d{3})", text)
    assert m, text[:200]
    return int(m.group(1)), text


async def test_raw_traversal_requests_on_a_real_server(dist: Path) -> None:
    """Request targets sent byte-for-byte to uvicorn (no client-side path normalisation)."""
    from proxyhub.api.app import app

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        port = server.servers[0].sockets[0].getsockname()[1]

        status, body = await _raw_get(port, "/accounts")
        assert status == 200 and MARKER in body

        for target in [
            "/assets/../../outside-secret.txt",
            "/assets/%2e%2e/%2e%2e/outside-secret.txt",
            "/%2e%2e/outside-secret.txt",
            "/..%2foutside-secret.txt",
            "/assets/evil.js",
            "/assets/..%5c..%5coutside-secret.txt",
            "/%2fetc%2fpasswd",
        ]:
            status, body = await _raw_get(port, target)
            assert "TOP-SECRET-OUTSIDE" not in body, target
            assert "root:" not in body, target
            assert status in (200, 400, 404), (target, status)
            if status == 200:  # only ever the SPA shell, never a file from elsewhere
                assert MARKER in body, target
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


DEV_INDEX = static.DEV_DIR / "index.html"


@pytest.mark.skipif(not DEV_INDEX.is_file(), reason="dashboard not built here (npm run build in dashboard/)")
async def test_real_built_dashboard_assets_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every script, style and preload that the real built index.html references is served."""
    from proxyhub.api.app import app

    monkeypatch.setenv(static.ENV_DIR, str(static.DEV_DIR))
    html = DEV_INDEX.read_text(encoding="utf-8")
    refs = re.findall(r'(?:src|href)="(/[^"]+)"', html)
    assert any(r.startswith("/assets/") and r.endswith(".js") for r in refs)
    assert "<script>" not in html  # no inline script: the CSP would block it
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://panel.test") as c:
        for ref in refs:
            r = await c.get(ref)
            assert r.status_code == 200, ref
            if ref.endswith(".js"):
                assert r.headers["content-type"].startswith("text/javascript"), ref
            if ref.endswith(".css"):
                assert r.headers["content-type"].startswith("text/css"), ref


@pytest.mark.db
@pytest.mark.redis
async def test_healthz_is_not_shadowed_with_a_dashboard_present(migrated_db: str, dist: Path) -> None:
    from proxyhub.api.app import app, lifespan

    async with (
        lifespan(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://panel.test") as c,
    ):
        r = await c.get("/healthz")
        assert r.headers["content-type"].startswith("application/json")
        assert "db" in r.json() and MARKER not in r.text
