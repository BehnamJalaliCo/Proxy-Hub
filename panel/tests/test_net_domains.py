"""The automatic domain pool (decision D35): filter checks from Iran, rotation, the owner's alerts,
and the deletion of filtered zones from Cloudflare, on real PostgreSQL.

Nothing is imitated (decision D10). Globalping answers are the real measurements recorded in
tests/test_net_support.py (youtube.com hijacked from Iran, digikala.com clean, a name that does not
exist), fed to the real parser. Cloudflare and Globalping clients are the real clients pointed at a
closed local port (``http://127.0.0.1:9``): every call they make fails as "unavailable", which
proves both that a failure is handled and, with a counter, that no call was even attempted.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_fleet_support import ADMIN, CSRF, make_vault
from test_net_support import (
    CF_DELETE_ZONE_INVALID_TOKEN,
    GP_DOMAIN_CLEAN,
    GP_DOMAIN_FILTERED,
    GP_DOMAIN_NXDOMAIN,
    ZONE_ID,
    ZONE_ID_2,
    actions,
    cf_zone_json,
    clean_net_tables,
    deep,
)

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_cloudflare import cloudflare_vault
from proxyhub.cloudflare import TOKEN_SECRET, deletion, rotation, service
from proxyhub.cloudflare.client import CloudflareClient, CloudflareError, _codes, parse_zone
from proxyhub.cloudflare.deletion import (
    CLAIM_TTL,
    DeletionContext,
    classify_delete_error,
    deletion_blockers,
    retry_delay,
)
from proxyhub.cloudflare.filtering import (
    ACTIVE_EVERY,
    DOMAIN_PROBES,
    MAX_AGE,
    MIN_GAP,
    STANDBY_EVERY,
    DomainRound,
    budget_allows,
    check_due,
    check_interval,
    classify,
    domain_dns_body,
    domain_verdict,
    parse_domain_round,
    suspicious,
    tighter_rate,
)
from proxyhub.cloudflare.pool import PoolState
from proxyhub.cloudflare.rotation import DELETE_GRACE, NO_STANDBY_FA, PoolZone, filtered_alert_fa
from proxyhub.fleet.settings_store import save_fleet_settings
from proxyhub.probes.globalping import GlobalpingClient, RateLimit
from proxyhub.probes.service import node_probe_reserve
from proxyhub.probes.settings import ProbeSettings
from proxyhub.reconcilers.domains import DomainsReconciler, due_checks
from proxyhub.settings_schema import FleetSettings
from proxyhub.telegram import store as tg_store
from proxyhub.vault import SecretValue, Vault

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
CLOSED = "http://127.0.0.1:9"
ZONE_ID_3 = "3c1b5ad0d1e94b8a9f0c7d6e5f4a3b2c"
ZONE_ID_4 = "4d2c6be1e2fa5c9bae1d8e7f6a5b4c3d"

FILTERED_ROUND = parse_domain_round(GP_DOMAIN_FILTERED, T0, ref=GP_DOMAIN_FILTERED["id"])
CLEAN_ROUND = parse_domain_round(GP_DOMAIN_CLEAN, T0, ref=GP_DOMAIN_CLEAN["id"])


# --- parsing real measurements --------------------------------------------------------------------


def test_real_filtered_measurement_is_a_hijack_round() -> None:
    r = FILTERED_ROUND
    assert (r.probes, r.answered, r.hijacked, r.networks_answered, r.networks_hijacked) == (4, 4, 3, 4, 3)
    assert r.informative and r.hijack_round and r.ref == "2KbKkYjc7WD5B02U000021RGx"
    by_asn = {n.asn: n for n in r.networks}
    assert (by_asn[197207].network, by_asn[197207].hijacked) == ("همراه اول", 1)
    assert (by_asn[48715].answered, by_asn[48715].hijacked) == (1, 0)  # resolved normally
    # Rule 7: only counts per measurement network are kept, never an address.
    detail = r.detail_json()
    assert all(set(n) == {"asn", "network", "answered", "hijacked"} for n in detail)
    assert "10.10.34" not in repr(detail) and "142.251" not in repr(detail)


def test_real_clean_and_nxdomain_measurements_are_clean_answers() -> None:
    nx = parse_domain_round(GP_DOMAIN_NXDOMAIN, T0)
    for r in (CLEAN_ROUND, nx):
        assert (r.probes, r.answered, r.hijacked, r.networks_answered, r.networks_hijacked) == (4, 4, 0, 4, 0)
        assert domain_verdict([r], T0) == ("clear", [])
    # An apex without an A record answers like NXDOMAIN: a final answer, so the domain is clear.


def test_probe_results_without_a_final_answer_do_not_count() -> None:
    raw = deep(GP_DOMAIN_FILTERED)
    raw["results"][0]["result"] = {"status": "failed", "rawOutput": "x"}
    raw["results"][1]["result"]["statusCodeName"] = "SERVFAIL"
    raw["results"][1]["result"]["answers"] = []
    raw["results"][2]["result"] = {"status": "in-progress"}
    r = parse_domain_round(raw, T0)
    assert (r.probes, r.answered, r.hijacked, r.networks_answered, r.networks_hijacked) == (4, 1, 1, 1, 1)
    assert not r.hijack_round
    assert parse_domain_round({}, T0).answered == 0
    assert classify(None) is None
    assert classify({"status": "finished", "statusCodeName": "REFUSED", "answers": []}) is None
    assert classify({"status": "finished", "statusCodeName": "NOERROR", "answers": []}) == "clean"
    aaaa = {"status": "finished", "statusCodeName": "NOERROR", "answers": [{"type": "AAAA", "value": "::1"}]}
    assert classify(aaaa) == "clean"
    odd = {"status": "finished", "statusCodeName": "", "answers": [{"type": "A", "value": "10.10.34.34"}]}
    assert classify(odd) == "hijacked"


def test_request_body_asks_four_probes_in_iran_for_an_a_record() -> None:
    assert domain_dns_body("example.com") == {
        "type": "dns",
        "target": "example.com",
        "locations": [{"country": "IR", "limit": 4}],
        "measurementOptions": {"query": {"type": "A"}},
    }
    assert DOMAIN_PROBES == 4


# --- the verdict --------------------------------------------------------------------------------------


def R(ago_min: float, answered: int = 4, hijacked: int = 3, na: int = 4, nh: int = 3) -> DomainRound:
    status = "ok" if answered else "error"
    return DomainRound(
        at=T0 - timedelta(minutes=ago_min),
        status=status,
        answered=answered,
        hijacked=hijacked,
        networks_answered=na,
        networks_hijacked=nh,
    )


EMPTY = {"answered": 0, "hijacked": 0, "na": 0, "nh": 0}


@pytest.mark.parametrize(
    ("rounds", "expected"),
    [
        ([], ("unknown", ["no_answers"])),
        ([R(0, **EMPTY), R(30, **EMPTY)], ("unknown", ["no_answers"])),
        ([R(0)], ("unknown", ["single_round"])),
        ([R(0), R(30)], ("filtered", [])),
        ([R(30), R(0)], ("filtered", [])),  # order of the input does not matter
        ([R(0), R(20)], ("filtered", [])),  # exactly 20 minutes apart
        ([R(0), R(19.9)], ("unknown", ["rounds_too_close"])),
        ([R(0), R(359.9)], ("filtered", [])),
        ([R(0), R(360)], ("unknown", ["rounds_too_old"])),  # younger than 6 h, strictly
        ([R(400), R(430)], ("unknown", ["rounds_too_old"])),
        ([R(0, hijacked=1, nh=1), R(30)], ("unknown", ["weak_signal"])),  # one hijacked answer
        ([R(0, hijacked=2, nh=1), R(30)], ("unknown", ["weak_signal"])),  # one network only
        ([R(0, answered=5, hijacked=2, na=5, nh=2), R(30)], ("unknown", ["weak_signal"])),  # 40%
        ([R(0, hijacked=2, nh=2), R(30, answered=4, hijacked=2, na=3, nh=2)], ("filtered", [])),  # 50%
        ([R(0), R(30, hijacked=1, nh=1)], ("unknown", ["not_confirmed"])),
        ([R(0, hijacked=0, nh=0), R(30), R(60)], ("clear", [])),
        ([R(0), R(10, **EMPTY), R(30)], ("filtered", [])),  # a round without answers is skipped
    ],
)
def test_domain_verdict(rounds: list[DomainRound], expected: tuple[str, list[str]]) -> None:
    assert domain_verdict(rounds, T0) == expected


_rounds = st.lists(
    st.builds(
        lambda ago, answered, hijacked, nets, nh: DomainRound(
            at=T0 - timedelta(minutes=ago),
            answered=answered,
            hijacked=min(hijacked, answered),
            networks_answered=min(nets, answered),
            networks_hijacked=min(nh, nets, answered, hijacked),
        ),
        st.integers(0, 600),
        st.integers(0, 4),
        st.integers(0, 4),
        st.integers(0, 4),
        st.integers(0, 4),
    ),
    max_size=6,
)


@settings(max_examples=400)
@given(_rounds)
def test_property_filtered_needs_two_strong_rounds_far_enough_apart_and_recent(
    rounds: list[DomainRound],
) -> None:
    verdict, reasons = domain_verdict(rounds, T0)
    useful = sorted((r for r in rounds if r.answered > 0), key=lambda r: r.at, reverse=True)
    if verdict == "filtered":
        assert len(useful) >= 2 and reasons == []
        latest, previous = useful[0], useful[1]
        for r in (latest, previous):
            assert r.hijacked >= 2 and r.networks_hijacked >= 2 and r.hijacked * 2 >= r.answered
            assert T0 - r.at < MAX_AGE
        assert latest.at - previous.at >= MIN_GAP
    elif verdict == "unknown":
        assert reasons, "unknown always says why"
    if len(useful) < 2:
        assert verdict != "filtered"


# --- cadence and the shared Globalping budget ------------------------------------------------------------


def test_check_cadence() -> None:
    assert check_interval("active", False) == ACTIVE_EVERY == timedelta(minutes=30)
    assert check_interval("standby", False) == STANDBY_EVERY == timedelta(hours=6)
    assert check_interval("standby", True) == timedelta(minutes=30)  # confirm a hijacked answer soon
    for state in ("none", "filtered", "retired", "deleted"):
        assert check_interval(state, True) is None and not check_due(state, None, True, T0)
    assert check_due("active", None, False, T0)
    assert not check_due("active", T0 - timedelta(minutes=29), False, T0)
    assert check_due("active", T0 - timedelta(minutes=30), False, T0)
    assert not check_due("standby", T0 - timedelta(hours=5, minutes=59), False, T0)
    assert check_due("standby", T0 - timedelta(hours=6), False, T0)
    assert check_due("standby", T0 - timedelta(minutes=30), True, T0)
    assert suspicious([R(0, hijacked=1, nh=1), R(30, hijacked=0, nh=0)])
    assert not suspicious([R(0, hijacked=0, nh=0), R(30)])
    assert not suspicious([R(0, **EMPTY)])


def test_globalping_budget_is_shared_with_the_node_probes() -> None:
    s = ProbeSettings()
    assert node_probe_reserve(s, 0) == 0
    assert node_probe_reserve(s, 1) == 4 * 2 + 4 + 4  # control + TCP, ICMP, DNS for one node
    assert node_probe_reserve(s, 50) == node_probe_reserve(s, 10) == 4 * 11 + 40 + 40
    assert node_probe_reserve(s.model_copy(update={"enabled": False}), 5) == 0
    window = T0 + timedelta(minutes=30)
    assert budget_allows(None, T0, 124)
    assert budget_allows(RateLimit(250, 128, window), T0, 124)
    assert not budget_allows(RateLimit(250, 127, window), T0, 124)
    assert budget_allows(RateLimit(250, 0, T0 - timedelta(seconds=1)), T0, 124)  # the window reset
    a, b = RateLimit(250, 100, window), RateLimit(250, 90, window)
    assert (
        tighter_rate(a, b, T0) == b
        and tighter_rate(None, a, T0) == a
        and tighter_rate(None, None, T0) is None
    )


# --- deletion gates (pure) -------------------------------------------------------------------------


def zone_row(**kw: Any) -> PoolZone:
    base = PoolZone(
        id=ZONE_ID,
        name="old.example",
        status="active",
        pool_state="filtered",
        dns_edit=True,
        pool_changed_at=T0 - timedelta(hours=25),
        created_at=T0 - timedelta(days=30),
        filtered_at=T0 - timedelta(hours=25),
        deleted_at=None,
        delete_claimed_at=None,
        delete_blocked=None,
        delete_error=None,
        delete_attempts=0,
        delete_retry_at=None,
    )
    return replace(base, **kw)


OK_CTX = DeletionContext(records_left=0, token_verified=True, kill_switch=False, active_zone_id=ZONE_ID_2)


def test_every_deletion_condition_is_required() -> None:
    assert deletion_blockers(zone_row(), OK_CTX, T0) == []
    cases: dict[str, tuple[PoolZone, DeletionContext]] = {
        "not_filtered": (zone_row(pool_state="standby"), OK_CTX),
        "grace_period": (zone_row(filtered_at=T0 - timedelta(hours=23, minutes=59)), OK_CTX),
        "records_remaining": (zone_row(), replace(OK_CTX, records_left=1)),
        "zone_gone": (zone_row(status="gone"), OK_CTX),
        "zone_active": (zone_row(), replace(OK_CTX, active_zone_id=ZONE_ID)),
        "token_not_verified": (zone_row(), replace(OK_CTX, token_verified=False)),
        "kill_switch_on": (zone_row(), replace(OK_CTX, kill_switch=True)),
        "delete_blocked": (zone_row(delete_blocked="zone_edit_denied"), OK_CTX),
        "retry_later": (zone_row(delete_retry_at=T0 + timedelta(minutes=1)), OK_CTX),
        "delete_in_progress": (zone_row(delete_claimed_at=T0 - timedelta(minutes=1)), OK_CTX),
        "already_deleted": (zone_row(deleted_at=T0 - timedelta(hours=1)), OK_CTX),
    }
    for code, (z, ctx) in cases.items():
        assert deletion_blockers(z, ctx, T0) == [code], code
    assert deletion_blockers(zone_row(filtered_at=None), OK_CTX, T0) == ["grace_period"]
    assert deletion_blockers(zone_row(filtered_at=T0 - DELETE_GRACE), OK_CTX, T0) == []  # exactly 24 h
    assert deletion_blockers(zone_row(delete_claimed_at=T0 - CLAIM_TTL), OK_CTX, T0) == []  # stale claim
    assert deletion_blockers(zone_row(delete_retry_at=T0), OK_CTX, T0) == []
    assert DELETE_GRACE == timedelta(hours=24)


def test_delete_outcomes_and_backoff() -> None:
    # The real answer of api.cloudflare.com to an unauthorised zone delete: 403 with code 9109.
    denied = CloudflareError("denied", 403, _codes(CF_DELETE_ZONE_INVALID_TOKEN))
    assert classify_delete_error(denied) == ("zone_edit_denied", "zone_edit_denied")
    assert classify_delete_error(CloudflareError("rejected", 400, (1001, 1002))) == (
        "cf_refused",
        "cf_refused:1001,1002",
    )
    assert classify_delete_error(CloudflareError("rejected", 400)) == ("cf_refused", "cf_refused")
    assert classify_delete_error(CloudflareError("not_found", 404)) == ("already_absent", None)
    assert classify_delete_error(None) == ("deleted", None)
    for kind in ("unavailable", "rate_limited", "invalid_response"):
        assert classify_delete_error(CloudflareError(kind)) == ("retry_later", None)  # type: ignore[arg-type]
    assert [retry_delay(n) for n in (1, 2, 3)] == [timedelta(minutes=m) for m in (15, 30, 60)]
    assert retry_delay(10) == retry_delay(100) == timedelta(hours=12)


async def test_delete_zone_client_checks_the_id_and_reports_failures() -> None:
    async with CloudflareClient(random_token(), base_url=CLOSED) as cf:
        with pytest.raises(ValueError, match="not a Cloudflare id"):
            await cf.delete_zone("../zones")
        with pytest.raises(CloudflareError) as err:
            await cf.delete_zone(ZONE_ID)
    assert err.value.kind == "unavailable"


def random_token() -> SecretValue:
    return SecretValue(secrets.token_urlsafe(30))


def test_alert_texts_are_persian_and_escaped() -> None:
    text_ = filtered_alert_fa("old.example", was_active=True, replacement="new.example")
    assert "<b>old.example</b>" in text_ and "<b>new.example</b>" in text_ and "جایش فعال شد" in text_
    assert "۲۴ ساعت" in text_ and "آماده‌به‌کار" in text_
    assert "ذخیره" in filtered_alert_fa("b.example", was_active=False, replacement=None)
    assert "&lt;x&gt;" in filtered_alert_fa("<x>", was_active=True, replacement=None)
    assert "دامنه‌ی ذخیره‌ای نمانده" in NO_STANDBY_FA and "Proxy-Hub" in NO_STANDBY_FA


# --- database ----------------------------------------------------------------------------------------


async def _clean(engine: AsyncEngine) -> None:
    await clean_net_tables(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox"))
        await conn.execute(text("DELETE FROM settings WHERE key IN ('fleet', 'telegram.owner')"))


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await _clean(eng)
    yield eng
    await _clean(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


async def add_zone(
    engine: AsyncEngine,
    zone_id: str,
    name: str,
    *,
    pool: str = "none",
    status: str = "active",
    dns_edit: bool | None = True,
    created_at: datetime | None = None,
    pool_changed_at: datetime | None = None,
    filtered_at: datetime | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cf_zones (id, name, status, pool_state, dns_edit, seen_at, created_at, "
                "pool_changed_at, filtered_at) VALUES (:id, :name, :status, :pool, :edit, now(), "
                "COALESCE(:created, now()), :changed, :filtered)"
            ),
            {
                "id": zone_id,
                "name": name,
                "status": status,
                "pool": pool,
                "edit": dns_edit,
                "created": created_at,
                "changed": pool_changed_at,
                "filtered": filtered_at,
            },
        )


async def pools(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT name, pool_state FROM cf_zones"))).all()
    return {r.name: r.pool_state for r in rows}


async def zone(engine: AsyncEngine, zone_id: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT * FROM cf_zones WHERE id = :id"), {"id": zone_id})).one()


async def alerts(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT payload FROM outbox WHERE kind = 'alert' ORDER BY id"))).all()
    return [str(r.payload["text"]) for r in rows]


async def pair_owner(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await tg_store.set_owner_chat(conn, 424242, datetime.now(UTC))


async def record(engine: AsyncEngine, zone_id: str, rnd: DomainRound, now: datetime) -> rotation.RoundOutcome:
    async with engine.begin() as conn:
        out = await rotation.record_round(conn, zone_id, rnd, now)
    assert out is not None
    return out


def pool_log(log: list[dict[str, Any]]) -> list[tuple[str, str | None, str, str, str]]:
    return [
        (
            a["actor"],
            a["target"],
            a["reasons"]["reasons"][0],
            a["reasons"]["context"]["from"],
            a["reasons"]["context"]["to"],
        )
        for a in log
        if a["action"] == "cloudflare_pool_change"
    ]


@pytest.mark.db
async def test_new_zones_join_the_pool_and_the_first_seen_becomes_active(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    first = parse_zone(cf_zone_json(ZONE_ID_2, "b-first.example"))
    async with engine.begin() as conn:  # seen an hour earlier than the others
        await service.store_zones(conn, [first], {ZONE_ID_2: True}, now - timedelta(hours=1))
    # Zones the owner placed by hand: never moved automatically.
    await add_zone(
        engine, ZONE_ID_3, "kept-none.example", pool="none", pool_changed_at=now - timedelta(days=1)
    )
    await add_zone(
        engine, ZONE_ID_4, "retired.example", pool="retired", pool_changed_at=now - timedelta(days=1)
    )
    listed = [
        first,
        parse_zone(cf_zone_json(ZONE_ID, "a-second.example")),
        parse_zone(cf_zone_json("5" * 32, "pending.example", status="pending")),
        parse_zone(cf_zone_json("6" * 32, "no-edit.example")),
        parse_zone(cf_zone_json(ZONE_ID_3, "kept-none.example")),
        parse_zone(cf_zone_json(ZONE_ID_4, "retired.example")),
    ]
    edit = {z.id: z.id != "6" * 32 for z in listed}
    async with engine.begin() as conn:
        await service.store_zones(conn, listed, edit, now)
        update = await rotation.auto_pool(conn, now)
    assert sorted(update.joined) == ["a-second.example", "b-first.example"]
    assert update.promoted == "b-first.example"  # seen first, although "a-second" sorts first
    assert await pools(engine) == {
        "b-first.example": "active",
        "a-second.example": "standby",
        "pending.example": "none",  # nameservers not on Cloudflare yet
        "no-edit.example": "none",  # DNS:Edit not confirmed
        "kept-none.example": "none",
        "retired.example": "retired",
    }
    assert sorted(pool_log(await actions(engine))) == [
        ("domains", "a-second.example", "auto_new_zone", "none", "standby"),
        ("domains", "b-first.example", "auto_new_zone", "none", "standby"),
        ("domains", "b-first.example", "auto_promote", "standby", "active"),
    ]
    async with engine.connect() as conn:
        assert await rotation.current_active_domain(conn) == "b-first.example"
    # A later refresh: the pending zone is served by Cloudflare now and joins; nothing else moves.
    listed[2] = parse_zone(cf_zone_json("5" * 32, "pending.example"))
    async with engine.begin() as conn:
        await service.store_zones(conn, listed, edit, now + timedelta(hours=1))
        update = await rotation.auto_pool(conn, now + timedelta(hours=1))
    assert update.joined == ["pending.example"] and update.promoted is None
    assert (await pools(engine))["pending.example"] == "standby"
    assert (await pools(engine))["b-first.example"] == "active"


@pytest.mark.db
async def test_filtered_active_domain_is_replaced_in_the_same_transaction(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=3)
    await add_zone(
        engine, ZONE_ID, "old.example", pool="active", created_at=long_ago, pool_changed_at=long_ago
    )
    await add_zone(
        engine, ZONE_ID_2, "next.example", pool="standby", created_at=long_ago + timedelta(hours=1)
    )
    await add_zone(
        engine, ZONE_ID_3, "later.example", pool="standby", created_at=long_ago + timedelta(hours=2)
    )
    await pair_owner(engine)
    first = await record(engine, ZONE_ID, replace(FILTERED_ROUND, at=now - timedelta(minutes=35)), now)
    assert (first.verdict, first.reasons, first.filtered) == ("unknown", ["single_round"], False)
    assert await pools(engine) == {
        "old.example": "active",
        "next.example": "standby",
        "later.example": "standby",
    }
    assert await alerts(engine) == []
    second = await record(engine, ZONE_ID, replace(FILTERED_ROUND, at=now - timedelta(minutes=1)), now)
    assert (second.verdict, second.filtered, second.replacement, second.standby_left) == (
        "filtered",
        True,
        "next.example",
        1,
    )
    assert await pools(engine) == {
        "old.example": "filtered",
        "next.example": "active",
        "later.example": "standby",
    }
    old = await zone(engine, ZONE_ID)
    assert old.filtered_at is not None and abs((old.filtered_at - now).total_seconds()) < 1
    log = await actions(engine)
    assert pool_log(log) == [
        ("domains", "old.example", "domain_filtered", "active", "filtered"),
        ("domains", "next.example", "replaces_filtered", "standby", "active"),
    ]
    verdicts = [(a["target"], a["reasons"]["reasons"]) for a in log if a["action"] == "domain_verdict"]
    assert verdicts == [("old.example", ["filtered"])]  # unknown -> unknown is not a change
    (alert,) = await alerts(engine)
    assert "<b>old.example</b>" in alert and "<b>next.example</b>" in alert and "جایش فعال شد" in alert
    assert NO_STANDBY_FA not in alert
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT pool_state, verdict, ref FROM domain_checks ORDER BY at"))
        ).all()
        assert (await service.active_zone(conn)).name == "next.example"  # type: ignore[union-attr]
    assert [(r.pool_state, r.verdict) for r in rows] == [("active", "unknown"), ("active", "filtered")]
    assert rows[0].ref == GP_DOMAIN_FILTERED["id"]


@pytest.mark.db
async def test_without_a_standby_the_owner_is_told_to_buy_a_domain_once_a_day(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    await add_zone(engine, ZONE_ID, "only.example", pool="active", pool_changed_at=now - timedelta(days=1))
    await record(engine, ZONE_ID, replace(FILTERED_ROUND, at=now - timedelta(minutes=40)), now)
    out = await record(engine, ZONE_ID, replace(FILTERED_ROUND, at=now - timedelta(minutes=5)), now)
    assert out.filtered and out.replacement is None and out.standby_left == 0
    assert await pools(engine) == {"only.example": "filtered"}
    assert await alerts(engine) == []  # no owner chat paired: nothing queued (the log has it)
    async with engine.connect() as conn:
        assert await rotation.current_active_domain(conn) is None
        since = (await conn.execute(text("SELECT standby_alert_at FROM cf_state"))).scalar_one()
    assert since is not None
    await pair_owner(engine)
    for hours, expected in ((1, False), (23, False), (24, True), (30, False), (48, True)):
        async with engine.begin() as conn:
            assert await rotation.remind_no_standby(conn, since + timedelta(hours=hours)) is expected, hours
    assert await alerts(engine) == ["⚠️ " + NO_STANDBY_FA] * 2
    assert [a["action"] for a in await actions(engine)].count("domain_no_standby") == 2
    # A new domain joins as a usable standby: the reminders stop.
    await add_zone(engine, ZONE_ID_2, "fresh.example", pool="standby")
    async with engine.begin() as conn:
        assert await rotation.remind_no_standby(conn, since + timedelta(days=5)) is False
        assert (await conn.execute(text("SELECT standby_alert_at FROM cf_state"))).scalar_one() is None
    assert len(await alerts(engine)) == 2


@pytest.mark.db
async def test_filtered_standby_leaves_the_pool_and_the_owner_can_bring_it_back(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    await add_zone(engine, ZONE_ID, "live.example", pool="active", pool_changed_at=now - timedelta(days=1))
    await add_zone(engine, ZONE_ID_2, "spare.example", pool="standby")
    await pair_owner(engine)
    await record(engine, ZONE_ID_2, replace(FILTERED_ROUND, at=now - timedelta(minutes=31)), now)
    out = await record(engine, ZONE_ID_2, replace(FILTERED_ROUND, at=now), now)
    assert out.filtered and out.replacement is None
    assert await pools(engine) == {"live.example": "active", "spare.example": "filtered"}
    (alert,) = await alerts(engine)
    assert "دامنه‌ی ذخیره‌ی <b>spare.example</b>" in alert and NO_STANDBY_FA in alert
    # The owner disagrees and moves it back during the grace period: the deletion is cancelled and
    # the rounds measured before the owner's change no longer count.
    sm = async_sessionmaker(engine, expire_on_commit=False)
    later = now + timedelta(minutes=10)
    async with sm() as session:
        status = await service.set_pool_state(
            session, ZONE_ID_2, PoolState.STANDBY, actor="admin:owner", now=later
        )
    spare = next(z for z in status["zones"] if z["id"] == ZONE_ID_2)
    assert (spare["pool_state"], spare["filtered_at"], spare["delete_after"]) == ("standby", None, None)
    again = await record(engine, ZONE_ID_2, replace(FILTERED_ROUND, at=later + timedelta(minutes=1)), later)
    assert (again.verdict, again.reasons) == ("unknown", ["single_round"])
    before = await record(engine, ZONE_ID_2, replace(FILTERED_ROUND, at=later - timedelta(minutes=1)), later)
    assert (before.verdict, before.reasons) == ("unknown", ["single_round"])  # measured before the change
    assert (await pools(engine))["spare.example"] == "standby"


@pytest.mark.db
async def test_unknown_or_single_rounds_never_move_anything(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    await add_zone(engine, ZONE_ID, "live.example", pool="active", pool_changed_at=now - timedelta(days=1))
    await add_zone(engine, ZONE_ID_2, "spare.example", pool="standby")
    await add_zone(engine, ZONE_ID_3, "kept.example", pool="retired", pool_changed_at=now - timedelta(days=1))
    weak = replace(FILTERED_ROUND, hijacked=1, networks_hijacked=1)
    expected = ["single_round", "rounds_too_close", "rounds_too_close", "weak_signal"]
    for rnd, why in zip(
        [
            replace(FILTERED_ROUND, at=now - timedelta(minutes=10)),
            replace(FILTERED_ROUND, at=now),  # 10 minutes apart: too close
            DomainRound(at=now + timedelta(minutes=40), status="error"),  # Globalping failed
            replace(weak, at=now + timedelta(minutes=80)),
        ],
        expected,
        strict=True,
    ):
        out = await record(engine, ZONE_ID, rnd, rnd.at)
        assert (out.verdict, out.reasons, out.filtered) == ("unknown", [why], False)
    # A retired zone (the owner's choice) is never changed, whatever its checks say.
    await record(engine, ZONE_ID_3, replace(FILTERED_ROUND, at=now - timedelta(minutes=40)), now)
    out = await record(engine, ZONE_ID_3, replace(FILTERED_ROUND, at=now), now)
    assert out.verdict == "filtered" and not out.filtered
    assert await pools(engine) == {
        "live.example": "active",
        "spare.example": "standby",
        "kept.example": "retired",
    }
    assert pool_log(await actions(engine)) == []


@pytest.mark.db
async def test_promotion_skips_a_standby_whose_last_check_looked_filtered(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    day = now - timedelta(days=2)
    await add_zone(engine, ZONE_ID, "live.example", pool="active", created_at=day, pool_changed_at=day)
    await add_zone(engine, ZONE_ID_2, "early.example", pool="standby", created_at=day + timedelta(hours=1))
    await add_zone(engine, ZONE_ID_3, "late.example", pool="standby", created_at=day + timedelta(hours=2))
    await record(
        engine, ZONE_ID_2, replace(FILTERED_ROUND, at=now - timedelta(hours=1)), now
    )  # one bad round
    await record(engine, ZONE_ID, replace(FILTERED_ROUND, at=now - timedelta(minutes=30)), now)
    out = await record(engine, ZONE_ID, replace(FILTERED_ROUND, at=now), now)
    assert out.replacement == "late.example"
    async with engine.connect() as conn:
        due = {d.zone.name for d in await due_checks(conn, now + timedelta(minutes=31))}
    assert "early.example" in due  # its hijacked answer is re-checked after 30 minutes, not 6 hours


# --- deletion on the real database ------------------------------------------------------------------


class _CountingCf(CloudflareClient):
    """The real client, pointed at a closed local port, counting the requests it attempts."""

    attempts = 0

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        type(self).attempts += 1
        return await super()._request(method, path, **kwargs)


def counting_cf() -> tuple[type[_CountingCf], Any]:
    cls = type("CountingCf", (_CountingCf,), {"attempts": 0})
    return cls, lambda token: cls(token, base_url=CLOSED)


async def deletable_setup(engine: AsyncEngine, vault: Vault, *, kill_switch: bool = False) -> datetime:
    now = datetime.now(UTC)
    await add_zone(engine, ZONE_ID_2, "live.example", pool="active")
    await add_zone(
        engine,
        ZONE_ID,
        "old.example",
        pool="filtered",
        pool_changed_at=now - timedelta(hours=25),
        filtered_at=now - timedelta(hours=25),
    )
    async with engine.begin() as conn:
        await vault.put(conn, TOKEN_SECRET, secrets.token_urlsafe(30))
        await service.save_state(conn, now, verified=True, checked_at=now, zones_refreshed_at=now, missing=[])
        await save_fleet_settings(conn, FleetSettings(kill_switch=kill_switch))
    return now


async def deletable(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return await deletion.deletable_zones(conn, datetime.now(UTC))


@pytest.mark.db
async def test_deletion_waits_for_every_condition_and_never_calls_cloudflare_before(
    engine: AsyncEngine, vault: Vault
) -> None:
    await deletable_setup(engine, vault, kill_switch=True)
    counting, factory = counting_cf()
    rec = DomainsReconciler(vault_factory=lambda: vault, client_factory=factory)
    sql = {
        "kill switch is on": None,  # the setup's state
        "our record is still on the zone": (
            "INSERT INTO cf_records (id, zone_id, cf_record_id, name, type, content, ttl, proxied, purpose) "
            "VALUES (gen_random_uuid(), :z, NULL, 'slot1.old.example', 'A', '203.0.113.1', 60, false, "
            "'slot')",
            "DELETE FROM cf_records",
        ),
        "still in the grace period": (
            "UPDATE cf_zones SET filtered_at = now() - interval '23 hours' WHERE id = :z",
            "UPDATE cf_zones SET filtered_at = now() - interval '25 hours' WHERE id = :z",
        ),
        "token not verified": ("UPDATE cf_state SET verified = false", "UPDATE cf_state SET verified = true"),
        "token no longer sees the zone": (
            "UPDATE cf_zones SET status = 'gone' WHERE id = :z",
            "UPDATE cf_zones SET status = 'active' WHERE id = :z",
        ),
        "token removed from the vault": (
            "UPDATE secrets SET name = 'cloudflare:parked' WHERE name = 'cloudflare:token'",
            "UPDATE secrets SET name = 'cloudflare:token' WHERE name = 'cloudflare:parked'",
        ),
        "deletion refused before": (
            "UPDATE cf_zones SET delete_blocked = 'zone_edit_denied' WHERE id = :z",
            "UPDATE cf_zones SET delete_blocked = NULL WHERE id = :z",
        ),
    }
    assert await deletable(engine) == []
    assert await rec.delete_filtered(engine) == [] and counting.attempts == 0
    async with engine.begin() as conn:
        await save_fleet_settings(conn, FleetSettings(kill_switch=False))
    assert await deletable(engine) == [ZONE_ID]
    for why, change in sql.items():
        if change is None:
            continue
        async with engine.begin() as conn:
            await conn.execute(text(change[0]), {"z": ZONE_ID})
        assert await deletable(engine) == [], why
        async with factory(random_token()) as cf:  # the deciding transaction refuses on its own too
            assert await deletion.delete_filtered_zone(engine, cf, ZONE_ID, datetime.now(UTC)) == "blocked", (
                why
            )
        assert await rec.delete_filtered(engine) == [], why
        assert counting.attempts == 0, why
        async with engine.begin() as conn:
            await conn.execute(text(change[1]), {"z": ZONE_ID})
    assert await deletable(engine) == [ZONE_ID]
    # Every condition holds: ONE call is made. The closed port makes it fail like a Cloudflare
    # outage, so it is retried later with backoff and nothing else changes.
    assert await rec.delete_filtered(engine) == ["retry_later"]
    assert counting.attempts == 1
    row = await zone(engine, ZONE_ID)
    assert (row.pool_state, row.delete_attempts, row.delete_error, row.delete_claimed_at) == (
        "filtered",
        1,
        "unavailable",
        None,
    )
    assert timedelta(minutes=14) < row.delete_retry_at - datetime.now(UTC) <= timedelta(minutes=15)
    assert await rec.delete_filtered(engine) == [] and counting.attempts == 1  # backoff holds
    log = [(a["action"], a["allowed"], a["reasons"]["reasons"]) for a in await actions(engine)]
    assert log == [("cloudflare_zone_delete", True, ["cf_unavailable"])]
    # A token stored again lets refused or postponed deletions start over.
    async with engine.begin() as conn:
        assert await deletion.allow_deletion_retries(conn) == 1
    assert await deletable(engine) == [ZONE_ID]


@pytest.mark.db
async def test_owner_moving_the_zone_back_cancels_the_deletion_and_a_claim_blocks_the_owner(
    engine: AsyncEngine, vault: Vault
) -> None:
    await deletable_setup(engine, vault)
    assert await deletable(engine) == [ZONE_ID]
    sm = async_sessionmaker(engine, expire_on_commit=False)
    # A deletion decided a moment ago is in flight: the owner's change waits for it.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE cf_zones SET delete_claimed_at = now() WHERE id = :z"), {"z": ZONE_ID}
        )
    async with sm() as session:
        with pytest.raises(service.CfServiceError) as err:
            await service.set_pool_state(
                session, ZONE_ID, PoolState.STANDBY, actor="admin:o", now=datetime.now(UTC)
            )
    assert err.value.code == "zone_delete_in_progress"
    assert await deletable(engine) == []  # and no second attempt starts meanwhile
    async with engine.begin() as conn:  # a claim left by a crash expires
        await conn.execute(
            text("UPDATE cf_zones SET delete_claimed_at = now() - interval '6 minutes' WHERE id = :z"),
            {"z": ZONE_ID},
        )
    async with sm() as session:
        await service.set_pool_state(
            session, ZONE_ID, PoolState.STANDBY, actor="admin:o", now=datetime.now(UTC)
        )
    row = await zone(engine, ZONE_ID)
    assert (row.pool_state, row.filtered_at) == ("standby", None)
    assert await deletable(engine) == []
    # Marked filtered again by the owner: a NEW grace period starts.
    async with sm() as session:
        await service.set_pool_state(
            session, ZONE_ID, PoolState.FILTERED, actor="admin:o", now=datetime.now(UTC)
        )
    row = await zone(engine, ZONE_ID)
    assert row.filtered_at is not None and datetime.now(UTC) - row.filtered_at < timedelta(minutes=1)
    assert await deletable(engine) == []


@pytest.mark.db
async def test_delete_results_are_recorded_and_told_to_the_owner(engine: AsyncEngine, vault: Vault) -> None:
    now = await deletable_setup(engine, vault)
    await pair_owner(engine)
    for zid, name in (
        (ZONE_ID_3, "denied.example"),
        (ZONE_ID_4, "registrar.example"),
        ("7" * 32, "gone.example"),
    ):
        await add_zone(
            engine,
            zid,
            name,
            pool="filtered",
            pool_changed_at=now - timedelta(days=2),
            filtered_at=now - timedelta(days=2),
        )

    async def result(zone_id: str, error: CloudflareError | None) -> str:
        async with engine.begin() as conn:
            z = next(z for z in await rotation.load_zones(conn, lock=True) if z.id == zone_id)
            return await deletion.record_delete_result(conn, z, datetime.now(UTC), error)

    assert await result(ZONE_ID, None) == "deleted"
    assert await result(ZONE_ID_3, CloudflareError("denied", 403, (9109,))) == "zone_edit_denied"
    assert await result(ZONE_ID_4, CloudflareError("rejected", 400, (1001,))) == "cf_refused"
    assert await result("7" * 32, CloudflareError("not_found", 404, (1049,))) == "already_absent"
    assert await pools(engine) == {
        "live.example": "active",
        "old.example": "deleted",
        "denied.example": "filtered",
        "registrar.example": "filtered",
        "gone.example": "deleted",
    }
    assert (await zone(engine, ZONE_ID)).deleted_at is not None
    assert (await zone(engine, ZONE_ID_3)).delete_blocked == "zone_edit_denied"
    assert (await zone(engine, ZONE_ID_4)).delete_blocked == "cf_refused:1001"
    assert await deletable(engine) == []  # refused ones wait for a new token
    texts = await alerts(engine)
    assert len(texts) == 3  # deleted, denied, refused; a zone already gone needs no news
    assert "old.example" in texts[0] and "پاک شد" in texts[0]
    assert "Zone · Zone · Edit" in texts[1] and "Registrar" in texts[2]
    log = [(a["target"], a["allowed"], a["reasons"]["reasons"]) for a in await actions(engine)]
    assert log == [
        ("old.example", True, ["deleted"]),
        ("denied.example", False, ["zone_edit_denied"]),
        ("registrar.example", False, ["cf_refused"]),
        ("gone.example", True, ["already_absent"]),
    ]
    # "deleted" is terminal: the owner cannot move it, and the API never offers it.
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        with pytest.raises(service.CfServiceError) as err:
            await service.set_pool_state(
                session, ZONE_ID, PoolState.STANDBY, actor="admin:o", now=datetime.now(UTC)
            )
    assert err.value.code == "pool_transition_invalid"
    async with sm() as session:
        with pytest.raises(service.CfServiceError):
            await service.set_pool_state(
                session, ZONE_ID_3, PoolState.DELETED, actor="admin:o", now=datetime.now(UTC)
            )
    with pytest.raises(IntegrityError):  # the database knows the states too
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE cf_zones SET pool_state = 'burned' WHERE id = :z"), {"z": ZONE_ID}
            )


# --- the loop ------------------------------------------------------------------------------------------


class _CountingGp(GlobalpingClient):
    attempts = 0

    async def create(self, body: Any) -> Any:
        type(self).attempts += 1
        return await super().create(body)


def counting_gp() -> tuple[type[_CountingGp], Any]:
    cls = type("CountingGp", (_CountingGp,), {"attempts": 0})
    return cls, lambda: cls(base_url=CLOSED, poll_seconds=0.01)


@pytest.mark.db
async def test_loop_does_nothing_without_pool_domains(engine: AsyncEngine, vault: Vault) -> None:
    gp, gp_factory = counting_gp()
    cf, cf_factory = counting_cf()
    rec = DomainsReconciler(vault_factory=lambda: vault, client_factory=cf_factory, gp_factory=gp_factory)
    await add_zone(engine, ZONE_ID, "unused.example", pool="none", pool_changed_at=datetime.now(UTC))
    async with engine.begin() as conn:
        await rec.run_once(conn)
    assert gp.attempts == 0 and cf.attempts == 0
    assert await actions(engine) == []


@pytest.mark.db
async def test_loop_checks_the_active_domain_and_handles_an_unreachable_globalping(
    engine: AsyncEngine, vault: Vault
) -> None:
    gp, gp_factory = counting_gp()
    cf, cf_factory = counting_cf()
    rec = DomainsReconciler(vault_factory=lambda: vault, client_factory=cf_factory, gp_factory=gp_factory)
    await add_zone(engine, ZONE_ID, "live.example", pool="active")
    await add_zone(engine, ZONE_ID_2, "spare.example", pool="standby")
    async with engine.begin() as conn:
        await rec.run_once(conn)
    assert gp.attempts == 2  # the active domain first, then the standby one (both never checked)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT zone_id, pool_state, status, answered, verdict, verdict_reasons "
                    "FROM domain_checks"
                )
            )
        ).all()
    assert sorted((r.pool_state, r.status, r.answered, r.verdict, r.verdict_reasons) for r in rows) == [
        ("active", "error", 0, "unknown", ["no_answers"]),
        ("standby", "error", 0, "unknown", ["no_answers"]),
    ]
    async with engine.begin() as conn:  # nothing is due a minute later
        await rec.run_once(conn)
    assert gp.attempts == 2 and cf.attempts == 0
    assert await pools(engine) == {"live.example": "active", "spare.example": "standby"}


@pytest.mark.db
async def test_loop_keeps_globalping_tests_for_the_node_probes(engine: AsyncEngine, vault: Vault) -> None:
    from test_net_support import make_node

    gp, gp_factory = counting_gp()
    _, cf_factory = counting_cf()
    rec = DomainsReconciler(vault_factory=lambda: vault, client_factory=cf_factory, gp_factory=gp_factory)
    await add_zone(engine, ZONE_ID, "live.example", pool="active")
    await make_node(engine, state="ready", ipv4="203.0.113.40")  # node probes need 16 tests per cycle
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO probe_sources (name, ok, note, checked_at, ratelimit_limit, "
                "ratelimit_remaining, ratelimit_reset_at) "
                "VALUES ('globalping', true, 'x', now(), 250, 19, now() + interval '20 minutes')"
            )
        )
    for _ in range(3):
        async with engine.begin() as conn:
            await rec.run_once(conn)
    assert gp.attempts == 0
    log = [(a["action"], a["reasons"]["reasons"]) for a in await actions(engine)]
    assert log == [("domain_check_skipped", ["globalping_budget_low"])]  # logged once, not every minute
    async with engine.begin() as conn:  # 20 = 4 for the check + 16 kept for the nodes
        await conn.execute(text("UPDATE probe_sources SET ratelimit_remaining = 20"))
    async with engine.begin() as conn:
        await rec.run_once(conn)
    assert gp.attempts == 1


def test_worker_registers_the_domains_loop() -> None:
    from proxyhub.worker import reconcilers

    loops = {r.name: r for r in reconcilers()}
    assert loops["domains"].interval_seconds == 60


# --- API ------------------------------------------------------------------------------------------------


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[cloudflare_vault] = lambda: vault
    try:
        async with lifespan(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://panel.test", headers=CSRF) as c:
                yield c
    finally:
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(cloudflare_vault, None)


@pytest.mark.db
@pytest.mark.redis
async def test_status_shows_checks_deletion_dates_and_the_pool(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    now = await deletable_setup(engine, vault, kill_switch=True)
    await add_zone(engine, ZONE_ID_3, "spare.example", pool="standby")
    await add_zone(engine, ZONE_ID_4, "gone.example", pool="deleted", pool_changed_at=now)
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE cf_zones SET deleted_at = now() WHERE id = :z"), {"z": ZONE_ID_4})
        await conn.execute(
            text(
                "INSERT INTO cf_records (id, zone_id, cf_record_id, name, type, content, ttl, proxied, "
                "purpose) VALUES (gen_random_uuid(), :z, NULL, 'slot1.old.example', 'A', '203.0.113.1', 60, "
                "false, 'slot')"
            ),
            {"z": ZONE_ID},
        )
    await record(engine, ZONE_ID_3, replace(CLEAN_ROUND, at=now), now)
    body = (await client.get("/api/cloudflare/status")).json()
    zones = {z["name"]: z for z in body["zones"]}
    old = zones["old.example"]
    assert old["pool_state"] == "filtered" and old["filtered_at"].endswith("Z")
    after = datetime.fromisoformat(old["delete_after"])
    assert after == datetime.fromisoformat(old["filtered_at"]) + timedelta(hours=24)
    assert old["delete_waiting"] == ["records_remaining", "kill_switch_on"]
    assert old["delete_blocked"] is None and old["deleted_at"] is None and old["last_check"] is None
    check = zones["spare.example"]["last_check"]
    assert {k: check[k] for k in ("status", "verdict", "answered", "hijacked", "networks_answered")} == {
        "status": "ok",
        "verdict": "clear",
        "answered": 4,
        "hijacked": 0,
        "networks_answered": 4,
    }
    assert {n["asn"] for n in check["networks"]} == {202468, 197207, 59580, 48715}
    assert zones["spare.example"]["delete_after"] is None and zones["spare.example"]["delete_waiting"] == []
    assert zones["gone.example"]["pool_state"] == "deleted" and zones["gone.example"]["deleted_at"]
    assert body["pool"] == {
        "standby_usable": 1,
        "in_use": True,
        "kill_switch": True,
        "delete_grace_hours": 24,
        "standby_check_hours": 6,
    }
    r = await client.put("/api/cloudflare/pool", json={"zone_id": ZONE_ID, "pool_state": "deleted"})
    assert r.status_code == 422
    r = await client.put("/api/cloudflare/pool", json={"zone_id": ZONE_ID, "pool_state": "standby"})
    assert r.status_code == 200
    moved = next(z for z in r.json()["zones"] if z["id"] == ZONE_ID)
    assert (moved["pool_state"], moved["filtered_at"], moved["delete_after"], moved["delete_waiting"]) == (
        "standby",
        None,
        None,
        [],
    )


@pytest.mark.db
@pytest.mark.redis
async def test_owner_change_during_a_deletion_gets_a_persian_409(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    await deletable_setup(engine, vault)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE cf_zones SET delete_claimed_at = now() WHERE id = :z"), {"z": ZONE_ID}
        )
    r = await client.put("/api/cloudflare/pool", json={"zone_id": ZONE_ID, "pool_state": "standby"})
    assert r.status_code == 409 and r.json()["error"] == "zone_delete_in_progress" and r.json()["message_fa"]


# --- migration -------------------------------------------------------------------------------------------


@pytest.mark.db
async def test_migration_0010_round_trip_and_no_drift(engine: AsyncEngine) -> None:
    from alembic import command
    from alembic.script import ScriptDirectory

    from proxyhub.cli import _alembic_config, downgrade, migrate

    cfg = _alembic_config()
    assert ScriptDirectory.from_config(cfg).get_heads() == ["0010_domains"]
    await add_zone(engine, ZONE_ID, "old.example", pool="deleted")
    await asyncio.to_thread(downgrade, "0009_merge")
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT pool_state FROM cf_zones"))).scalar_one() == "retired"
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE cf_zones SET pool_state = 'filtered'"))
    await asyncio.to_thread(migrate, "head")
    row = await zone(engine, ZONE_ID)
    assert row.pool_state == "filtered" and row.filtered_at is not None and row.delete_attempts == 0
    await asyncio.to_thread(command.check, cfg)  # the models and the migrations agree


# --- real public APIs (opt-in with the other real calls: PROXYHUB_GLOBALPING_TESTS=1) ----------------


@pytest.mark.globalping
async def test_real_filter_check_of_a_filtered_domain_from_iran() -> None:
    """youtube.com is filtered in Iran; one real check costs 4 Globalping tests."""
    from proxyhub.reconcilers.domains import measure

    async with GlobalpingClient() as gp:
        rnd = await measure(gp, "youtube.com")
    print("\nREAL DOMAIN CHECK:", rnd.status, rnd.answered, rnd.hijacked, rnd.detail_json())
    if not rnd.informative:
        pytest.skip(f"no probe in Iran answered (status {rnd.status})")
    assert rnd.hijacked >= 1 and rnd.probes <= DOMAIN_PROBES


@pytest.mark.globalping
async def test_real_cloudflare_refuses_a_zone_delete_without_the_permission() -> None:
    """A random token and a random zone id: nothing can be deleted. Cloudflare answers 403 (code
    9109), which the panel records as "the token lacks Zone Edit" and never retries by itself."""
    async with CloudflareClient(random_token()) as cf:
        with pytest.raises(CloudflareError) as err:
            await cf.delete_zone(secrets.token_hex(16))
    assert (err.value.kind, err.value.status) == ("denied", 403)
    assert classify_delete_error(err.value) == ("zone_edit_denied", "zone_edit_denied")
