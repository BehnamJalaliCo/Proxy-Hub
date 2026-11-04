"""REAL Telegram tests of the operator account (marker ``telegram_user``): MTProto to Telegram's own
data centres, run on the panel server (this sandbox's outbound traffic cannot carry raw MTProto).

Environment (skipped without it, tests/conftest.py):
  PROXYHUB_TELEGRAM_USER_TEST_FILE  a 0600 file outside the repo, never its content in a variable.
      ``{}`` runs only the negative test (random api_id/api_hash: Telegram must answer API_ID_INVALID).
      ``{"api_id": 1234567, "api_hash": "<32 hex>", "session": "<StringSession>"}`` also runs the
      tests that use a real operator-account session (D37: the SECOND account, never the personal
      one): get_me, resolving @MTProxybot, and the worker's check. None of them logs out or sends
      anything to anyone.

Every test runs with the Telethon loggers at DEBUG and asserts that no secret reached a log line.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF, make_vault
from test_mtproxy_support import PHONE, actions, assert_nowhere, clean, load_row, secret_names
from test_telegram_support import all_logs

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_mtproxy_account import get_vault
from proxyhub.config import read_secret_file
from proxyhub.fleet.settings_store import save_fleet_settings
from proxyhub.mtproxy import service, state, store
from proxyhub.mtproxy.state import AccountRow
from proxyhub.mtproxy.userclient import ApiCredentials, operator_session
from proxyhub.settings_schema import FleetSettings
from proxyhub.vault import SecretValue, Vault

pytestmark = pytest.mark.telegram_user


def _file() -> dict[str, Any]:
    path = Path(os.environ.get("PROXYHUB_TELEGRAM_USER_TEST_FILE", ""))
    if path.stat().st_mode & 0o077:
        pytest.fail(f"{path} must be readable by its owner only (chmod 600)")
    data = json.loads(read_secret_file(path))
    assert isinstance(data, dict)
    return data


def _account() -> tuple[ApiCredentials, SecretValue]:
    data = _file()
    if not {"api_id", "api_hash", "session"} <= set(data):
        pytest.skip("the test file holds no operator-account session")
    return (
        ApiCredentials(api_id=int(data["api_id"]), api_hash=SecretValue(str(data["api_hash"]))),
        SecretValue(str(data["session"])),
    )


@pytest.fixture
def telethon_debug() -> Iterator[None]:
    logger = logging.getLogger("telethon")
    old = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        logger.setLevel(old)


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


@pytest.mark.db
@pytest.mark.redis
async def test_a_random_api_id_is_refused_by_telegram_and_nothing_leaks(
    engine: AsyncEngine, vault: Vault, telethon_debug: None
) -> None:
    from proxyhub.api.app import app, lifespan

    _file()
    body = {
        "api_id": str(100_000 + secrets.randbelow(900_000)),
        "api_hash": secrets.token_hex(16),
        "phone": PHONE,
    }
    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    try:
        async with lifespan(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
                with all_logs(logging.DEBUG) as logs:
                    r = await c.put("/api/mtproxy/account", json=body)  # real auth.sendCode
                assert r.status_code == 400, r.text
                assert (
                    r.json()["error"] == "api_id_invalid" and r.json()["telegram_error"] == "API_ID_INVALID"
                )
                assert "telethon" in logs.getvalue()  # Telethon really talked to Telegram
                await assert_nowhere(
                    engine, app.state.redis, [body["api_hash"], PHONE, PHONE[1:]], r.text, logs.getvalue()
                )
    finally:
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(get_vault, None)
    assert await secret_names(engine) == []
    assert [a["reasons"]["reasons"] for a in await actions(engine)] == [["api_id_invalid"]]


async def test_get_me_and_mtproxybot_with_the_real_session(telethon_debug: None) -> None:
    creds, session = _account()
    with all_logs(logging.DEBUG) as logs:
        async with operator_session(creds, session) as op:
            me = await op.get_me()
            bot_id = await op.resolve_mtproxybot()
    assert me.user_id > 0 and not me.is_bot
    assert bot_id > 0
    text = logs.getvalue()
    for needle in (creds.api_hash.reveal(), session.reveal()):
        assert needle not in text and needle.encode().hex() not in text


@pytest.mark.db
async def test_the_worker_check_with_the_real_session(
    engine: AsyncEngine, vault: Vault, telethon_debug: None
) -> None:
    creds, session = _account()
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    row = state.connected(
        AccountRow(phone_masked="+00•••••00"),
        session_id="real",
        user_id=1,
        first_name="",
        mtproxybot_id=None,
        check_error=None,
        now=long_ago,
    )
    async with engine.begin() as conn:
        await store.save_account_secrets(conn, vault, creds, session)
        await store.save_row(conn, row)
        await save_fleet_settings(conn, FleetSettings(kill_switch=False))
    with all_logs(logging.DEBUG) as logs:
        outcome = await service.check_session(service.Deps(engine=engine, vault=vault, redis=None))
    assert outcome == "ok"
    checked = await load_row(engine)
    assert checked.state == "connected" and checked.user_id == 1 and checked.mtproxybot_id
    assert checked.first_name  # get_me's first name replaced the empty one
    await assert_nowhere(engine, None, [creds.api_hash.reveal(), session.reveal()], logs.getvalue())
