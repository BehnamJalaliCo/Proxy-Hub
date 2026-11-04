"""Control loops on the real database: fleet (dry-run), account_guard and pricing scheduling."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_fleet_support import clean_fleet_tables, live_offers, make_vault, new_token, server

from proxyhub.accounts import service
from proxyhub.accounts.hetzner_io import Verification, summarize_servers
from proxyhub.fleet.settings_store import save_fleet_settings
from proxyhub.reconcilers.account_guard import AccountGuardReconciler
from proxyhub.reconcilers.fleet import PLAN_RETENTION, FleetReconciler
from proxyhub.reconcilers.pricing import PricingReconciler
from proxyhub.settings_schema import FleetSettings
from proxyhub.vault import Vault

pytestmark = pytest.mark.db


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db, pool_size=4)
    await clean_fleet_tables(eng)
    yield eng
    await clean_fleet_tables(eng)
    await eng.dispose()


async def q(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one_or_none()


async def set_settings(engine: AsyncEngine, s: FleetSettings) -> None:
    async with engine.begin() as conn:
        await save_fleet_settings(conn, s)


async def add_account(engine: AsyncEngine, vault: Vault, name: str) -> uuid.UUID:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        acc = await service.persist_verified_account(
            session,
            vault,
            name=name,
            token=service.clean_token(new_token()),
            result=Verification(servers=summarize_servers([]), access="read_write", offers=live_offers()),
            actor="admin:owner",
            now=datetime.now(UTC),
        )
    return uuid.UUID(acc["id"])


OPEN = FleetSettings(
    kill_switch=False, budget_cap_eur=Decimal("20"), node_cap_total=10, creations_per_hour_cap=3
)


async def test_fresh_panel_plans_nine_blocked_creations_and_logs_them_once(engine: AsyncEngine) -> None:
    fleet = FleetReconciler()
    assert await fleet.try_run_once(engine)
    plan = await q(engine, "SELECT actions FROM fleet_plans ORDER BY id DESC LIMIT 1")
    assert isinstance(plan, list) and len(plan) == 9
    assert all(a["action"] == "create" and not a["allowed"] for a in plan)
    assert all("kill_switch_on" in a["reasons"] and "budget_cap_not_set" in a["reasons"] for a in plan)
    logged = await q(engine, "SELECT count(*) FROM actions_log WHERE actor = 'fleet'")
    assert logged == 9
    assert await q(engine, "SELECT bool_and(dry_run) FROM actions_log WHERE actor = 'fleet'") is True
    assert await q(engine, "SELECT bool_or(allowed) FROM actions_log WHERE actor = 'fleet'") is False

    # Unchanged plan: stored again, but not logged again.
    assert await fleet.try_run_once(engine)
    assert await q(engine, "SELECT count(*) FROM fleet_plans") == 2
    assert await q(engine, "SELECT count(*) FROM actions_log WHERE actor = 'fleet'") == 9

    # A changed plan is logged.
    await set_settings(engine, FleetSettings(target_slots=3))
    assert await fleet.try_run_once(engine)
    assert await q(engine, "SELECT count(*) FROM actions_log WHERE actor = 'fleet'") == 12
    assert await q(engine, "SELECT count(*) FROM nodes") == 0  # dry-run: nothing is ever created


async def test_open_caps_allow_creations_only_within_budget_and_rate(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    await add_account(engine, vault, "nodes-a")
    await set_settings(engine, OPEN)
    assert await FleetReconciler().try_run_once(engine)
    rows = await q(
        engine,
        "SELECT json_agg(json_build_array(target, allowed, reasons->'reasons') ORDER BY id) "
        "FROM actions_log WHERE actor = 'fleet'",
    )
    assert isinstance(rows, list) and len(rows) == 9
    assert [r[1] for r in rows] == [True, True, True] + [False] * 6  # 3 x 6.49 <= 20 EUR, rate cap 3
    assert all({"budget_cap_reached", "creation_rate_cap_reached"} & set(r[2]) for r in rows[3:])
    summary = await q(engine, "SELECT summary FROM fleet_plans ORDER BY id DESC LIMIT 1")
    assert isinstance(summary, dict) and summary["creates_allowed"] == 3
    assert Decimal(summary["projected_after_eur"]) <= Decimal("20")
    assert await q(engine, "SELECT bool_and(dry_run) FROM actions_log WHERE actor = 'fleet'") is True
    assert await q(engine, "SELECT count(*) FROM nodes") == 0


async def test_stale_stored_prices_block_creations(engine: AsyncEngine, tmp_path: Path) -> None:
    """The snapshot carries hetzner_offers.fetched_at into the planner (fail closed on old prices)."""
    vault = make_vault(tmp_path)
    await add_account(engine, vault, "nodes-old-prices")
    await set_settings(engine, OPEN)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE hetzner_offers SET fetched_at = now() - interval '7 hours'"))
    assert await FleetReconciler().try_run_once(engine)
    plan = await q(engine, "SELECT actions FROM fleet_plans ORDER BY id DESC LIMIT 1")
    assert isinstance(plan, list) and len(plan) == 9
    assert all(not a["allowed"] and "prices_stale" in a["reasons"] for a in plan)


async def test_dry_run_false_with_the_kill_switch_on_still_plans_dry(engine: AsyncEngine, tmp_path: Path) -> None:
    """dry_run=false alone is not enough: with the kill switch on the loop stays dry (the live
    fleet is tested in test_fleet_live_loop.py)."""
    await add_account(engine, make_vault(tmp_path), "nodes-b")
    await set_settings(engine, OPEN.model_copy(update={"dry_run": False, "kill_switch": True}))
    assert await FleetReconciler().try_run_once(engine)
    assert await q(engine, "SELECT dry_run FROM fleet_plans ORDER BY id DESC LIMIT 1") is True
    assert await q(engine, "SELECT bool_and(dry_run) FROM actions_log WHERE actor = 'fleet'") is True
    assert await q(engine, "SELECT count(*) FROM nodes") == 0


async def test_corrupt_settings_block_everything(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('fleet', CAST(:v AS jsonb))"),
            {"v": json.dumps({"kill_switch": False, "budget_cap_eur": "-1"})},
        )
    assert await FleetReconciler().try_run_once(engine)
    plan = await q(engine, "SELECT actions FROM fleet_plans ORDER BY id DESC LIMIT 1")
    assert isinstance(plan, list) and plan
    assert all(not a["allowed"] and "settings_invalid" in a["reasons"] for a in plan)


async def test_unchanged_plan_relogged_hourly_and_old_plans_pruned(engine: AsyncEngine) -> None:
    fleet = FleetReconciler()
    assert await fleet.try_run_once(engine)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE fleet_plans SET generated_at = generated_at - interval '2 hours'"))
        await conn.execute(
            text(
                "INSERT INTO fleet_plans (generated_at, dry_run, fingerprint, logged, actions, summary) "
                "VALUES (:at, true, 'x', false, '[]', '{}')"
            ),
            {"at": datetime.now(UTC) - PLAN_RETENTION - timedelta(hours=1)},
        )
    assert await fleet.try_run_once(engine)
    assert await q(engine, "SELECT count(*) FROM actions_log WHERE actor = 'fleet'") == 18
    assert await q(engine, "SELECT count(*) FROM fleet_plans WHERE fingerprint = 'x'") == 0


async def test_orphan_server_reported_in_events(engine: AsyncEngine, tmp_path: Path) -> None:
    account_id = await add_account(engine, make_vault(tmp_path), "nodes-c")
    async with engine.begin() as conn:
        await service.store_observation(
            conn, account_id, summarize_servers([server(4242)]), datetime.now(UTC)
        )
    assert await FleetReconciler().try_run_once(engine)
    row = await q(
        engine,
        "SELECT json_build_array(action, allowed) FROM actions_log "
        "WHERE actor = 'fleet' AND target = 'server:4242'",
    )
    assert row == ["orphan_server_not_in_db", False]


async def test_account_guard_without_token_records_failure_and_schedules(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    async with engine.begin() as conn:
        for name, state in (("no-token", "active"), ("locked-one", "locked")):
            await conn.execute(
                text(
                    "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                    "VALUES (gen_random_uuid(), :n, 'hcloud:x', :s)"
                ),
                {"n": name, "s": state},
            )
    guard = AccountGuardReconciler(vault_factory=lambda: vault)
    assert await guard.try_run_once(engine)
    rows = await q(
        engine,
        "SELECT json_agg(json_build_array(a.name, o.ok, o.error)) FROM hetzner_accounts a "
        "JOIN hetzner_account_observations o ON o.account_id = a.id",
    )
    assert rows == [["no-token", False, "token_missing"]]  # locked accounts are not even looked at
    first = await q(engine, "SELECT checked_at FROM hetzner_account_observations")
    assert await guard.try_run_once(engine)  # not due again within 10 minutes
    assert await q(engine, "SELECT checked_at FROM hetzner_account_observations") == first
    assert await q(engine, "SELECT state FROM hetzner_accounts WHERE name = 'no-token'") == "active"


async def test_pricing_skips_accounts_without_token(engine: AsyncEngine, tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (gen_random_uuid(), 'no-token-p', 'hcloud:x', 'active')"
            )
        )
    pricing = PricingReconciler(vault_factory=lambda: vault)
    assert await pricing.try_run_once(engine)
    assert await q(engine, "SELECT count(*) FROM hetzner_offers") == 0
    assert await q(engine, "SELECT count(*) FROM actions_log WHERE actor = 'pricing'") == 0


async def _damage_first_token(engine: AsyncEngine, name: str) -> None:
    """Flip a ciphertext byte of one account's stored token: decrypting it now fails (GCM tag)."""
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE secrets SET ciphertext = set_byte(ciphertext, 0, get_byte(ciphertext, 0) # 1) "
                "WHERE name = (SELECT token_secret_name FROM hetzner_accounts WHERE name = :n)"
            ),
            {"n": name},
        )


async def test_account_guard_one_broken_account_does_not_undo_the_others(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    await add_account(engine, vault, "damaged")
    await _damage_first_token(engine, "damaged")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (gen_random_uuid(), 'no-token-2', 'hcloud:y', 'active')"
            )
        )
        await conn.execute(text("DELETE FROM hetzner_account_observations"))
    guard = AccountGuardReconciler(vault_factory=lambda: vault)
    assert await guard.try_run_once(engine)  # no exception escapes the loop
    rows = await q(
        engine,
        "SELECT json_object_agg(a.name, json_build_array(o.ok, o.error)) FROM hetzner_accounts a "
        "JOIN hetzner_account_observations o ON o.account_id = a.id",
    )
    assert rows == {"damaged": [False, "internal:VaultDecryptError"], "no-token-2": [False, "token_missing"]}


async def test_pricing_one_broken_account_does_not_stop_the_loop(engine: AsyncEngine, tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    await add_account(engine, vault, "damaged-p")
    await _damage_first_token(engine, "damaged-p")
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM hetzner_offers"))
    pricing = PricingReconciler(vault_factory=lambda: vault)
    assert await pricing.try_run_once(engine)
    assert await q(engine, "SELECT count(*) FROM hetzner_offers") == 0


def test_worker_runs_every_fleet_loop() -> None:
    from proxyhub.worker import reconcilers

    loops = {r.name: r for r in reconcilers()}
    # Other teams register their own loops in worker.py (round 2: "nodes"); the fleet's must all run.
    assert {"heartbeat", "account_guard", "pricing", "fleet"} <= set(loops)
    assert len(loops) == len(reconcilers())  # no duplicate names (each loop has its own advisory lock)
    assert loops["fleet"].interval_seconds == 60
