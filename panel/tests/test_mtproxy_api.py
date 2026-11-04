"""/api/mtproxy/account over real HTTP (ASGI transport) with a real PostgreSQL and Redis.

Telegram is never imitated (D10). Every route here that would reach Telegram is pointed at a local
port nobody listens on (a real refusal) or at a server that never answers (a real hang). Whatever
needs Telegram to answer (a real code, a real session) is in tests/test_mtproxy_real.py (marker
``telegram_user``).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF, make_vault
from test_mtproxy_support import (
    MASKED,
    PHONE,
    USER_ID,
    actions,
    api_hash,
    assert_nowhere,
    clean,
    load_row,
    open_transactions_during_hang,
    secret_names,
    seed_connected,
    seed_pending,
)
from test_telegram_support import SilentServer, all_logs, free_closed_port

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_mtproxy_account import get_client_options, get_lock_wait_s, get_vault
from proxyhub.mtproxy import state, store
from proxyhub.mtproxy.userclient import ClientOptions, Endpoint
from proxyhub.reconcilers.base import lock_key
from proxyhub.vault import Vault, VaultDecryptError

pytestmark = [pytest.mark.db, pytest.mark.redis]

BASE = "/api/mtproxy/account"
PATHS = {
    ("GET", BASE),
    ("PUT", BASE),
    ("POST", f"{BASE}/code"),
    ("POST", f"{BASE}/password"),
    ("POST", f"{BASE}/logout"),
}


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean(eng)
    yield eng
    await clean(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


@pytest.fixture
def port() -> int:
    return free_closed_port()


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault, port: int) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    app.dependency_overrides[get_client_options] = lambda: ClientOptions(
        endpoint=Endpoint("127.0.0.1", port), deadline_s=5
    )
    app.dependency_overrides[get_lock_wait_s] = lambda: 0.5
    try:
        async with lifespan(app):
            await clean(engine, app.state.redis)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
            await clean(engine, app.state.redis)
    finally:
        for dep in (require_admin, get_vault, get_client_options, get_lock_wait_s):
            app.dependency_overrides.pop(dep, None)


def app_redis() -> Any:
    from proxyhub.api.app import app

    return app.state.redis


def login_body(**kw: Any) -> dict[str, Any]:
    return {"api_id": "1234567", "api_hash": api_hash(), "phone": PHONE, **kw}


def assert_error(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    assert body["error"] == code and body["message_fa"], body
    assert r.headers["cache-control"] == "no-store"
    return body


# --- protection ----------------------------------------------------------------------------------


async def test_every_route_requires_admin_and_csrf(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_mtproxy_account
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_mtproxy_account.router.routes:
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
    assert await secret_names(engine) == []


# --- status --------------------------------------------------------------------------------------


async def test_fresh_status(client: AsyncClient) -> None:
    r = await client.get(BASE)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["state"] == "none" and body["account"] is None and body["phone_masked"] is None
    assert body["kill_switch"] is True  # no fleet settings row: the switch is ON
    assert body["check_interval_min"] == 30


async def test_connected_status_shows_no_secret(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    api, session, _ = await seed_connected(engine, vault, port=1, mtproxybot_id=93372553)
    r = await client.get(BASE)
    body = r.json()
    assert body["state"] == "connected"
    assert body["account"] == {"first_name": "Operator", "user_id": USER_ID}
    assert body["phone_masked"] == MASKED == "+44•••••23"
    assert body["mtproxybot_ok"] is True and body["connected_at"] and body["last_check_at"]
    await assert_nowhere(
        engine, app_redis(), [api.api_hash.reveal(), session.reveal(), PHONE, PHONE[1:]], r.text
    )


# --- input checks (nothing reaches Telegram) -------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"api_id": "12ab"}, "api_id_format"),
        ({"api_id": "0"}, "api_id_format"),
        ({"api_id": "99999999999"}, "api_id_format"),
        ({"api_hash": "not-a-hash"}, "api_hash_format"),
        ({"api_hash": "a" * 31}, "api_hash_format"),
        ({"phone": "07700900123"}, "phone_format"),
        ({"phone": "+44 7700 900123 ext 4"}, "phone_format"),
    ],
)
async def test_put_refuses_bad_formats(
    client: AsyncClient, engine: AsyncEngine, body: dict[str, Any], code: str
) -> None:
    sent = login_body(**body)
    r = await client.put(BASE, json=sent)
    assert_error(r, 400, code)
    assert sent["api_hash"] not in r.text and "7700" not in r.text
    assert await secret_names(engine) == [] and await actions(engine) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"api_id": 1234567, "api_hash": "x"},
        {**login_body(), "extra": 1},
        {**login_body(), "api_id": True},
        {**login_body(), "api_id": 12.5},
        {**login_body(), "phone": 447700900123},
    ],
)
async def test_put_with_a_wrong_shape_is_invalid_request(client: AsyncClient, body: dict[str, Any]) -> None:
    r = await client.put(BASE, json=body)
    assert_error(r, 422, "invalid_request")
    assert PHONE not in r.text


@pytest.mark.parametrize(
    ("path", "body", "code"),
    [
        ("code", {"code": "12"}, "code_format"),
        ("code", {"code": "abcde"}, "code_format"),
        ("password", {"password": ""}, "password_format"),
        ("password", {"password": "x" * 300}, "password_format"),
    ],
)
async def test_code_and_password_formats(
    client: AsyncClient, path: str, body: dict[str, Any], code: str
) -> None:
    assert_error(await client.post(f"{BASE}/{path}", json=body), 400, code)


# --- step 1 against a Telegram that cannot be reached ------------------------------------------------


async def test_unreachable_telegram_stores_nothing_and_leaks_nothing(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    body = login_body()
    with all_logs(logging.DEBUG) as logs:
        r = await client.put(BASE, json=body)
    assert_error(r, 502, "telegram_unavailable")
    assert await secret_names(engine) == []
    assert (await load_row(engine)).state == "none"
    assert [k async for k in app_redis().scan_iter(match=store.PENDING_PREFIX + "*")] == []
    rows = await actions(engine)
    assert len(rows) == 1
    assert rows[0]["action"] == "mtproxy.account_login_failed" and rows[0]["target"] == MASKED
    assert rows[0]["reasons"] == {"reasons": ["telegram_unavailable"], "context": {"step": "send_code"}}
    assert rows[0]["allowed"] is False and rows[0]["actor"] == "admin:owner"
    await assert_nowhere(
        engine, app_redis(), [body["api_hash"], PHONE, PHONE[1:], "7700900123"], r.text, logs.getvalue()
    )


async def test_a_telegram_that_never_answers_ends_at_the_deadline(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    from proxyhub.api.app import app

    async with SilentServer() as silent:
        app.dependency_overrides[get_client_options] = lambda: ClientOptions(
            endpoint=Endpoint("127.0.0.1", silent.port), deadline_s=1.5
        )
        started = asyncio.get_running_loop().time()
        r = await client.put(BASE, json=login_body())
        took = asyncio.get_running_loop().time() - started
        assert silent.connections >= 1
    assert_error(r, 502, "telegram_unavailable")
    assert took < 15
    assert await secret_names(engine) == []


async def test_no_transaction_stays_open_while_telegram_is_awaited(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    from proxyhub.api.app import app

    async with SilentServer() as silent:
        app.dependency_overrides[get_client_options] = lambda: ClientOptions(
            endpoint=Endpoint("127.0.0.1", silent.port), deadline_s=3
        )
        call = asyncio.create_task(client.put(BASE, json=login_body()))
        seen = await open_transactions_during_hang(engine, call, silent, deadline_s=3)
        assert_error(await call, 502, "telegram_unavailable")
    assert seen and max(seen) == 0


# --- the 409 rules --------------------------------------------------------------------------------


async def test_a_second_login_is_refused_while_one_is_connected(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    _, session, before = await seed_connected(engine, vault, port=1)
    assert_error(await client.put(BASE, json=login_body()), 409, "mtproxy_connected")
    assert_error(await client.post(f"{BASE}/code", json={"code": "12345"}), 409, "mtproxy_connected")
    assert_error(await client.post(f"{BASE}/password", json={"password": "pw"}), 409, "mtproxy_connected")
    assert await load_row(engine) == before
    async with engine.connect() as conn:
        assert (await store.load_session(conn, vault)) is not None
        assert (await store.load_session(conn, vault)).reveal() == session.reveal()  # type: ignore[union-attr]
    assert await actions(engine) == []


async def test_code_or_password_without_a_login(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, port: int
) -> None:
    assert_error(await client.post(f"{BASE}/code", json={"code": "12345"}), 409, "mtproxy_no_login")
    assert_error(await client.post(f"{BASE}/password", json={"password": "pw"}), 409, "mtproxy_no_login")
    await seed_pending(engine, app_redis(), vault, port=port)  # step is "code"
    assert_error(await client.post(f"{BASE}/password", json={"password": "pw"}), 409, "mtproxy_no_login")


async def test_an_expired_login_is_ended(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, port: int
) -> None:
    pending = await seed_pending(
        engine, app_redis(), vault, port=port, now=datetime.now(UTC) - timedelta(minutes=6)
    )
    assert (await client.get(BASE)).json()["notice"] == "login_expired"
    assert_error(await client.post(f"{BASE}/code", json={"code": "12345"}), 409, "mtproxy_login_expired")
    assert (await load_row(engine)) == state.cleared()
    assert await app_redis().get(store.PENDING_PREFIX + pending.login_id) is None
    rows = await actions(engine)
    assert [r["action"] for r in rows] == ["mtproxy.account_login_failed"]
    assert rows[0]["reasons"]["reasons"] == ["mtproxy_login_expired"]


async def test_busy_while_another_operation_holds_the_lock(client: AsyncClient, engine: AsyncEngine) -> None:
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key(store.LOCK_NAME)})
        await holder.commit()
        try:
            assert_error(await client.put(BASE, json=login_body()), 409, "mtproxy_busy")
            assert_error(await client.post(f"{BASE}/logout"), 409, "mtproxy_busy")
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(store.LOCK_NAME)})
            await holder.commit()
    assert_error(await client.post(f"{BASE}/logout"), 409, "mtproxy_not_connected")  # the lock is free


async def test_without_redis_no_login_starts(client: AsyncClient, engine: AsyncEngine) -> None:
    from proxyhub.api.app import app

    redis = app.state.redis
    app.state.redis = None
    try:
        assert_error(await client.put(BASE, json=login_body()), 503, "state_unavailable")
    finally:
        app.state.redis = redis
    assert await actions(engine) == []


# --- steps 2 and 3 with a stored pending login ---------------------------------------------------------


async def test_the_pending_login_in_redis_is_sealed(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, port: int
) -> None:
    pending = await seed_pending(engine, app_redis(), vault, port=port)
    raw = await app_redis().get(store.PENDING_PREFIX + pending.login_id)
    assert raw is not None and 0 < await app_redis().ttl(store.PENDING_PREFIX + pending.login_id) <= 300
    needles = [
        pending.creds.api_hash.reveal(),
        pending.session.reveal(),
        pending.phone_code_hash.reveal(),
        PHONE,
        PHONE[1:],
    ]
    await assert_nowhere(engine, app_redis(), needles, raw.decode())
    assert store.open_pending(vault, pending.login_id, raw).session.reveal() == pending.session.reveal()
    with pytest.raises(VaultDecryptError):  # bound to its login id: it cannot be opened as another login
        store.open_pending(vault, secrets.token_hex(8), raw)
    r = await client.get(BASE)
    assert r.json()["state"] == "code_sent" and r.json()["login"]["code_via"] == "app"
    assert pending.phone_code_hash.reveal() not in r.text and pending.session.reveal() not in r.text


async def test_a_code_with_telegram_unreachable_keeps_the_login(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, port: int
) -> None:
    pending = await seed_pending(engine, app_redis(), vault, port=port)
    with all_logs(logging.DEBUG) as logs:
        r = await client.post(f"{BASE}/code", json={"code": "73519"})
    assert_error(r, 502, "telegram_unavailable")
    assert (await load_row(engine)).state == "code_sent"
    assert await app_redis().get(store.PENDING_PREFIX + pending.login_id) is not None
    rows = await actions(engine)
    assert [r["action"] for r in rows] == ["mtproxy.account_login_failed"]
    assert rows[0]["reasons"] == {"reasons": ["telegram_unavailable"], "context": {"step": "code"}}
    needles = [
        pending.creds.api_hash.reveal(),
        pending.session.reveal(),
        pending.phone_code_hash.reveal(),
        PHONE,
        "73519",
    ]
    await assert_nowhere(engine, app_redis(), needles, r.text, logs.getvalue())


async def test_a_password_with_telegram_unreachable_keeps_the_login(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, port: int
) -> None:
    password = "two step " + secrets.token_hex(4)
    pending = await seed_pending(engine, app_redis(), vault, port=port, step="password")
    with all_logs(logging.DEBUG) as logs:
        r = await client.post(f"{BASE}/password", json={"password": password})
    assert_error(r, 502, "telegram_unavailable")
    assert (await load_row(engine)).state == "password_needed"
    await assert_nowhere(
        engine, app_redis(), [password, pending.session.reveal(), PHONE], r.text, logs.getvalue()
    )


# --- logout ----------------------------------------------------------------------------------------------


async def test_logout_with_nothing_connected(client: AsyncClient, engine: AsyncEngine) -> None:
    assert_error(await client.post(f"{BASE}/logout"), 409, "mtproxy_not_connected")
    assert await actions(engine) == []


async def test_logout_cancels_a_login_in_progress(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, port: int
) -> None:
    pending = await seed_pending(engine, app_redis(), vault, port=port)
    r = await client.post(f"{BASE}/logout")
    assert r.status_code == 200 and r.json()["state"] == "none"
    assert await app_redis().get(store.PENDING_PREFIX + pending.login_id) is None
    rows = await actions(engine)
    assert [(r["action"], r["target"], r["reasons"]["reasons"]) for r in rows] == [
        ("mtproxy.account_login_cancelled", MASKED, ["owner"])
    ]


async def test_logout_keeps_everything_when_telegram_cannot_end_the_session(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, port: int
) -> None:
    _, _, before = await seed_connected(engine, vault, port=port)
    r = await client.post(f"{BASE}/logout")
    body = assert_error(r, 502, "logout_unavailable")
    assert "دستگاه‌ها" in body["message_fa"]
    assert await secret_names(engine) == [store.API_SECRET, store.SESSION_SECRET]
    assert await load_row(engine) == before
    assert await actions(engine) == []


async def test_logout_of_an_account_telegram_already_ended(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    _, _, row = await seed_connected(engine, vault, port=1)
    async with engine.begin() as conn:
        await store.drop_secrets(conn, session_only=True)
        await store.save_row(
            conn,
            state.after_check(row, code="session_terminated", rpc="SESSION_REVOKED", now=datetime.now(UTC)),
        )
    shown = (await client.get(BASE)).json()
    assert shown["state"] == "disconnected" and shown["disconnect_reason"] == "session_revoked"
    r = await client.post(f"{BASE}/logout")
    assert r.status_code == 200 and r.json()["state"] == "none"
    assert await secret_names(engine) == []
    rows = await actions(engine)
    assert [(r["action"], r["reasons"]["reasons"]) for r in rows] == [
        ("mtproxy.account_logged_out", ["session_already_ended"])
    ]
    assert rows[0]["reasons"]["context"] == {"user_id": USER_ID}


async def test_logout_when_the_session_cannot_be_decrypted(
    client: AsyncClient, engine: AsyncEngine, tmp_path: Path
) -> None:
    (tmp_path / "other").mkdir()
    other_key = make_vault(tmp_path / "other")  # stored with another master key
    await seed_connected(engine, other_key, port=1)
    r = await client.post(f"{BASE}/logout")
    assert r.status_code == 200
    assert await secret_names(engine) == []
    assert (await actions(engine))[0]["reasons"]["reasons"] == ["session_unreadable"]
