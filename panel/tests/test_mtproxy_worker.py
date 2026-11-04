"""The operator account's session check (worker) on a real PostgreSQL. Telegram is never imitated
(D10): a stored session points at a local port nobody listens on (a real refusal) or at a server
that never answers. What Telegram itself says about an ended session (AUTH_KEY_UNREGISTERED, ...)
is fed to our own recording step as the contract code the wrapper produces for it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import make_vault
from test_mtproxy_support import (
    MASKED,
    USER_ID,
    actions,
    clean,
    load_row,
    open_transactions_during_hang,
    secret_names,
    seed_connected,
)
from test_telegram_support import SilentServer, free_closed_port

from proxyhub.fleet.settings_store import save_fleet_settings
from proxyhub.mtproxy import service, state, store
from proxyhub.mtproxy.userclient import ClientOptions, Endpoint
from proxyhub.reconcilers.base import lock_key
from proxyhub.reconcilers.mtproxy_account import MtproxyAccountReconciler
from proxyhub.settings_schema import FleetSettings
from proxyhub.telegram import store as tg_store
from proxyhub.vault import Vault, VaultKeyError

pytestmark = pytest.mark.db

LONG_AGO = datetime.now(UTC) - timedelta(hours=2)


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


def deps(engine: AsyncEngine, vault: Vault, port: int | None = None, deadline_s: float = 5) -> service.Deps:
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", port or free_closed_port()), deadline_s=deadline_s)
    return service.Deps(engine=engine, vault=vault, redis=None, options=options)


async def kill_switch(engine: AsyncEngine, on: bool) -> None:
    async with engine.begin() as conn:
        await save_fleet_settings(conn, FleetSettings(kill_switch=on))


async def outbox_alerts(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT payload->>'text' FROM outbox WHERE kind = 'alert' ORDER BY id")
        )
        return [str(r[0]) for r in rows]


# --- the kill switch (docs/11 lock 6) -------------------------------------------------------------------


async def test_kill_switch_on_by_default_means_no_check(engine: AsyncEngine, vault: Vault) -> None:
    _, _, before = await seed_connected(engine, vault, port=free_closed_port(), now=LONG_AGO)
    assert await service.check_session(deps(engine, vault)) == "kill_switch"  # no fleet row: ON
    await kill_switch(engine, True)
    assert await service.check_session(deps(engine, vault)) == "kill_switch"
    assert await load_row(engine) == before  # not even a check time is written


async def test_an_unreadable_kill_switch_counts_as_on(engine: AsyncEngine, vault: Vault) -> None:
    await seed_connected(engine, vault, port=free_closed_port(), now=LONG_AGO)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('fleet', '{\"kill_switch\": \"maybe\"}'::jsonb)")
        )
    assert await service.check_session(deps(engine, vault)) == "kill_switch"


# --- checks with the switch OFF --------------------------------------------------------------------------


async def test_nothing_to_check(engine: AsyncEngine, vault: Vault) -> None:
    await kill_switch(engine, False)
    assert await service.check_session(deps(engine, vault)) == "idle"
    await seed_connected(engine, vault, port=free_closed_port())  # checked at login, just now
    assert await service.check_session(deps(engine, vault)) == "not_due"


async def test_telegram_unreachable_is_recorded_and_passes(engine: AsyncEngine, vault: Vault) -> None:
    await kill_switch(engine, False)
    await seed_connected(engine, vault, port=free_closed_port(), now=LONG_AGO)
    assert await service.check_session(deps(engine, vault)) == "failed:telegram_unavailable"
    row = await load_row(engine)
    assert row.state == "connected" and row.last_check_error == "telegram_unavailable"
    assert row.last_check is not None and row.last_check > LONG_AGO
    assert await service.check_session(deps(engine, vault)) == "not_due"  # 30 minutes until the next
    assert await actions(engine) == [] and await outbox_alerts(engine) == []  # passes by itself


async def test_a_session_telegram_ended_disconnects_once_and_alerts_once(
    engine: AsyncEngine, vault: Vault
) -> None:
    await kill_switch(engine, False)
    async with engine.begin() as conn:
        await tg_store.set_owner_chat(conn, 111222333, datetime.now(UTC))
    _, _, checked = await seed_connected(engine, vault, port=1, now=LONG_AGO)
    d = deps(engine, vault)
    # What the wrapper returns when Telegram answers AUTH_KEY_UNREGISTERED (test_mtproxy_units).
    outcome = await service._record_check(
        d, checked, code="session_terminated", rpc="AUTH_KEY_UNREGISTERED", account=None, bot_id=None
    )
    assert outcome == "terminated"
    row = await load_row(engine)
    assert (row.state, row.session_id, row.disconnect_reason) == (
        "disconnected",
        None,
        "auth_key_unregistered",
    )
    assert await secret_names(engine) == [store.API_SECRET]  # the dead session is gone
    rows = await actions(engine)
    assert [(r["actor"], r["action"], r["target"], r["reasons"]["reasons"]) for r in rows] == [
        ("mtproxy", "mtproxy.account_disconnected", MASKED, ["auth_key_unregistered"])
    ]
    alerts = await outbox_alerts(engine)
    assert len(alerts) == 1 and "AUTH_KEY_UNREGISTERED" in alerts[0] and "دستگاه‌ها" in alerts[0]
    # The same news again (or a check that started before) changes nothing and alerts nobody.
    again = await service._record_check(
        d, checked, code="session_terminated", rpc="AUTH_KEY_UNREGISTERED", account=None, bot_id=None
    )
    assert again == "stale"
    assert len(await outbox_alerts(engine)) == 1 and len(await actions(engine)) == 1
    assert await service.check_session(d) == "idle"  # nothing left to check


async def test_a_lasting_failure_moves_to_error_and_back(engine: AsyncEngine, vault: Vault) -> None:
    await kill_switch(engine, False)
    _, _, checked = await seed_connected(engine, vault, port=1, now=LONG_AGO)
    d = deps(engine, vault)
    assert await service._record_check(
        d, checked, code="api_id_invalid", rpc="API_ID_INVALID", account=None, bot_id=None
    ) == ("failed:api_id_invalid")
    row = await load_row(engine)
    assert row.state == "error" and row.error == "api_id_invalid"
    assert [r["action"] for r in await actions(engine)] == ["mtproxy.account_check_failed"]
    assert await service.check_session(d) == "idle"  # an error waits for the owner


async def test_a_session_that_cannot_be_decrypted_is_an_error(
    engine: AsyncEngine, vault: Vault, tmp_path: Path
) -> None:
    await kill_switch(engine, False)
    (tmp_path / "other").mkdir()
    await seed_connected(engine, make_vault(tmp_path / "other"), port=1, now=LONG_AGO)
    assert await service.check_session(deps(engine, vault)) == "failed:session_unreadable"
    assert (await load_row(engine)).state == "error"


# --- the loop itself ---------------------------------------------------------------------------------------


async def test_the_loop_waits_for_the_api_lock(engine: AsyncEngine, vault: Vault) -> None:
    loop = MtproxyAccountReconciler(vault_factory=lambda: vault)
    assert loop.name == store.LOCK_NAME
    async with engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key(store.LOCK_NAME)})
        await holder.commit()
        try:
            assert await loop.try_run_once(engine) is False
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(store.LOCK_NAME)})
            await holder.commit()
    assert await loop.try_run_once(engine) is True and loop.last_outcome == "kill_switch"


async def test_the_loop_without_a_master_key_does_nothing(engine: AsyncEngine) -> None:
    def no_key() -> Vault:
        raise VaultKeyError("missing")

    loop = MtproxyAccountReconciler(vault_factory=no_key)
    assert await loop.try_run_once(engine) is True and loop.last_outcome == "vault_unavailable"


async def test_no_transaction_stays_open_during_a_hung_check(engine: AsyncEngine, vault: Vault) -> None:
    await kill_switch(engine, False)
    async with SilentServer() as silent:
        await seed_connected(engine, vault, port=silent.port, now=LONG_AGO)
        loop = MtproxyAccountReconciler(
            vault_factory=lambda: vault,
            options=ClientOptions(endpoint=Endpoint("127.0.0.1", silent.port), deadline_s=2),
        )
        task = asyncio.create_task(loop.try_run_once(engine))
        seen = await open_transactions_during_hang(engine, task, silent, deadline_s=2)
        assert await task is True
    assert seen and max(seen) == 0
    assert loop.last_outcome == "failed:telegram_unavailable"
    row = await load_row(engine)
    assert row.state == "connected" and row.user_id == USER_ID


def test_the_worker_runs_the_check() -> None:
    from proxyhub.worker import reconcilers

    assert sum(isinstance(r, MtproxyAccountReconciler) for r in reconcilers()) == 1
    assert state.CHECK_EVERY == timedelta(minutes=30)
