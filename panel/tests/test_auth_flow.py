"""Auth end to end: the real app over ASGI, a real PostgreSQL, real argon2id / AES-GCM / TOTP.

Nothing is imitated: TOTP codes are computed by pyotp from the secret the API returned, timeouts
are tested by moving timestamps in the database, and every test checks behaviour through HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from proxyhub.api.deps import AdminIdentity, ApiRoute, install_error_handlers, require_admin
from proxyhub.auth.service import create_setup_token
from proxyhub.auth.tokens import new_token
from proxyhub.config import get_config
from proxyhub.db.session import get_engine

pytestmark = pytest.mark.db

AdminRequired = Annotated[AdminIdentity, Depends(require_admin)]

CLIENT_IP = "203.0.113.77"  # what the ASGI server reports as the peer; must never be stored or logged
BASE = "https://panel.test"
UA = "Mozilla/5.0 (Linux; Android 15) ProxyHubTest/1.0"
CSRF = {"X-Requested-With": "proxyhub"}
USERNAME = "owner"
PASSWORD = "Kite!river-7-Moss_lamp"
AUTH_TABLES = (
    "admin_sessions",
    "admin_recovery_codes",
    "admin_enrollments",
    "admin_setup_tokens",
    "admin_users",
    "auth_throttle",
)


@dataclass
class Env:
    engine: AsyncEngine

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []

    async def new_setup_token(self, now: datetime | None = None) -> str:
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            return await create_setup_token(session, now=now)

    async def fresh_code(self, secret: str) -> str:
        """Forget the last used TOTP step so the current code is accepted again (one login per
        30 s otherwise). The replay rule itself is tested separately without this."""
        await self.rows("UPDATE admin_users SET totp_last_step = NULL")
        return pyotp.TOTP(secret).now()

    async def dump(self) -> str:
        """Every auth row and every audit row as JSON text (bytea shows as hex)."""
        parts: list[str] = []
        async with self.engine.connect() as conn:
            for table in (*AUTH_TABLES, "actions_log"):
                result = await conn.execute(text(f"SELECT row_to_json(t)::text FROM {table} t"))  # noqa: S608
                parts.extend(result.scalars().all())
        return "\n".join(parts)

    async def audit(self, action: str) -> list[Any]:
        return await self.rows(
            "SELECT target, allowed, reasons, dry_run FROM actions_log "
            "WHERE actor = 'auth' AND action = :a ORDER BY id",
            a=action,
        )


@dataclass
class Owner:
    secret: str
    recovery_codes: list[str]
    cookie: str
    setup_token: str


@pytest.fixture
async def env(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    key = tmp_path / "master_key"
    key.write_text(secrets.token_hex(32))
    key.chmod(0o600)
    monkeypatch.setenv("PROXYHUB_MASTER_KEY_FILE", str(key))
    get_config.cache_clear()
    engine = create_async_engine(migrated_db)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(AUTH_TABLES)}"))
        await conn.execute(text("DELETE FROM actions_log WHERE actor = 'auth'"))
    try:
        yield Env(engine)
    finally:
        await engine.dispose()
        await get_engine().dispose()
        get_config.cache_clear()


def make_client(app: FastAPI | None = None, *, csrf: bool = True) -> AsyncClient:
    from proxyhub.api.app import app as real_app

    transport = ASGITransport(app=app or real_app, client=(CLIENT_IP, 4242))
    headers = {"User-Agent": UA, **(CSRF if csrf else {})}
    return AsyncClient(transport=transport, base_url=BASE, headers=headers)


@pytest.fixture
async def client(env: Env) -> AsyncIterator[AsyncClient]:
    async with make_client() as c:
        yield c


async def do_setup(env: Env, client: AsyncClient, username: str = USERNAME) -> Owner:
    token = await env.new_setup_token()
    r = await client.post("/api/setup/begin", json={"token": token})
    assert r.status_code == 200, r.text
    secret = r.json()["totp_secret"]
    r = await client.post(
        "/api/setup/finish",
        json={
            "token": token,
            "enrollment_id": r.json()["enrollment_id"],
            "username": username,
            "password": PASSWORD,
            "totp_code": pyotp.TOTP(secret).now(),
        },
    )
    assert r.status_code == 200, r.text
    cookie = client.cookies.get("ph_session")
    assert cookie
    return Owner(secret=secret, recovery_codes=r.json()["recovery_codes"], cookie=cookie, setup_token=token)


async def login(client: AsyncClient, **body: Any) -> Response:
    payload = {"username": USERNAME, "password": PASSWORD, **body}
    return await client.post("/api/auth/login", json=payload)


def assert_error(r: Response, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    body: dict[str, Any] = r.json()
    assert body["error"] == code
    assert body["message_fa"] and any("؀" <= ch <= "ۿ" for ch in body["message_fa"])
    return body


def session_cookie_attrs(r: Response) -> set[str]:
    headers = [h for h in r.headers.get_list("set-cookie") if h.startswith("ph_session=")]
    assert len(headers) == 1
    return {part.strip().lower() for part in headers[0].split(";")[1:]}


async def cli_setup_link(capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    from proxyhub.cli import main

    code = await asyncio.to_thread(main, ["setup-link", "--base-url", "https://panel.beyra-ai.com/"])
    out, err = capsys.readouterr()
    return code, out.strip(), err.strip()


# --- setup ---------------------------------------------------------------------------------


async def test_full_setup_flow_without_leaking_secrets(
    env: Env, client: AsyncClient, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)  # everything, including SQL statements and their parameters
    r = await client.get("/api/setup/status")
    assert r.status_code == 200 and r.json() == {"setup_required": True}

    code, link, _ = await cli_setup_link(capsys)
    assert code == 0
    parts = urlsplit(link)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://panel.beyra-ai.com/setup"
    token = parts.fragment
    assert len(token) == 43

    async with make_client(csrf=False) as no_csrf:
        assert_error(await no_csrf.post("/api/setup/begin", json={"token": token}), 403, "csrf")
    assert_error(
        await client.post("/api/setup/begin", json={"token": new_token()}), 404, "setup_token_invalid"
    )

    r = await client.post("/api/setup/begin", json={"token": token})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    begin = r.json()
    secret = begin["totp_secret"]
    parsed = pyotp.parse_uri(begin["totp_uri"])
    assert parsed.secret == secret and parsed.issuer == "IPOXXO Panel"

    finish = {"token": token, "enrollment_id": begin["enrollment_id"], "username": " Owner "}
    totp_now = pyotp.TOTP(secret)
    r = await client.post(
        "/api/setup/finish", json={**finish, "password": "password1234", "totp_code": totp_now.now()}
    )
    assert_error(r, 400, "weak_password")
    r = await client.post(
        "/api/setup/finish",
        json={**finish, "username": "bad name!", "password": PASSWORD, "totp_code": totp_now.now()},
    )
    assert_error(r, 400, "invalid_username")
    stale = totp_now.at(datetime.now(UTC) - timedelta(minutes=10))
    r = await client.post("/api/setup/finish", json={**finish, "password": PASSWORD, "totp_code": stale})
    assert_error(r, 400, "totp_invalid")

    r = await client.post(
        "/api/setup/finish", json={**finish, "password": PASSWORD, "totp_code": totp_now.now()}
    )
    assert r.status_code == 200, r.text
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    assert {"httponly", "secure", "samesite=strict", "path=/"} <= session_cookie_attrs(r)
    first_cookie = client.cookies.get("ph_session")
    assert first_cookie

    r = await client.get("/api/auth/me")
    assert r.status_code == 200 and r.json()["username"] == "owner"

    # The link is dead now, and no second admin can ever be set up.
    r = await client.post(
        "/api/setup/finish", json={**finish, "password": PASSWORD, "totp_code": totp_now.now()}
    )
    assert_error(r, 404, "setup_token_invalid")
    assert_error(await client.post("/api/setup/begin", json={"token": token}), 404, "setup_token_invalid")
    assert (await client.get("/api/setup/status")).json() == {"setup_required": False}
    code, out, err = await cli_setup_link(capsys)
    assert code == 1 and out == "" and "already exists" in err

    # Log in with the next TOTP step (setup used the current one); the same code twice is refused.
    async with make_client() as other:
        next_code = totp_now.at(datetime.now(UTC) + timedelta(seconds=30))
        r = await login(other, totp_code=next_code)
        assert r.status_code == 200 and r.json() == {"username": "owner"}
        second_cookie = other.cookies.get("ph_session")
        assert second_cookie and second_cookie != first_cookie
    async with make_client() as replay:
        assert_error(await login(replay, totp_code=next_code), 401, "invalid_credentials")

    secrets_handed_out = [PASSWORD, token, secret, first_cookie, second_cookie, *codes]
    secrets_handed_out += [c.replace("-", "") for c in codes]
    log_text = caplog.text + "\n".join(str(rec.args) for rec in caplog.records)
    for value in secrets_handed_out:
        assert value not in log_text
    assert CLIENT_IP not in log_text

    stored = await env.dump()
    for value in secrets_handed_out:
        assert value not in stored
    assert CLIENT_IP not in stored
    (user,) = await env.rows("SELECT username, password_hash, totp_last_step FROM admin_users")
    assert user.username == "owner"
    assert user.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert user.totp_last_step is not None
    assert [row.target for row in await env.audit("setup_completed")] == ["owner"]
    assert len(await env.audit("login_ok")) == 1
    assert all(row.dry_run is False for row in await env.audit("login_failed"))


async def test_setup_token_expired_wrong_superseded_and_two_tabs(env: Env, client: AsyncClient) -> None:
    expired = await env.new_setup_token(now=datetime.now(UTC) - timedelta(minutes=61))
    assert_error(await client.post("/api/setup/begin", json={"token": expired}), 404, "setup_token_invalid")
    for wrong in ("abc", new_token(), ""):
        assert_error(await client.post("/api/setup/begin", json={"token": wrong}), 404, "setup_token_invalid")

    older = await env.new_setup_token()
    newer = await env.new_setup_token()  # a new link kills the older one
    assert_error(await client.post("/api/setup/begin", json={"token": older}), 404, "setup_token_invalid")

    # The owner opened the link in two tabs: both enrollments stay valid until one finishes.
    tab1 = (await client.post("/api/setup/begin", json={"token": newer})).json()
    tab2 = (await client.post("/api/setup/begin", json={"token": newer})).json()
    assert tab1["enrollment_id"] != tab2["enrollment_id"] and tab1["totp_secret"] != tab2["totp_secret"]
    body = {"token": newer, "username": USERNAME, "password": PASSWORD}
    r = await client.post(
        "/api/setup/finish",
        json={**body, "enrollment_id": "0b5f7c1e-0000-4000-8000-000000000000", "totp_code": "123456"},
    )
    assert_error(r, 404, "setup_token_invalid")
    # A code from tab 2's secret does not finish tab 1's enrollment.
    r = await client.post(
        "/api/setup/finish",
        json={
            **body,
            "enrollment_id": tab1["enrollment_id"],
            "totp_code": pyotp.TOTP(tab2["totp_secret"]).now(),
        },
    )
    assert_error(r, 400, "totp_invalid")
    r = await client.post(
        "/api/setup/finish",
        json={
            **body,
            "enrollment_id": tab1["enrollment_id"],
            "totp_code": pyotp.TOTP(tab1["totp_secret"]).now(),
        },
    )
    assert r.status_code == 200, r.text
    assert await env.rows("SELECT id FROM admin_enrollments") == []  # pending secrets are wiped

    # The enrolled secret is the one that works for login.
    async with make_client() as c:
        r = await login(c, totp_code=await env.fresh_code(tab1["totp_secret"]))
        assert r.status_code == 200

    # Single use on its own, not only because an admin exists: even if the admin row were removed
    # (a manual reset), the used link stays dead.
    await env.rows("DELETE FROM admin_users")
    assert (await client.get("/api/setup/status")).json() == {"setup_required": True}
    assert_error(await client.post("/api/setup/begin", json={"token": newer}), 404, "setup_token_invalid")


async def test_concurrent_setup_finish_creates_exactly_one_admin(env: Env, client: AsyncClient) -> None:
    token = await env.new_setup_token()
    begin = (await client.post("/api/setup/begin", json={"token": token})).json()
    body = {
        "token": token,
        "enrollment_id": begin["enrollment_id"],
        "password": PASSWORD,
        "totp_code": pyotp.TOTP(begin["totp_secret"]).now(),
    }
    async with make_client() as a, make_client() as b:
        ra, rb = await asyncio.gather(
            a.post("/api/setup/finish", json={**body, "username": "first"}),
            b.post("/api/setup/finish", json={**body, "username": "second"}),
        )
    assert sorted([ra.status_code, rb.status_code]) == [200, 404]
    assert len(await env.rows("SELECT id FROM admin_users")) == 1


# --- login ---------------------------------------------------------------------------------


async def test_login_success_and_generic_failures(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    async with make_client() as c:
        wrong_password = await login(
            c, password="Kite!river-7-Moss_lamP", totp_code=await env.fresh_code(owner.secret)
        )
        unknown_user = await login(c, username="nobody", totp_code=pyotp.TOTP(owner.secret).now())
        no_second_factor = await login(c)
        wrong_code = await login(
            c, totp_code=pyotp.TOTP(owner.secret).at(datetime.now(UTC) - timedelta(minutes=5))
        )
        bad_format = await login(c, username="x", totp_code="123456")
        bodies = [
            assert_error(r, 401, "invalid_credentials")
            for r in (wrong_password, unknown_user, no_second_factor, wrong_code, bad_format)
        ]
        assert all(b == bodies[0] for b in bodies)  # identical answers: nothing to learn from them
        assert c.cookies.get("ph_session") is None

        r = await login(c, username="  OWNER ", totp_code=await env.fresh_code(owner.secret))
        assert r.status_code == 200 and r.json() == {"username": "owner"}
        assert {"httponly", "secure", "samesite=strict", "path=/"} <= session_cookie_attrs(r)
        assert c.cookies.get("ph_session") not in (None, owner.cookie)  # a new session id on login
        me = (await c.get("/api/auth/me")).json()
        assert me["username"] == "owner" and me["last_login_at"] and me["created_at"]

    failed = await env.audit("login_failed")
    assert [row.reasons["factor"] for row in failed] == [
        "password",
        "unknown_user",
        "second_factor",
        "second_factor",
        "unknown_user",
    ]
    assert [row.target for row in failed] == ["owner", None, "owner", "owner", None]
    assert all(row.allowed is False for row in failed)
    ok = await env.audit("login_ok")
    assert [(row.target, row.reasons["method"]) for row in ok] == [("owner", "totp")]


async def test_totp_code_cannot_be_replayed(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    code = await env.fresh_code(owner.secret)
    async with make_client() as a:
        assert (await login(a, totp_code=code)).status_code == 200
    async with make_client() as b:
        assert_error(await login(b, totp_code=code), 401, "invalid_credentials")
    (row,) = await env.rows("SELECT totp_last_step FROM admin_users")
    assert row.totp_last_step >= int(datetime.now(UTC).timestamp()) // 30 - 1


async def test_recovery_code_is_single_use(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    first, second = owner.recovery_codes[0], owner.recovery_codes[1]
    async with make_client() as c:
        r = await login(c, recovery_code=first.replace("-", "").lower())
        assert r.status_code == 200, r.text
        assert_error(await login(c, recovery_code=first), 401, "invalid_credentials")
        assert_error(
            await login(c, password="wrong-password-12", recovery_code=second), 401, "invalid_credentials"
        )
        assert (await login(c, recovery_code=second)).status_code == 200
    used = await env.audit("recovery_used")
    assert [row.reasons["remaining"] for row in used] == [9, 8]
    assert [row.reasons["method"] for row in await env.audit("login_ok")] == ["recovery", "recovery"]


async def test_regenerate_recovery_codes(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    async with make_client(csrf=False) as no_csrf:
        no_csrf.cookies.set("ph_session", owner.cookie)
        r = await no_csrf.post("/api/auth/recovery-codes/regenerate", json={"totp_code": "123456"})
        assert_error(r, 403, "csrf")
    stale = pyotp.TOTP(owner.secret).at(datetime.now(UTC) - timedelta(minutes=5))
    r = await client.post("/api/auth/recovery-codes/regenerate", json={"totp_code": stale})
    assert_error(r, 400, "totp_invalid")
    r = await client.post(
        "/api/auth/recovery-codes/regenerate", json={"totp_code": await env.fresh_code(owner.secret)}
    )
    assert r.status_code == 200, r.text
    new_codes = r.json()["recovery_codes"]
    assert len(new_codes) == 10 and not set(new_codes) & set(owner.recovery_codes)
    async with make_client() as c:
        assert_error(await login(c, recovery_code=owner.recovery_codes[0]), 401, "invalid_credentials")
        assert (await login(c, recovery_code=new_codes[0])).status_code == 200
    assert len(await env.audit("recovery_codes_regenerated")) == 1
    assert len(await env.audit("recovery_regenerate_failed")) == 1


async def test_password_hash_is_upgraded_on_login(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    await env.rows("UPDATE admin_users SET password_hash = :h", h=weak)
    async with make_client() as c:
        assert (await login(c, totp_code=await env.fresh_code(owner.secret))).status_code == 200
    (row,) = await env.rows("SELECT password_hash FROM admin_users")
    assert row.password_hash != weak and row.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


async def test_validation_errors_use_contract_shape_and_never_echo_input(
    env: Env, client: AsyncClient
) -> None:
    long_password = "P" + secrets.token_urlsafe(1500)
    r = await client.post("/api/auth/login", json={"username": "owner", "password": long_password})
    body = assert_error(r, 422, "invalid_request")
    assert long_password not in r.text and set(body) == {"error", "message_fa"}
    assert_error(await client.post("/api/auth/login", json={"username": "owner"}), 422, "invalid_request")
    assert_error(await client.post("/api/auth/login", content=b"not json"), 422, "invalid_request")


# --- brute force ---------------------------------------------------------------------------


async def expire_user_locks(env: Env) -> None:
    await env.rows(
        "UPDATE auth_throttle SET locked_until = now() - interval '1 second' "
        "WHERE key <> 'global' AND locked_until IS NOT NULL"
    )


async def test_lockout_429_and_doubling(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    async with make_client() as c:
        for _ in range(4):
            assert_error(await login(c, password="wrong-password-12"), 401, "invalid_credentials")
        r = await login(c, password="wrong-password-12")
        body = assert_error(r, 429, "locked")
        assert 899 <= body["retry_after_s"] <= 900
        assert r.headers["retry-after"] == str(body["retry_after_s"])
        # Even the right password and code are refused while locked.
        r = await login(c, totp_code=await env.fresh_code(owner.secret))
        assert assert_error(r, 429, "locked")["retry_after_s"] <= 900

        await expire_user_locks(env)
        for _ in range(4):
            assert_error(await login(c, password="wrong-password-12"), 401, "invalid_credentials")
        body = assert_error(await login(c, password="wrong-password-12"), 429, "locked")
        assert 1799 <= body["retry_after_s"] <= 1800  # second lock: doubled

        await expire_user_locks(env)
        assert (await login(c, totp_code=await env.fresh_code(owner.secret))).status_code == 200

        # Unknown usernames lock exactly the same way (no account enumeration).
        for _ in range(4):
            assert_error(await login(c, username="ghost"), 401, "invalid_credentials")
        assert_error(await login(c, username="ghost"), 429, "locked")

    locks = await env.audit("locked")
    assert [(row.reasons["scope"], row.reasons["level"], row.reasons["lock_s"]) for row in locks] == [
        ("user", 1, 900),
        ("user", 2, 1800),
        ("user", 1, 900),
    ]
    assert [row.target for row in locks] == ["owner", "owner", None]
    keys = [
        row.key for row in await env.rows("SELECT key, lock_level FROM auth_throttle WHERE lock_level = 0")
    ]
    assert "global" in keys and len(keys) == 2  # the owner's row was reset by the successful login
    assert all(k == "global" or (k.startswith("u:") and "owner" not in k and "ghost" not in k) for k in keys)


async def test_global_lock_stops_everyone(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    await env.rows(
        "INSERT INTO auth_throttle (key, failures, window_started_at) VALUES ('global', 49, now()) "
        "ON CONFLICT (key) DO UPDATE SET failures = 49, window_started_at = now()"
    )
    async with make_client() as c:
        body = assert_error(await login(c, username="someone-else"), 429, "locked")
        assert 899 <= body["retry_after_s"] <= 900
        assert_error(await login(c, totp_code=await env.fresh_code(owner.secret)), 429, "locked")
    (lock,) = await env.audit("locked")
    assert lock.reasons["scope"] == "global" and lock.target is None


async def test_parallel_guesses_cannot_exceed_the_threshold(env: Env, client: AsyncClient) -> None:
    await do_setup(env, client)
    async with make_client() as c:
        results = await asyncio.gather(*(login(c, password=f"wrong-password-{i:02d}") for i in range(8)))
    statuses = sorted(r.status_code for r in results)
    assert statuses == [401, 401, 401, 401, 429, 429, 429, 429]


async def test_idle_throttle_rows_are_pruned_without_waiting_on_busy_ones(
    env: Env, client: AsyncClient
) -> None:
    await env.rows(
        "INSERT INTO auth_throttle (key, failures, last_failure_at, locked_until) VALUES "
        "('u:old-idle', 1, now() - interval '3 days', NULL), "
        "('u:old-busy', 1, now() - interval '3 days', NULL), "
        "('u:recent', 1, now() - interval '1 hour', NULL), "
        "('u:still-locked', 0, now() - interval '3 days', now() + interval '1 hour')"
    )
    # Another attempt in flight holds 'u:old-busy': pruning must skip it, not wait for it.
    async with env.engine.connect() as busy:
        await busy.execute(text("SELECT key FROM auth_throttle WHERE key = 'u:old-busy' FOR UPDATE"))
        async with make_client() as c:
            r = await asyncio.wait_for(login(c, username="someone"), timeout=10)
        assert_error(r, 401, "invalid_credentials")
        await busy.rollback()
    keys = {row.key for row in await env.rows("SELECT key FROM auth_throttle")}
    assert "u:old-idle" not in keys
    assert {"u:old-busy", "u:recent", "u:still-locked", "global"} <= keys


# --- CSRF and sessions ---------------------------------------------------------------------


async def test_csrf_header_required_on_every_non_get(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    async with make_client(csrf=False) as raw:
        assert_error(await login(raw, totp_code="123456"), 403, "csrf")
        r = await raw.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert_error(r, 403, "csrf")
        raw.cookies.set("ph_session", owner.cookie)
        assert_error(await raw.post("/api/auth/logout"), 403, "csrf")
        assert_error(await raw.delete(f"/api/auth/sessions/{secrets.token_hex(4)}"), 403, "csrf")
        assert (await raw.get("/api/auth/me")).status_code == 200  # GET needs no header
    assert (await client.get("/api/auth/me")).status_code == 200  # the session survived


async def test_session_idle_and_absolute_expiry(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    (row,) = await env.rows("SELECT created_at, expires_at FROM admin_sessions")
    assert row.expires_at - row.created_at == timedelta(days=7)

    await env.rows("UPDATE admin_sessions SET last_seen_at = now() - interval '11 hours 59 minutes'")
    assert (await client.get("/api/auth/me")).status_code == 200
    (row,) = await env.rows("SELECT now() - last_seen_at AS age FROM admin_sessions")
    assert row.age < timedelta(minutes=1)  # activity keeps the session alive

    await env.rows("UPDATE admin_sessions SET last_seen_at = now() - interval '12 hours 1 minute'")
    assert_error(await client.get("/api/auth/me"), 401, "unauthenticated")

    async with make_client() as c:
        assert (await login(c, totp_code=await env.fresh_code(owner.secret))).status_code == 200
        await env.rows(
            "UPDATE admin_sessions SET created_at = now() - interval '7 days 1 minute' "
            "WHERE revoked_at IS NULL"
        )
        assert_error(await c.get("/api/auth/me"), 401, "unauthenticated")
    async with make_client() as c:
        assert (await login(c, totp_code=await env.fresh_code(owner.secret))).status_code == 200
        await env.rows("UPDATE admin_sessions SET expires_at = now() - interval '1 second'")
        assert_error(await c.get("/api/auth/me"), 401, "unauthenticated")


async def test_sessions_list_revoke_and_logout(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)
    async with make_client() as other:
        assert (await login(other, totp_code=await env.fresh_code(owner.secret))).status_code == 200
        listed = (await client.get("/api/auth/sessions")).json()
        assert len(listed) == 2 and [s["current"] for s in listed].count(True) == 1
        assert all(set(s) == {"id", "created_at", "last_seen_at", "user_agent", "current"} for s in listed)
        assert all(s["user_agent"] == UA for s in listed)
        other_id = next(s["id"] for s in listed if not s["current"])

        assert (await client.delete(f"/api/auth/sessions/{other_id}")).status_code == 204
        assert_error(await other.get("/api/auth/me"), 401, "unauthenticated")
        assert_error(await client.delete(f"/api/auth/sessions/{other_id}"), 404, "not_found")
        assert_error(await client.delete("/api/auth/sessions/not-a-uuid"), 404, "not_found")
        assert len((await client.get("/api/auth/sessions")).json()) == 1
    (revoked,) = await env.audit("session_revoked")
    assert revoked.target == f"session:{other_id}" and revoked.reasons == {"own_session": False}

    r = await client.post("/api/auth/logout")
    assert r.status_code == 204
    assert "max-age=0" in session_cookie_attrs(r)
    assert client.cookies.get("ph_session") is None
    async with make_client() as replay:
        replay.cookies.set("ph_session", owner.cookie)  # the old cookie is dead on the server too
        assert_error(await replay.get("/api/auth/me"), 401, "unauthenticated")
    assert [row.target for row in await env.audit("logout")] == ["owner"]


async def test_require_admin_protects_a_dummy_route(env: Env, client: AsyncClient) -> None:
    owner = await do_setup(env, client)

    def build(with_handlers: bool) -> FastAPI:
        app = FastAPI()
        router = APIRouter(prefix="/api")

        @router.get("/dummy")
        async def dummy_get(admin: AdminRequired) -> dict[str, str]:
            return {"username": admin.username, "session_id": admin.session_id}

        @router.post("/dummy")
        async def dummy_post(admin: AdminRequired) -> dict[str, str]:
            return {"username": admin.username}

        app.include_router(router)
        if with_handlers:
            install_error_handlers(app)
        return app

    app = build(with_handlers=True)
    async with make_client(app) as anon:
        assert_error(await anon.get("/api/dummy"), 401, "unauthenticated")
        anon.cookies.set("ph_session", "garbage")
        assert_error(await anon.get("/api/dummy"), 401, "unauthenticated")
        anon.cookies.set("ph_session", new_token())
        assert_error(await anon.get("/api/dummy"), 401, "unauthenticated")
    async with make_client(app) as admin:
        admin.cookies.set("ph_session", owner.cookie)
        r = await admin.get("/api/dummy")
        assert r.status_code == 200 and r.json()["username"] == "owner"
        (row,) = await env.rows("SELECT id FROM admin_sessions WHERE revoked_at IS NULL")
        assert r.json()["session_id"] == str(row.id)
        assert (await admin.post("/api/dummy")).status_code == 200
    async with make_client(app, csrf=False) as admin_no_csrf:
        admin_no_csrf.cookies.set("ph_session", owner.cookie)
        assert_error(await admin_no_csrf.post("/api/dummy"), 403, "csrf")

    # Without the app-wide handler the status codes are still right (body nested under "detail").
    async with make_client(build(with_handlers=False)) as anon:
        r = await anon.get("/api/dummy")
        assert r.status_code == 401 and r.json()["detail"]["error"] == "unauthenticated"

    # A router built with ApiRoute renders the contract shape on its own.
    app = FastAPI()
    router = APIRouter(prefix="/api", route_class=ApiRoute)

    @router.get("/dummy")
    async def dummy(admin: AdminRequired) -> dict[str, str]:
        return {"username": admin.username}

    app.include_router(router)
    async with make_client(app) as anon:
        assert_error(await anon.get("/api/dummy"), 401, "unauthenticated")

    # And the real app's own protected routes.
    async with make_client() as anon:
        for method, path in (
            ("GET", "/api/auth/me"),
            ("GET", "/api/auth/sessions"),
            ("POST", "/api/auth/logout"),
        ):
            assert_error(await anon.request(method, path), 401, "unauthenticated")


async def test_auth_tables_have_no_ip_columns(env: Env) -> None:
    rows = await env.rows(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = ANY(:t)",
        t=list(AUTH_TABLES),
    )
    assert rows
    assert not [r for r in rows if "ip" in r.column_name.split("_") or r.data_type in ("inet", "cidr")]
