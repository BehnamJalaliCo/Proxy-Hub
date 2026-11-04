"""Hetzner accounts: state machine, limit learning, token verification outcome, guard (D9, rules 3/4).

Verification outcomes are fed as DATA (``summarize_servers`` over ``ServerView`` lists, offers
parsed from Hetzner-shaped JSON by the real hcloud objects), the same way the safety gates are
tested. The live verification against a real account is in tests/test_fleet_hetzner_real.py.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hcloud import APIException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from test_fleet_support import (
    assert_absent_from_database,
    clean_fleet_tables,
    live_offers,
    make_vault,
    new_token,
    server,
)

from proxyhub.accounts import service
from proxyhub.accounts.hetzner_io import (
    HetznerFailure,
    Verification,
    classify_api_exception,
    classify_write_probe,
    summarize_servers,
)
from proxyhub.accounts.limits import learned_server_limit, parse_resource_limit, record_resource_limit
from proxyhub.accounts.state import (
    ACCOUNT_TRANSITIONS,
    REASON_FA,
    AccountState,
    IllegalAccountTransition,
    check_account_transition,
    reason_code,
)
from proxyhub.reconcilers.account_guard import AccountRef, apply_failure, apply_observation
from proxyhub.reconcilers.pricing import log_price_changes
from proxyhub.safety.budget import Price
from proxyhub.vault import hcloud_secret_name, secret_exists

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

# --- state machine ----------------------------------------------------------------------------


def test_state_machine_table() -> None:
    expected = {
        ("verifying", "active"),
        ("verifying", "preparing"),
        ("verifying", "locked"),
        ("preparing", "active"),
        ("preparing", "locked"),
        ("active", "draining"),
        ("active", "locked"),
        ("draining", "retired"),
        ("draining", "locked"),
        ("locked", "retired"),
    }
    for cur in AccountState:
        for target in AccountState:
            if (cur.value, target.value) in expected:
                assert check_account_transition(cur.value, target) is target
            else:
                with pytest.raises(IllegalAccountTransition):
                    check_account_transition(cur.value, target)
    assert ACCOUNT_TRANSITIONS[AccountState.RETIRED] == frozenset()
    # Code never unlocks an account: the owner decides (rule 4).
    assert AccountState.ACTIVE not in ACCOUNT_TRANSITIONS[AccountState.LOCKED]


def test_every_reason_code_has_persian_text() -> None:
    for code in ("verified", "foreign_servers", "token_rejected", "token_forbidden"):
        assert REASON_FA[code]
    assert reason_code("foreign_servers:3") == "foreign_servers" and reason_code(None) is None


# --- resource_limit_exceeded (H7) -------------------------------------------------------------

DOCUMENTED = {
    "error": {
        "code": "resource_limit_exceeded",
        "message": "project limit exceeded",
        "details": {"limits": [{"name": "project_limit"}]},
    }
}


@pytest.mark.parametrize(
    ("body", "kind", "stated"),
    [
        (DOCUMENTED, "project", None),
        ({"error": {"code": "resource_limit_exceeded", "message": "server limit exceeded"}}, "server", None),
        (
            {
                "error": {
                    "code": "resource_limit_exceeded",
                    "message": "server limit of 5 servers reached",
                    "details": {"limits": [{"name": "server_limit"}]},
                }
            },
            "server",
            5,
        ),
        ({"error": {"code": "resource_limit_exceeded", "message": "cores limit exceeded"}}, "core", None),
        (
            {
                "error": {
                    "code": "resource_limit_exceeded",
                    "message": "limit exceeded",
                    "details": {"limits": [{"name": "primary_ip_limit"}]},
                }
            },
            "primary_ip",
            None,
        ),
        ({"error": {"code": "resource_limit_exceeded", "message": "quota", "details": None}}, "other", None),
    ],
)
def test_resource_limit_parsing(body: dict, kind: str, stated: int | None) -> None:  # type: ignore[type-arg]
    parsed = parse_resource_limit(body)
    assert parsed is not None and parsed.kind == kind and parsed.stated_server_limit == stated
    # Same result from the exception hcloud raises for that body.
    err = body["error"]
    exc = APIException(code=err["code"], message=err["message"], details=err.get("details"))
    assert parse_resource_limit(exc) == parsed


def test_other_errors_are_not_limits() -> None:
    assert parse_resource_limit({"error": {"code": "rate_limit_exceeded", "message": "slow down"}}) is None
    assert (
        parse_resource_limit(APIException(code="uniqueness_error", message="name used", details={})) is None
    )
    assert parse_resource_limit({"error": "garbage"}) is None


def test_learned_limit_is_current_count_unless_stated() -> None:
    parsed = parse_resource_limit(DOCUMENTED)
    assert parsed is not None
    assert learned_server_limit(parsed, 7) == 7
    stated = parse_resource_limit(
        {"error": {"code": "resource_limit_exceeded", "message": "limit of 10 servers"}}
    )
    assert stated is not None and learned_server_limit(stated, 7) == 10
    with pytest.raises(ValueError):
        learned_server_limit(parsed, -1)


# --- Hetzner error classification (codes from docs.hetzner.cloud "Errors") --------------------


def test_api_error_classification() -> None:
    cases = {
        "unauthorized": "token_invalid",
        "token_readonly": "read_only",
        "forbidden": "forbidden",
        "rate_limit_exceeded": "rate_limited",
        "server_error": "api_error",
    }
    for code, kind in cases.items():
        failure = classify_api_exception(APIException(code=code, message="m", details=None))
        assert isinstance(failure, HetznerFailure) and failure.kind == kind
    assert classify_api_exception(APIException(code=401, message="Unauthorized", details=None)).kind == (
        "token_invalid"
    )


def test_write_probe_classification() -> None:
    assert classify_write_probe("token_readonly") == "read_only"
    assert classify_write_probe("forbidden") == "read_only"
    assert classify_write_probe("unauthorized") == "invalid"
    assert classify_write_probe("not_found") == "read_write"
    assert classify_write_probe("invalid_input") == "read_write"
    assert classify_write_probe("server_error") == "unknown"
    assert classify_write_probe(None) == "unknown"  # the probe must never succeed


def test_guard_counts_foreign_servers_without_keeping_them() -> None:
    obs = summarize_servers([server(1), server(2, managed=False), server(3, role="builder")])
    assert not obs.guard.allowed and obs.foreign_count == 1 and obs.total == 3
    assert {s.hetzner_id for s in obs.managed} == {1, 3}


def test_name_and_token_cleaning() -> None:
    assert service.clean_name("  acc   nov  ") == "acc nov"
    for bad in ("", " ", "x" * 101, "<script>", "a;b"):
        with pytest.raises(service.InvalidName):
            service.clean_name(bad)
    token = new_token()
    assert service.clean_token(f"  {token}\n").reveal() == token
    for bad in ("short", "has space " + token, token + "\r\nX-Evil: 1"):
        with pytest.raises(service.TokenInvalid):
            service.clean_token(bad)


# --- against the real database ----------------------------------------------------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_fleet_tables(eng)
    yield eng
    await clean_fleet_tables(eng)
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


def verification(*servers_: object, access: str = "read_write") -> Verification:
    return Verification(servers=summarize_servers(servers_), access=access, offers=live_offers())  # type: ignore[arg-type]


async def scalar(engine: AsyncEngine, sql: str, **params: object) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one_or_none()


@pytest.mark.db
async def test_clean_project_becomes_active_with_token_only_in_vault(
    engine: AsyncEngine, session: AsyncSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    vault = make_vault(tmp_path)
    token = new_token()
    acc = await service.persist_verified_account(
        session,
        vault,
        name="nodes-2026-11",
        token=service.clean_token(token),
        result=verification(server(11), server(12, role="builder")),
        actor="admin:owner",
        now=NOW,
    )
    assert acc["state"] == "active" and acc["state_reason"] == "verified"
    assert acc["servers_in_account"] == 2 and acc["learned_server_limit"] is None
    assert token not in repr(acc)
    account_id = uuid.UUID(acc["id"])
    async with engine.connect() as conn:
        stored = await vault.get(conn, hcloud_secret_name(account_id))
        assert stored is not None and stored.reveal() == token
    assert (
        await scalar(engine, "SELECT count(*) FROM hetzner_offers WHERE account_id = :a", a=account_id) == 12
    )
    assert (
        await scalar(
            engine, "SELECT count(*) FROM hetzner_account_servers WHERE account_id = :a", a=account_id
        )
        == 2
    )
    monthly = await scalar(
        engine,
        "SELECT price_monthly_net FROM hetzner_offers "
        "WHERE account_id = :a AND server_type = 'cpx02' AND location = 'fsn1'",
        a=account_id,
    )
    assert monthly == Decimal("6.4900000000")
    events = await scalar(
        engine,
        "SELECT count(*) FROM actions_log WHERE target IN (:n, :i)",
        n="nodes-2026-11",
        i=str(account_id),
    )
    assert events == 2  # account_active transition + account_add
    await assert_absent_from_database(engine, token)
    assert token not in caplog.text


@pytest.mark.db
async def test_foreign_server_locks_account_and_token_is_never_stored(
    engine: AsyncEngine, session: AsyncSession, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    token = new_token()
    with pytest.raises(service.AccountNotEmpty) as exc:
        await service.persist_verified_account(
            session,
            vault,
            name="main-by-mistake",
            token=service.clean_token(token),
            result=verification(
                server(1), server(2, managed=False), server(3, managed=False), access="unknown"
            ),
            actor="admin:owner",
            now=NOW,
        )
    assert exc.value.extra == {"foreign_servers": 2}
    row = (
        await (await session.connection()).execute(
            text("SELECT id, state, state_reason FROM hetzner_accounts WHERE name = 'main-by-mistake'")
        )
    ).one()
    assert row.state == "locked" and row.state_reason == "foreign_servers:2"
    async with engine.connect() as conn:
        assert not await secret_exists(conn, hcloud_secret_name(row.id))
    # Foreign servers are only counted, never stored.
    assert (
        await scalar(engine, "SELECT count(*) FROM hetzner_account_servers WHERE hetzner_id IN (2, 3)") == 0
    )
    await assert_absent_from_database(engine, token)


@pytest.mark.db
async def test_read_only_or_invalid_token_persists_nothing(
    engine: AsyncEngine, session: AsyncSession, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    for access, error in (
        ("read_only", service.TokenReadOnly),
        ("invalid", service.TokenInvalid),
        ("unknown", service.HetznerUnavailable),
    ):
        with pytest.raises(error):
            await service.persist_verified_account(
                session,
                vault,
                name=f"acc-{access}",
                token=service.clean_token(new_token()),
                result=verification(access=access),
                actor="admin:owner",
                now=NOW,
            )
        await session.rollback()
    assert await scalar(engine, "SELECT count(*) FROM hetzner_accounts") == 0
    assert await scalar(engine, "SELECT count(*) FROM secrets WHERE name LIKE 'hcloud:%'") == 0


@pytest.mark.db
async def test_name_taken_case_insensitive(
    engine: AsyncEngine, session: AsyncSession, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    await service.persist_verified_account(
        session,
        vault,
        name="Acc-One",
        token=service.clean_token(new_token()),
        result=verification(),
        actor="admin:owner",
        now=NOW,
    )
    with pytest.raises(service.NameTaken):
        await service.persist_verified_account(
            session,
            vault,
            name="acc-one",
            token=service.clean_token(new_token()),
            result=verification(),
            actor="admin:owner",
            now=NOW,
        )


@pytest.mark.db
async def test_drain_retire_and_node_check(
    engine: AsyncEngine, session: AsyncSession, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    acc = await service.persist_verified_account(
        session,
        vault,
        name="to-retire",
        token=service.clean_token(new_token()),
        result=verification(),
        actor="admin:owner",
        now=NOW,
    )
    account_id = uuid.UUID(acc["id"])
    with pytest.raises(service.IllegalTransition):
        await service.retire_account(session, account_id, actor="admin:owner")  # active: drain first
    await session.rollback()
    await service.drain_account(session, account_id, actor="admin:owner")
    await session.commit()
    assert (await service.get_account(session, account_id))["state"] == "draining"

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO nodes (id, account_id, name, location, server_type, state, is_test) "
                "VALUES (gen_random_uuid(), :a, 'n1', 'fsn1', 'cpx02', 'ready', false)"
            ),
            {"a": account_id},
        )
    with pytest.raises(service.AccountHasNodes) as exc:
        await service.retire_account(session, account_id, actor="admin:owner")
    assert exc.value.extra == {"live_nodes": 1}
    await session.rollback()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE nodes SET state = 'deleted' WHERE account_id = :a"), {"a": account_id}
        )
    await service.retire_account(session, account_id, actor="admin:owner")
    await session.commit()
    assert (await service.get_account(session, account_id))["state"] == "retired"
    async with engine.connect() as conn:
        assert not await secret_exists(conn, hcloud_secret_name(account_id))
    with pytest.raises(service.AccountNotFound):
        await service.drain_account(session, uuid.uuid4(), actor="admin:owner")


@pytest.mark.db
async def test_record_resource_limit_updates_account(
    engine: AsyncEngine, session: AsyncSession, tmp_path: Path
) -> None:
    acc = await service.persist_verified_account(
        session,
        make_vault(tmp_path),
        name="limit",
        token=service.clean_token(new_token()),
        result=verification(),
        actor="admin:owner",
        now=NOW,
    )
    account_id = uuid.UUID(acc["id"])
    async with engine.begin() as conn:
        learned = await record_resource_limit(conn, account_id, 5, DOCUMENTED)
        assert learned == 5
        not_limit = APIException(code="server_error", message="x", details=None)
        assert await record_resource_limit(conn, account_id, 6, not_limit) is None
    assert (
        await scalar(engine, "SELECT learned_server_limit FROM hetzner_accounts WHERE id = :a", a=account_id)
        == 5
    )
    assert await scalar(engine, "SELECT count(*) FROM actions_log WHERE action = 'learned_server_limit'") == 1


@pytest.mark.db
async def test_guard_locks_on_foreign_server_and_rejected_token(
    engine: AsyncEngine, session: AsyncSession, tmp_path: Path
) -> None:
    vault = make_vault(tmp_path)
    ids = []
    for name in ("guard-a", "guard-b", "guard-c"):
        acc = await service.persist_verified_account(
            session,
            vault,
            name=name,
            token=service.clean_token(new_token()),
            result=verification(),
            actor="admin:owner",
            now=NOW,
        )
        ids.append(uuid.UUID(acc["id"]))
    a, b, c = (
        AccountRef(i, n, "active", NOW) for i, n in zip(ids, ("guard-a", "guard-b", "guard-c"), strict=True)
    )
    later = NOW + timedelta(minutes=10)
    async with engine.begin() as conn:
        assert await apply_observation(conn, a, summarize_servers([server(1)]), later)
        assert not await apply_observation(
            conn, b, summarize_servers([server(1), server(9, managed=False)]), later
        )
        await apply_failure(conn, c, HetznerFailure("token_invalid", "unauthorized"), later)
    states = {
        r.name: (r.state, r.state_reason)
        for r in (
            await (await session.connection()).execute(
                text("SELECT name, state, state_reason FROM hetzner_accounts")
            )
        ).all()
    }
    assert states == {
        "guard-a": ("active", "verified"),
        "guard-b": ("locked", "foreign_servers:1"),
        "guard-c": ("locked", "token_rejected"),
    }
    # A transient failure only marks the observation; it never changes the account state.
    async with engine.begin() as conn:
        await apply_failure(conn, a, HetznerFailure("unreachable", "ConnectionError"), later)
    assert await scalar(engine, "SELECT state FROM hetzner_accounts WHERE id = :a", a=ids[0]) == "active"
    obs = await scalar(engine, "SELECT ok FROM hetzner_account_observations WHERE account_id = :a", a=ids[0])
    assert obs is False
    last_ok = await scalar(
        engine, "SELECT last_ok_at FROM hetzner_account_observations WHERE account_id = :a", a=ids[0]
    )
    assert last_ok == later  # the last good look is kept


@pytest.mark.db
async def test_price_changes_are_detected_and_logged(
    engine: AsyncEngine, session: AsyncSession, tmp_path: Path
) -> None:
    acc = await service.persist_verified_account(
        session,
        make_vault(tmp_path),
        name="prices",
        token=service.clean_token(new_token()),
        result=verification(),
        actor="admin:owner",
        now=NOW,
    )
    account_id = uuid.UUID(acc["id"])
    offers = live_offers()
    raised = [
        o
        if (o.server_type, o.location) != ("cpx02", "fsn1")
        else type(o)(
            o.server_type,
            o.location,
            Price(Decimal("0.0112"), Decimal("6.99")),
            o.included_traffic_bytes,
            o.deprecated,
            o.available,
        )
        for o in offers
        if (o.server_type, o.location) != ("cpx12", "hel1")
    ]
    async with engine.begin() as conn:
        changes = await service.store_offers(conn, account_id, raised, NOW + timedelta(hours=1))
        assert {(c.server_type, c.location) for c in changes} == {("cpx02", "fsn1"), ("cpx12", "hel1")}
        assert await log_price_changes(conn, account_id, "prices", changes) == 2
    reasons = await scalar(
        engine,
        "SELECT string_agg(reasons->'reasons'->>0, ',' ORDER BY target) "
        "FROM actions_log WHERE action = 'pricing_change'",
    )
    assert reasons == "price_changed,offer_gone"
