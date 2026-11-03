"""/api/telegram/* over real HTTP (ASGI transport) with a real PostgreSQL and Redis.

Telegram itself is never imitated (D10): a token-shaped random value sent to the real Bot API gets
its real 401; a local port nobody listens on gives a real connection error. Everything that needs a
real bot (getMe success, real channels) is in tests/test_telegram_real.py (marker ``telegram``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF, assert_absent_from_database, make_vault
from test_telegram_support import (
    all_logs,
    assert_no_token,
    clean_telegram_tables,
    closed_port_bot,
    random_token,
    real_bot,
    telegram_reachable,
)

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_telegram import get_bot_factory, get_vault
from proxyhub.telegram import pairing, store
from proxyhub.vault import SecretValue, Vault

pytestmark = [pytest.mark.db, pytest.mark.redis]

TG_PATHS = {
    ("GET", "/api/telegram/status"),
    ("PUT", "/api/telegram/bot"),
    ("POST", "/api/telegram/pairing"),
    ("PUT", "/api/telegram/channel"),
    ("GET", "/api/telegram/settings"),
    ("PUT", "/api/telegram/settings"),
    ("POST", "/api/telegram/test-alert"),
    ("POST", "/api/telegram/test-post"),
}
BOT_ID = 8123456789
BOT_USERNAME = "proxyhub_test_bot"
OWNER_CHAT = 111222333
TEST_CHANNEL = {
    "chat_id": -1009876543210,
    "username": "-1009876543210",
    "title": "Proxy-Hub test",
    "can_post": True,
    "can_edit": True,
    "can_delete": False,
    "checked_at": "2026-11-03T20:00:00Z",
}


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_telegram_tables(eng)
    yield eng
    await clean_telegram_tables(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    app.dependency_overrides[get_bot_factory] = closed_port_bot  # no accidental real call
    try:
        async with lifespan(app):
            await app.state.redis.delete("proxyhub:telegram:pairing")
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        for dep in (require_admin, get_vault, get_bot_factory):
            app.dependency_overrides.pop(dep, None)


def use_real_telegram() -> None:
    from proxyhub.api.app import app

    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    app.dependency_overrides[get_bot_factory] = lambda: real_bot


async def configure(
    engine: AsyncEngine,
    vault: Vault,
    *,
    token: str | None = None,
    owner: bool = False,
    test_channel: bool = False,
) -> str:
    """Bot (and optionally owner chat and test channel) stored directly in our own database."""
    token = token or random_token()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=BOT_ID, username=BOT_USERNAME),
            token=SecretValue(token),
            now=now,
        )
        if owner:
            await store.set_owner_chat(conn, OWNER_CHAT, now)
        if test_channel:
            await store.save_channel(conn, "test", dict(TEST_CHANNEL))
    return token


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


# --- protection ----------------------------------------------------------------------------------


async def test_every_telegram_route_requires_admin(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_telegram
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_telegram.api.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            found.add((method, route.path))
            assert require_admin in {d.call for d in route.dependant.dependencies}, route.path
    assert found == TG_PATHS
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(TG_PATHS):
                r = await c.request(method, path, json={})
                assert r.status_code == 401, (method, path, r.status_code)
                assert r.json()["error"] == "unauthenticated"
                if method != "GET":  # require_admin checks CSRF first
                    r = await c.request(method, path, json={}, headers={"X-Requested-With": "nope"})
                    assert r.status_code == 403 and r.json()["error"] == "csrf", path


# --- status --------------------------------------------------------------------------------------


async def test_fresh_status(client: AsyncClient) -> None:
    r = await client.get("/api/telegram/status")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "bot": None,
        "owner_paired": False,
        "pairing": None,
        "channels": {"main": None, "test": None},
        "outbox": {"pending": 0, "failed": 0},
        "last_alert_at": None,
        "bot_error": None,
    }


async def test_status_shows_a_conflicting_poller(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    await configure(engine, vault)
    async with engine.begin() as conn:
        await store.put_setting(
            conn, store.KEY_UPDATES, {"bot_id": BOT_ID, "offset": None, "error": "updates_conflict"}
        )
    assert (await client.get("/api/telegram/status")).json()["bot_error"] == "updates_conflict"
    async with engine.begin() as conn:
        await store.set_health(conn, "bot_unauthorized", datetime.now(UTC))
    assert (await client.get("/api/telegram/status")).json()["bot_error"] == "bot_unauthorized"


async def test_status_after_setup(client: AsyncClient, engine: AsyncEngine, vault: Vault) -> None:
    token = await configure(engine, vault, owner=True, test_channel=True)
    async with engine.begin() as conn:
        await store.enqueue(conn, "alert", {"text": "x"})
        failed = await store.enqueue(conn, "alert", {"text": "y"})
        await conn.execute(
            text("UPDATE outbox SET done_at = now(), last_error = 'failed: x' WHERE id = :i"), {"i": failed}
        )
        await store.mark_alert_delivered(conn, datetime(2026, 11, 3, 20, 5, tzinfo=UTC))
    r = await client.get("/api/telegram/status")
    body = r.json()
    assert body["bot"] == {"username": BOT_USERNAME, "id": BOT_ID}
    assert body["owner_paired"] is True
    assert body["channels"]["main"] is None
    assert body["channels"]["test"] == {
        "username": "-1009876543210",
        "title": "Proxy-Hub test",
        "can_post": True,
        "can_edit": True,
        "can_delete": False,
        "checked_at": "2026-11-03T20:00:00Z",
        "chat_id": -1009876543210,
    }
    assert body["outbox"] == {"pending": 1, "failed": 1}
    assert body["last_alert_at"] == "2026-11-03T20:05:00Z"
    assert_no_token(token, r.text)


# --- bot token -----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "not-a-token", "123:abc", "12345:" + "x" * 31 + "/../getMe"])
async def test_malformed_token_is_refused_without_a_request(
    client: AsyncClient, engine: AsyncEngine, bad: str
) -> None:
    r = await client.put("/api/telegram/bot", json={"token": bad})
    assert r.status_code in (400, 422)
    assert r.json()["error"] in ("bot_token_invalid", "invalid_request")
    assert r.json()["message_fa"]
    assert await rows(engine, "SELECT name FROM secrets WHERE name LIKE 'telegram:%'") == []


async def test_unreachable_telegram_is_502_and_nothing_stored(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    token = random_token()
    with all_logs() as logs:
        r = await client.put("/api/telegram/bot", json={"token": token})
    assert r.status_code == 502 and r.json()["error"] == "telegram_unavailable"
    assert await rows(engine, "SELECT name FROM secrets WHERE name LIKE 'telegram:%'") == []
    assert_no_token(token, r.text, logs.getvalue())
    await assert_absent_from_database(engine, token.split(":", 1)[1])


async def test_rejected_token_with_real_telegram_leaks_nowhere(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    use_real_telegram()
    token = random_token()
    with all_logs() as logs:
        r = await client.put("/api/telegram/bot", json={"token": token})
    assert r.status_code == 400 and r.json()["error"] == "bot_token_invalid"
    log_text = logs.getvalue()
    assert "api.telegram.org/bot<redacted>/getMe" in log_text  # httpx's INFO line, redacted
    assert_no_token(token, r.text, log_text)
    await assert_absent_from_database(engine, token)
    await assert_absent_from_database(engine, token.split(":", 1)[1])


# --- pairing -------------------------------------------------------------------------------------


async def test_pairing_needs_a_bot(client: AsyncClient) -> None:
    r = await client.post("/api/telegram/pairing")
    assert r.status_code == 409 and r.json()["error"] == "bot_not_set"


async def test_pairing_code_is_shown_but_only_its_hash_is_stored(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    await configure(engine, vault)
    before = datetime.now(UTC)
    r = await client.post("/api/telegram/pairing")
    assert r.status_code == 200
    body = r.json()
    code = body["code"]
    assert pairing.normalize(code) == code
    assert body["link"] == f"https://t.me/{BOT_USERNAME}?start={code}"
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert timedelta(minutes=9, seconds=58) <= expires - before <= timedelta(minutes=10, seconds=2)
    status = (await client.get("/api/telegram/status")).json()
    assert status["pairing"] == {"code": code, "expires_at": body["expires_at"]}
    hashes = await rows(engine, "SELECT code_hash FROM telegram_pairings WHERE used_at IS NULL")
    assert [h[0] for h in hashes] == [pairing.code_hash(code)]
    await assert_absent_from_database(engine, code)
    # A second code cancels the first.
    second = (await client.post("/api/telegram/pairing")).json()["code"]
    open_rows = await rows(engine, "SELECT code_hash FROM telegram_pairings WHERE cancelled_at IS NULL")
    assert [h[0] for h in open_rows] == [pairing.code_hash(second)]
    assert (await client.get("/api/telegram/status")).json()["pairing"]["code"] == second


async def test_expired_pairing_is_not_shown(client: AsyncClient, engine: AsyncEngine, vault: Vault) -> None:
    await configure(engine, vault)
    await client.post("/api/telegram/pairing")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE telegram_pairings SET created_at = now() - interval '11 minutes', "
                "expires_at = now() - interval '1 minute'"
            )
        )
    assert (await client.get("/api/telegram/status")).json()["pairing"] is None


# --- channels ------------------------------------------------------------------------------------


async def test_channel_needs_a_bot_and_a_valid_name(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    r = await client.put("/api/telegram/channel", json={"role": "main", "username": "@ipoxxo"})
    assert r.status_code == 400 and r.json()["error"] == "bot_not_set"
    await configure(engine, vault)
    for name in ("ipo xxo", "@ab", "-12345", "https://t.me/ipoxxo"):
        r = await client.put("/api/telegram/channel", json={"role": "test", "username": name})
        assert r.status_code == 400 and r.json()["error"] == "channel_not_found", name
    r = await client.put("/api/telegram/channel", json={"role": "other", "username": "@ipoxxo"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


async def test_channel_check_when_telegram_is_unreachable(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    await configure(engine, vault)
    r = await client.put("/api/telegram/channel", json={"role": "main", "username": "@ipoxxo"})
    assert r.status_code == 502 and r.json()["error"] == "telegram_unavailable"
    assert (await client.get("/api/telegram/status")).json()["channels"]["main"] is None


async def test_channel_check_with_a_revoked_token(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    use_real_telegram()
    token = await configure(engine, vault)
    with all_logs() as logs:
        r = await client.put("/api/telegram/channel", json={"role": "main", "username": "@ipoxxo"})
    assert r.status_code == 400 and r.json()["error"] == "bot_token_invalid"
    assert_no_token(token, r.text, logs.getvalue())


# --- settings (D21) ------------------------------------------------------------------------------


async def test_settings_default_and_update(client: AsyncClient, engine: AsyncEngine) -> None:
    r = await client.get("/api/telegram/settings")
    assert r.json() == {"quiet_windows": [], "post_interval_min": 60}
    new = {
        "quiet_windows": [{"start": "23:00", "end": "01:00"}, {"start": "13:30", "end": "15:30"}],
        "post_interval_min": 90,
    }
    r = await client.put("/api/telegram/settings", json=new)
    assert r.status_code == 200 and r.json() == new
    assert (await client.get("/api/telegram/settings")).json() == new
    logged = await rows(
        engine, "SELECT actor, reasons FROM actions_log WHERE action = 'telegram.settings_set'"
    )
    assert logged[0][0] == "admin:owner" and logged[0][1]["context"]["new"] == new


@pytest.mark.parametrize(
    "payload",
    [
        {"quiet_windows": [{"start": "25:00", "end": "01:00"}], "post_interval_min": 60},
        {"quiet_windows": [{"start": "10:00", "end": "10:00"}], "post_interval_min": 60},
        {
            "quiet_windows": [{"start": "00:00", "end": "12:00"}, {"start": "12:00", "end": "00:00"}],
            "post_interval_min": 60,
        },
        {"quiet_windows": [], "post_interval_min": -1},
        {"quiet_windows": [], "post_interval_min": 1441},
        {"quiet_windows": [], "post_interval_min": True},
        {"quiet_windows": [], "post_interval_min": "60"},
        {"quiet_windows": [{"start": "10:00"}], "post_interval_min": 60},
        {"quiet_windows": [{"start": 10, "end": 11}], "post_interval_min": 60},
        {"quiet_windows": [], "post_interval_min": 60, "extra": 1},
        {"post_interval_min": 60},
        [],
    ],
)
async def test_invalid_settings(client: AsyncClient, payload: Any) -> None:
    r = await client.put("/api/telegram/settings", json=payload)
    assert r.status_code == 422 and r.json()["error"] == "invalid_settings"
    assert "HH:MM" in r.json()["message_fa"]
    assert (await client.get("/api/telegram/settings")).json()["post_interval_min"] == 60


# --- test alert / test post ----------------------------------------------------------------------


async def test_test_alert_needs_bot_and_owner(client: AsyncClient, engine: AsyncEngine, vault: Vault) -> None:
    r = await client.post("/api/telegram/test-alert")
    assert r.status_code == 409 and r.json()["error"] == "bot_not_set"
    await configure(engine, vault)
    r = await client.post("/api/telegram/test-alert")
    assert r.status_code == 409 and r.json()["error"] == "owner_not_paired"
    await configure(engine, vault, owner=True)
    r = await client.post("/api/telegram/test-alert")
    assert r.status_code == 202 and r.json() == {}
    jobs = await rows(engine, "SELECT kind, payload FROM outbox")
    assert len(jobs) == 1 and jobs[0][0] == "alert" and "آزمایشی" in jobs[0][1]["text"]


async def test_test_post_goes_to_the_test_channel_only(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    r = await client.post("/api/telegram/test-post")
    assert r.status_code == 409 and r.json()["error"] == "bot_not_set"
    await configure(engine, vault)
    r = await client.post("/api/telegram/test-post")
    assert r.status_code == 409 and r.json()["error"] == "test_channel_not_set"
    await configure(engine, vault, test_channel=True)
    r = await client.post("/api/telegram/test-post")
    assert r.status_code == 202
    job_id = r.json()["outbox_id"]
    assert isinstance(job_id, int)
    jobs = await rows(engine, "SELECT id, kind, payload FROM outbox")
    assert [(j[0], j[1], j[2]["role"], j[2]["post_kind"]) for j in jobs] == [
        (job_id, "send_post", "test", "test")
    ]
    assert jobs[0][2]["text"].endswith("🔗 @ipoxxo")
    assert (await client.get("/api/telegram/status")).json()["outbox"] == {"pending": 1, "failed": 0}


async def test_audit_rows_never_hold_the_token(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    token = await configure(engine, vault, owner=True, test_channel=True)
    await client.post("/api/telegram/pairing")
    await client.post("/api/telegram/test-alert")
    await client.post("/api/telegram/test-post")
    await client.put("/api/telegram/settings", json={"quiet_windows": [], "post_interval_min": 30})
    audit = await rows(
        engine, "SELECT action, target, CAST(reasons AS text) FROM actions_log WHERE action LIKE 'telegram.%'"
    )
    assert {a[0] for a in audit} == {
        "telegram.pairing_created",
        "telegram.test_alert",
        "telegram.test_post",
        "telegram.settings_set",
    }
    assert_no_token(token, *(f"{a[1]} {a[2]}" for a in audit))
    await assert_absent_from_database(engine, token)
    stored = await rows(engine, "SELECT octet_length(ciphertext) FROM secrets WHERE name = 'telegram:bot'")
    assert stored and stored[0][0] >= len(token)  # encrypted in the vault, not plaintext


async def test_new_bot_resets_owner_and_channels(engine: AsyncEngine, vault: Vault) -> None:
    await configure(engine, vault, owner=True, test_channel=True)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        # Same bot, rotated token (BotFather /revoke): owner and channels stay.
        same = await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=BOT_ID, username=BOT_USERNAME),
            token=SecretValue(random_token()),
            now=now,
        )
        assert same is False and await store.owner_chat_id(conn) == OWNER_CHAT
        other = await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=BOT_ID + 1, username="other_bot"),
            token=SecretValue(random_token()),
            now=now,
        )
        assert other is True
        assert await store.owner_chat_id(conn) is None and await store.channels(conn) == {}
