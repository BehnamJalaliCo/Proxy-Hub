"""Cloudflare DNS and the domain pool: the managed-record rule, the planner, the pool state machine,
the service and the API, on real PostgreSQL and over real HTTP (ASGI).

Cloudflare itself is never imitated (decision D10): record and zone JSON are data in Cloudflare's
documented shape, fed to the real parser and the real decision functions. Calls to the real API
are in tests/test_net_cloudflare_real.py.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_fleet_support import ADMIN, CSRF, assert_absent_from_database, make_vault
from test_net_support import (
    CF_INVALID_TOKEN_VERIFY,
    ZONE_ID,
    ZONE_ID_2,
    actions,
    cf_record_json,
    cf_zone_json,
    clean_net_tables,
    make_node,
)

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_cloudflare import cloudflare_vault
from proxyhub.cloudflare import service
from proxyhub.cloudflare.client import (
    CloudflareClient,
    CloudflareError,
    DnsRecord,
    _codes,
    parse_record,
    parse_zone,
    valid_token_format,
)
from proxyhub.cloudflare.pool import ALLOWED, PoolState, PoolTransitionError, check_pool_transition
from proxyhub.cloudflare.records import (
    MARKER,
    Change,
    Desired,
    OwnedRow,
    comment_ref,
    has_marker,
    managed_comment,
    owned_row,
    plan_zone,
    same_content,
)
from proxyhub.reconcilers.dns import DnsReconciler
from proxyhub.vault import SecretValue, Vault

CF_PATHS = {
    ("GET", "/api/cloudflare/status"),
    ("PUT", "/api/cloudflare/token"),
    ("PUT", "/api/cloudflare/pool"),
}
ZONE = "example.com"


def remote(name: str, content: str, **kw: object) -> DnsRecord:
    return parse_record(ZONE_ID, cf_record_json(name, content, **kw))  # type: ignore[arg-type]


def row_for(rec: DnsRecord, *, ref: uuid.UUID | None = None, pending: bool = False) -> OwnedRow:
    return OwnedRow(
        ref=ref or uuid.uuid4(),
        zone_id=rec.zone_id,
        cf_record_id=None if pending else rec.id,
        name=rec.name,
        type=rec.type,
        content=rec.content,
        ttl=rec.ttl,
        proxied=rec.proxied,
    )


def ours(name: str, content: str) -> tuple[DnsRecord, OwnedRow]:
    ref = uuid.uuid4()
    rec = remote(name, content, comment=managed_comment(ref))
    return rec, row_for(rec, ref=ref)


# --- parsing real shapes ------------------------------------------------------------------------


def test_parse_zone_and_record_shapes() -> None:
    zone = parse_zone(cf_zone_json(name="Example.COM"))
    assert (zone.id, zone.name, zone.status, zone.account_id) == (
        ZONE_ID,
        "example.com",
        "active",
        "372e67954025e0ba6aaa6d586b9e0b59",
    )
    rec = remote("Slot1.Example.com", "198.51.100.4", comment=None)
    assert (rec.name, rec.type, rec.ttl, rec.proxied, rec.comment) == (
        "slot1.example.com",
        "A",
        60,
        False,
        None,
    )
    auto = parse_record(ZONE_ID, cf_record_json("x.example.com", "198.51.100.4", ttl=1, proxied=True))
    assert auto.ttl == 1 and auto.proxied
    assert _codes(CF_INVALID_TOKEN_VERIFY) == (1000,)


def test_token_format_check_runs_before_any_network_call() -> None:
    assert valid_token_format("A" * 40)
    assert valid_token_format("abc_DEF-123." + "x" * 20)
    for bad in ("", "short", "a" * 201, "has space" + "x" * 30, "new\nline" + "x" * 30, "ü" * 40):
        assert not valid_token_format(bad)


# --- the managed-record rule --------------------------------------------------------------------


def test_marker_and_ref() -> None:
    ref = uuid.uuid4()
    comment = managed_comment(ref)
    assert comment == f"{MARKER} ref={ref}" and len(comment) <= 100  # free-plan comment limit
    assert has_marker(comment) and comment_ref(comment) == ref
    assert not has_marker(None) and not has_marker("managed-by=proxy-hubX") and not has_marker("hub")
    assert has_marker(f"note {MARKER}")
    assert comment_ref("ref=not-a-uuid") is None and comment_ref(None) is None


def test_owned_row_requires_marker_and_row() -> None:
    rec, row = ours("slot1.example.com", "198.51.100.4")
    assert owned_row(rec, [row]) == row
    # Marker but no row of ours (another panel, or a copy): foreign.
    assert owned_row(rec, []) is None
    # Our row by id but the marker was removed in the Cloudflare dashboard: foreign.
    unmarked = remote("slot1.example.com", "198.51.100.4", record_id=rec.id, comment="owner's note")
    assert owned_row(unmarked, [row]) is None
    # Same id in another zone: foreign.
    other_zone = OwnedRow(**{**row.__dict__, "zone_id": ZONE_ID_2})
    assert owned_row(rec, [other_zone]) is None
    # Pending row (crash before the id was stored) is matched by ref AND name AND type only.
    pending = row_for(rec, ref=row.ref, pending=True)
    assert owned_row(rec, [pending]) == pending
    renamed = remote("slot2.example.com", "198.51.100.4", comment=managed_comment(row.ref))
    assert owned_row(renamed, [pending]) is None
    stranger_ref = remote("slot1.example.com", "198.51.100.4", comment=managed_comment(uuid.uuid4()))
    assert owned_row(stranger_ref, [pending]) is None


def test_plan_create_update_delete_only_ours() -> None:
    foreign = remote("www.example.com", "192.0.2.1", comment="site")
    marked_foreign = remote("slot9.example.com", "192.0.2.9", comment=f"{MARKER} ref={uuid.uuid4()}")
    stale, stale_row = ours("slot2.example.com", "198.51.100.2")
    moved, moved_row = ours("slot1.example.com", "198.51.100.1")
    desired = [
        Desired("slot1.example.com", "A", "203.0.113.1"),
        Desired("slot3.example.com", "A", "203.0.113.3"),
    ]
    changes = plan_zone(ZONE_ID, desired, [foreign, marked_foreign, stale, moved], [stale_row, moved_row])
    ops = {(c.op, c.name) for c in changes}
    assert ops == {
        ("update", "slot1.example.com"),
        ("create", "slot3.example.com"),
        ("delete", "slot2.example.com"),
    }
    for c in changes:
        if c.op in ("update", "delete"):
            assert c.remote is not None and owned_row(c.remote, [stale_row, moved_row]) is not None
    create = next(c for c in changes if c.op == "create")
    assert create.desired is not None and create.desired.ttl == 60 and create.desired.proxied is False
    spec = create.desired.spec(uuid.uuid4())
    assert has_marker(spec.comment) and spec.body()["proxied"] is False and spec.body()["ttl"] == 60


def test_plan_conflict_when_a_foreign_record_uses_the_name() -> None:
    owner_a = remote("slot1.example.com", "192.0.2.50", comment=None)
    owner_txt = remote("slot2.example.com", '"v=spf1 -all"', rtype="TXT")
    changes = plan_zone(
        ZONE_ID,
        [Desired("slot1.example.com", "A", "203.0.113.1"), Desired("slot2.example.com", "A", "203.0.113.2")],
        [owner_a, owner_txt],
        [],
    )
    assert [(c.op, c.name) for c in changes] == [
        ("conflict", "slot1.example.com"),
        ("conflict", "slot2.example.com"),
    ]


def test_plan_after_crash_adopts_by_ref_and_forgets_vanished_records() -> None:
    created = remote("slot1.example.com", "203.0.113.1", comment=None)
    ref = uuid.uuid4()
    created = remote("slot1.example.com", "203.0.113.1", record_id=created.id, comment=managed_comment(ref))
    pending = row_for(created, ref=ref, pending=True)
    vanished_row = OwnedRow(
        uuid.uuid4(), ZONE_ID, secrets.token_hex(16), "slot2.example.com", "A", "1.2.3.4", 60, False
    )
    changes = plan_zone(
        ZONE_ID,
        [Desired("slot1.example.com", "A", "203.0.113.1"), Desired("slot2.example.com", "A", "203.0.113.2")],
        [created],
        [pending, vanished_row],
    )
    assert [(c.op, c.name) for c in changes] == [
        ("adopt", "slot1.example.com"),
        ("forget", "slot2.example.com"),
        ("create", "slot2.example.com"),
    ]
    # Two records claiming one pending row: only one is adopted, the other stays foreign.
    twin = remote("slot1.example.com", "203.0.113.1", comment=managed_comment(ref))
    changes = plan_zone(
        ZONE_ID, [Desired("slot1.example.com", "A", "203.0.113.1")], [created, twin], [pending]
    )
    assert [c.op for c in changes].count("adopt") == 1
    assert ("conflict", "slot1.example.com") in {(c.op, c.name) for c in changes}


def test_plan_is_stable_for_txt_quotes_and_case() -> None:
    assert same_content("TXT", '"hello world"', "hello world")
    assert same_content("A", "203.0.113.1", "203.0.113.1 ")
    rec, row = ours("t.example.com", '"proxy-hub test"')
    rec = remote(
        "t.example.com", '"proxy-hub test"', rtype="TXT", record_id=rec.id, comment=managed_comment(row.ref)
    )
    row = row_for(rec, ref=row.ref)
    assert plan_zone(ZONE_ID, [Desired("T.Example.com", "TXT", "proxy-hub test")], [rec], [row]) == []


_names = st.sampled_from([f"slot{i}.example.com" for i in range(1, 6)] + ["www.example.com", "example.com"])
_ips = st.sampled_from(["203.0.113.1", "203.0.113.2", "198.51.100.7"])


@st.composite
def _zone_state(draw: st.DrawFn) -> tuple[list[DnsRecord], list[OwnedRow], list[Desired]]:
    recs: list[DnsRecord] = []
    rows: list[OwnedRow] = []
    for _ in range(draw(st.integers(0, 8))):
        kind = draw(st.sampled_from(["ours", "foreign", "marked_foreign", "unmarked_with_row", "pending"]))
        name, ip = draw(_names), draw(_ips)
        ref = uuid.uuid4()
        if kind == "foreign":
            recs.append(remote(name, ip, comment=draw(st.sampled_from([None, "owner", "managed-by=other"]))))
        elif kind == "marked_foreign":
            recs.append(remote(name, ip, comment=managed_comment(ref)))
        elif kind == "unmarked_with_row":
            rec = remote(name, ip, comment="edited by hand")
            recs.append(rec)
            rows.append(row_for(rec, ref=ref))
        elif kind == "pending":
            rec = remote(name, ip, comment=managed_comment(ref))
            recs.append(rec)
            rows.append(row_for(rec, ref=ref, pending=True))
        else:
            rec = remote(name, ip, comment=managed_comment(ref))
            recs.append(rec)
            rows.append(row_for(rec, ref=ref))
    desired = [Desired(n, "A", draw(_ips)) for n in draw(st.sets(_names, max_size=5))]
    return recs, rows, desired


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(_zone_state())
def test_property_foreign_records_are_never_touched(
    state: tuple[list[DnsRecord], list[OwnedRow], list[Desired]],
) -> None:
    recs, rows, desired = state
    changes = plan_zone(ZONE_ID, desired, recs, rows)
    for c in changes:
        if c.op in ("update", "delete"):
            assert c.remote is not None
            assert has_marker(c.remote.comment)
            assert owned_row(c.remote, rows) is not None
        if c.op == "create":
            # Never next to a record we do not own.
            assert all(owned_row(r, rows) is not None for r in recs if r.name == c.name)
    deleted = [c.remote.id for c in changes if c.op == "delete" and c.remote is not None]
    assert len(deleted) == len(set(deleted))


# --- pool state machine --------------------------------------------------------------------------


def test_pool_transition_table() -> None:
    for current in PoolState:
        for target in PoolState:
            if current == target:
                assert check_pool_transition(current, target, zone_status="active", dns_edit=True) is False
            elif target in ALLOWED[current]:
                assert check_pool_transition(current, target, zone_status="active", dns_edit=True)
            else:
                with pytest.raises(PoolTransitionError) as err:
                    check_pool_transition(current, target, zone_status="active", dns_edit=True)
                assert err.value.code == "pool_transition_invalid"
    # filtered -> active needs a stop at standby; retired is never directly reactivated.
    assert PoolState.ACTIVE not in ALLOWED[PoolState.FILTERED]
    assert PoolState.ACTIVE not in ALLOWED[PoolState.RETIRED]
    for status, edit in (("pending", True), ("gone", True), ("active", False)):
        with pytest.raises(PoolTransitionError) as err:
            check_pool_transition(PoolState.STANDBY, PoolState.ACTIVE, zone_status=status, dns_edit=edit)
        assert err.value.code == "zone_not_ready"
    assert check_pool_transition(PoolState.NONE, PoolState.ACTIVE, zone_status="active", dns_edit=None)


# --- database-backed ---------------------------------------------------------------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_net_tables(eng)
    yield eng
    await clean_net_tables(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


async def add_zone(
    engine: AsyncEngine, zone_id: str, name: str, *, status: str = "active", pool: str = "none"
) -> None:
    zone = parse_zone(cf_zone_json(zone_id, name, status))
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cf_zones (id, name, status, pool_state, dns_edit, seen_at) "
                "VALUES (:id, :name, :status, :pool, true, now())"
            ),
            {"id": zone.id, "name": zone.name, "status": zone.status, "pool": pool},
        )


@pytest.mark.db
async def test_database_allows_one_active_zone_only(engine: AsyncEngine) -> None:
    await add_zone(engine, ZONE_ID, "example.com", pool="active")
    await add_zone(engine, ZONE_ID_2, "example.net")
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE cf_zones SET pool_state = 'active' WHERE id = :id"), {"id": ZONE_ID_2}
            )


@pytest.mark.db
async def test_set_pool_state_moves_the_old_active_to_standby(engine: AsyncEngine) -> None:
    await add_zone(engine, ZONE_ID, "example.com", pool="active")
    await add_zone(engine, ZONE_ID_2, "example.net", pool="standby")
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        status = await service.set_pool_state(
            session, ZONE_ID_2, PoolState.ACTIVE, actor="admin:owner", now=datetime.now(UTC)
        )
    pools = {z["name"]: z["pool_state"] for z in status["zones"]}
    assert pools == {"example.com": "standby", "example.net": "active"}
    log = [(a["action"], a["target"], a["reasons"]["context"]) for a in await actions(engine)]
    assert ("cloudflare_pool_change", "example.com", {"from": "active", "to": "standby"}) in log
    assert ("cloudflare_pool_change", "example.net", {"from": "standby", "to": "active"}) in log
    async with sm() as session:
        with pytest.raises(service.CfServiceError) as err:
            await service.set_pool_state(
                session, ZONE_ID_2, PoolState.RETIRED, actor="a", now=datetime.now(UTC)
            )
            await service.set_pool_state(
                session, ZONE_ID_2, PoolState.ACTIVE, actor="a", now=datetime.now(UTC)
            )
        assert err.value.code == "pool_transition_invalid"
    async with sm() as session:
        with pytest.raises(service.CfServiceError) as err:
            await service.set_pool_state(
                session, "f" * 32, PoolState.ACTIVE, actor="a", now=datetime.now(UTC)
            )
        assert err.value.code == "not_found"


@pytest.mark.db
async def test_zones_not_listed_anymore_become_gone_but_keep_their_pool_state(engine: AsyncEngine) -> None:
    await add_zone(engine, ZONE_ID, "example.com", pool="active")
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        gone = await service.store_zones(conn, [parse_zone(cf_zone_json(ZONE_ID_2, "example.net"))], {}, now)
    assert gone == ["example.com"]
    async with engine.connect() as conn:
        status = await service.status(conn)
        assert await service.active_zone(conn) is None  # a gone zone is never used
    zones = {z["name"]: (z["status"], z["pool_state"]) for z in status["zones"]}
    assert zones == {"example.com": ("gone", "active"), "example.net": ("active", "none")}


@pytest.mark.db
async def test_desired_slot_records_prefer_ready_and_hold_through_unhealthy(engine: AsyncEngine) -> None:
    t0 = datetime.now(UTC) - timedelta(hours=5)
    await make_node(engine, state="ready", slot=1, ipv4="203.0.113.1", ready_at=t0)
    await make_node(engine, state="ready", slot=1, ipv4="203.0.113.11", ready_at=t0 + timedelta(hours=1))
    await make_node(engine, state="unhealthy", slot=2, ipv4="203.0.113.2", ready_at=t0)
    await make_node(engine, state="burned", slot=3, ipv4="203.0.113.3", ready_at=t0)
    await make_node(engine, state="ready", slot=3, ipv4="203.0.113.33", ready_at=t0 - timedelta(hours=9))
    await make_node(engine, state="retiring", slot=4, ipv4="203.0.113.4", ready_at=t0)
    await make_node(engine, state="ready", slot=None, ipv4="203.0.113.5", ready_at=t0)
    await make_node(engine, state="ready", slot=6, ipv4=None, ready_at=t0)
    await make_node(engine, state="ready", slot=7, ipv4="2001:db8::7", ready_at=t0)
    async with engine.connect() as conn:
        desired = await service.desired_slot_records(conn, ZONE)
    assert [(d.name, d.content) for d in desired] == [
        ("slot1.example.com", "203.0.113.11"),  # newest READY node of the slot
        ("slot2.example.com", "203.0.113.2"),  # unhealthy still holds the name
        ("slot3.example.com", "203.0.113.33"),  # READY beats a burned node
    ]
    assert all(d.ttl == 60 and d.proxied is False and d.type == "A" for d in desired)


@pytest.mark.db
async def test_dns_reconciler_is_a_safe_no_op_today(engine: AsyncEngine, vault: Vault) -> None:
    """No token: nothing happens. Token + verified + active zone but no slot node: nothing is read
    or changed. Any Cloudflare call would go to a closed local port and leave an error behind."""
    closed = lambda token: CloudflareClient(token, base_url="http://127.0.0.1:9")  # noqa: E731
    rec = DnsReconciler(vault_factory=lambda: vault, client_factory=closed)
    async with engine.begin() as conn:
        await rec.run_once(conn)
    assert await actions(engine) == []
    token = secrets.token_urlsafe(30)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await vault.put(conn, service.TOKEN_SECRET, token)
        await service.save_state(conn, now, verified=True, checked_at=now, zones_refreshed_at=now, missing=[])
    await add_zone(engine, ZONE_ID, "example.com", pool="active")
    await make_node(engine, state="ready", slot=None, ipv4="203.0.113.5")
    async with engine.begin() as conn:
        await rec.run_once(conn)
        state = await service.load_state(conn)
    assert state is not None and state.error is None and state.verified
    assert await actions(engine) == []
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM cf_records"))).scalar_one() == 0


@pytest.mark.db
async def test_dns_reconciler_with_a_slot_node_and_unreachable_api_changes_nothing(
    engine: AsyncEngine, vault: Vault
) -> None:
    closed = lambda token: CloudflareClient(token, base_url="http://127.0.0.1:9")  # noqa: E731
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await vault.put(conn, service.TOKEN_SECRET, secrets.token_urlsafe(30))
        await service.save_state(conn, now, verified=True, checked_at=now, zones_refreshed_at=now, missing=[])
    await add_zone(engine, ZONE_ID, "example.com", pool="active")
    await make_node(engine, state="ready", slot=1, ipv4="203.0.113.1")
    async with engine.begin() as conn:
        await DnsReconciler(vault_factory=lambda: vault, client_factory=closed).run_once(conn)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM cf_records"))).scalar_one() == 0


# --- token handling and rule 3 -------------------------------------------------------------------


@pytest.mark.db
async def test_stored_token_never_appears_in_status_logs_or_plaintext(
    engine: AsyncEngine, vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    """The storage half of PUT /token, with zones in Cloudflare's real shape (the verification half
    needs a real token: tests/test_net_cloudflare_real.py)."""
    caplog.set_level(logging.DEBUG)
    token = secrets.token_urlsafe(30)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await vault.put(conn, service.TOKEN_SECRET, SecretValue(token))
        await service.store_zones(conn, [parse_zone(cf_zone_json())], {ZONE_ID: True}, now)
        await service.save_state(conn, now, verified=True, checked_at=now, missing=[], token_kind="user")
    async with engine.connect() as conn:
        status = await service.status(conn)
        assert (await vault.get(conn, service.TOKEN_SECRET)).reveal() == token  # type: ignore[union-attr]
    assert status["token_set"] and status["verified"] and token not in repr(status)
    await assert_absent_from_database(engine, token)
    assert token not in caplog.text


async def test_unreachable_cloudflare_error_does_not_carry_the_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    token = secrets.token_urlsafe(30)
    async with CloudflareClient(SecretValue(token), base_url="http://127.0.0.1:9") as cf:
        with pytest.raises(CloudflareError) as err:
            await cf.verify_user_token()
        with pytest.raises(service.CfServiceError) as svc_err:
            await service.check_token(cf)
    assert err.value.kind == "unavailable" and err.value.__cause__ is None and err.value.__context__ is None
    assert svc_err.value.code == "cf_unavailable"
    assert token not in str(err.value) and token not in repr(err.value) and token not in caplog.text
    assert token not in repr(CloudflareClient(SecretValue(token)))


# --- API ---------------------------------------------------------------------------------------


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
async def test_every_cloudflare_route_requires_admin(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_cloudflare
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_cloudflare.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            found.add((method, route.path))
            assert require_admin in {d.call for d in route.dependant.dependencies}
    assert found == CF_PATHS
    app.dependency_overrides.pop(require_admin, None)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(CF_PATHS):
                r = await c.request(method, path, json={})
                assert r.status_code == 401, (method, path, r.status_code)
                assert r.json()["error"] == "unauthenticated"
                if method != "GET":  # CSRF is checked first, by require_admin itself
                    r = await c.request(method, path, json={}, headers={"X-Requested-With": "x"})
                    assert r.status_code == 403 and r.json()["error"] == "csrf"


@pytest.mark.db
@pytest.mark.redis
async def test_status_contract_shape_when_nothing_is_set(client: AsyncClient) -> None:
    r = await client.get("/api/cloudflare/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "token_set": False,
        "verified": False,
        "checked_at": None,
        "error": None,
        "zones": [],
        "records": [],
    }


@pytest.mark.db
@pytest.mark.redis
async def test_put_token_rejects_malformed_tokens_without_echo(
    client: AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bad = "not a token " + secrets.token_hex(12)
    r = await client.put("/api/cloudflare/token", json={"token": bad})
    assert r.status_code == 400 and r.json()["error"] == "cf_token_invalid" and r.json()["message_fa"]
    assert bad not in r.text and bad not in caplog.text
    # Validation errors never echo the body either.
    r = await client.put("/api/cloudflare/token", json={"token": bad, "extra": 1})
    assert r.status_code == 422 and bad not in r.text
    assert (await client.get("/api/cloudflare/status")).json()["token_set"] is False
    log = await actions(engine)
    assert [(a["action"], a["allowed"], a["reasons"]["reasons"]) for a in log] == [
        ("cloudflare_token_set", False, ["cf_token_invalid"])
    ]
    assert bad not in repr(log)
    await assert_absent_from_database(engine, bad)


@pytest.mark.db
@pytest.mark.redis
async def test_put_pool_over_http(client: AsyncClient, engine: AsyncEngine) -> None:
    await add_zone(engine, ZONE_ID, "example.com")
    await add_zone(engine, ZONE_ID_2, "example.net", status="pending")
    r = await client.put("/api/cloudflare/pool", json={"zone_id": ZONE_ID, "pool_state": "active"})
    assert r.status_code == 200
    body = r.json()
    assert {z["name"]: z["pool_state"] for z in body["zones"]} == {
        "example.com": "active",
        "example.net": "none",
    }
    assert set(body) == {"token_set", "verified", "checked_at", "error", "zones", "records"}
    assert set(body["zones"][0]) == {"id", "name", "status", "pool_state", "dns_edit"}
    r = await client.put("/api/cloudflare/pool", json={"zone_id": ZONE_ID_2, "pool_state": "active"})
    assert r.status_code == 409 and r.json()["error"] == "zone_not_ready"
    r = await client.put("/api/cloudflare/pool", json={"zone_id": "e" * 32, "pool_state": "standby"})
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    r = await client.put("/api/cloudflare/pool", json={"zone_id": ZONE_ID, "pool_state": "burned"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    r = await client.put("/api/cloudflare/pool", json={"zone_id": ZONE_ID, "pool_state": "retired"})
    assert r.status_code == 200
    r = await client.put("/api/cloudflare/pool", json={"zone_id": ZONE_ID, "pool_state": "active"})
    assert r.status_code == 409 and r.json()["error"] == "pool_transition_invalid"


@pytest.mark.db
@pytest.mark.redis
async def test_status_lists_only_our_synced_records(client: AsyncClient, engine: AsyncEngine) -> None:
    await add_zone(engine, ZONE_ID, "example.com", pool="active")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cf_records (id, zone_id, cf_record_id, name, type, content, ttl, proxied, "
                "purpose) VALUES (:a, :z, :cf, 'slot1.example.com', 'A', '203.0.113.1', 60, false, 'slot'), "
                "(:b, :z, NULL, 'slot2.example.com', 'A', '203.0.113.2', 60, false, 'slot')"
            ),
            {"a": uuid.uuid4(), "b": uuid.uuid4(), "z": ZONE_ID, "cf": secrets.token_hex(16)},
        )
    records = (await client.get("/api/cloudflare/status")).json()["records"]
    assert len(records) == 1
    rec = records[0]
    assert {k: rec[k] for k in ("name", "type", "content", "ttl", "proxied", "managed")} == {
        "name": "slot1.example.com",
        "type": "A",
        "content": "203.0.113.1",
        "ttl": 60,
        "proxied": False,
        "managed": True,
    }
    assert rec["updated_at"].endswith("Z")


@pytest.mark.db
async def test_apply_plan_local_steps_refusals_and_crash_safe_create(engine: AsyncEngine) -> None:
    """Everything in apply_plan that needs no successful Cloudflare call, on the real database. The
    client points at a closed local port: any call that did happen would fail as 'unavailable'."""
    await add_zone(engine, ZONE_ID, "example.com", pool="active")
    ours_rec, _ = ours("slot1.example.com", "203.0.113.1")
    pending_ref = uuid.uuid4()
    adopt_rec = remote("slot2.example.com", "203.0.113.2", comment=managed_comment(pending_ref))
    gone_id = secrets.token_hex(16)
    foreign = remote("slot5.example.com", "192.0.2.5", comment="owner's record")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cf_records (id, zone_id, cf_record_id, name, type, content, ttl, proxied, "
                "purpose) VALUES "
                "(:r1, :z, :c1, 'slot1.example.com', 'A', '198.51.100.1', 60, false, 'slot'), "
                "(:r2, :z, NULL, 'slot2.example.com', 'A', '203.0.113.2', 60, false, 'slot'), "
                "(:r3, :z, :c3, 'slot3.example.com', 'A', '203.0.113.3', 60, false, 'slot')"
            ),
            {
                "r1": comment_ref(ours_rec.comment),
                "r2": pending_ref,
                "r3": uuid.uuid4(),
                "z": ZONE_ID,
                "c1": ours_rec.id,
                "c3": gone_id,
            },
        )
    async with engine.connect() as conn:
        rows = await service.owned_rows(conn, ZONE_ID, "slot")
    desired = [
        Desired("slot1.example.com", "A", "203.0.113.1"),
        Desired("slot2.example.com", "A", "203.0.113.2"),
        Desired("slot5.example.com", "A", "203.0.113.5"),
    ]
    changes = plan_zone(ZONE_ID, desired, [ours_rec, adopt_rec, foreign], rows)
    assert sorted((c.op, c.name) for c in changes) == [
        ("adopt", "slot2.example.com"),
        ("conflict", "slot5.example.com"),
        ("forget", "slot3.example.com"),
        ("sync", "slot1.example.com"),
    ]
    known: set[tuple[str, str, str]] = set()
    closed = "http://127.0.0.1:9"
    async with CloudflareClient(SecretValue(secrets.token_urlsafe(30)), base_url=closed) as cf:

        async def apply(steps: list[Change]) -> service.ApplyReport:
            return await service.apply_plan(
                engine, cf, steps, rows, purpose="slot", actor="dns", max_api_calls=5, known_conflicts=known
            )

        report = await apply(changes)
        assert report.api_calls == 0 and report.failed == [] and len(report.conflicts) == 1
        await apply(changes)
        # A delete of a record we do not own is refused before any call (defence in depth).
        forged = Change("delete", ZONE_ID, foreign.name, "A", remote=foreign, row=rows[0])
        assert (await apply([forged])).failed == []
        # A create whose call fails leaves its row behind, committed BEFORE the call.
        want = Desired("slot4.example.com", "A", "203.0.113.4")
        failed = await apply([Change("create", ZONE_ID, want.name, "A", desired=want)])
        assert failed.failed == ["create:slot4.example.com"] and failed.stopped
    async with engine.connect() as conn:
        db_rows = {
            r.name: (r.cf_record_id, r.content)
            for r in (await conn.execute(text("SELECT name, cf_record_id, content FROM cf_records"))).all()
        }
    assert db_rows == {
        "slot1.example.com": (ours_rec.id, "203.0.113.1"),  # synced from Cloudflare
        "slot2.example.com": (adopt_rec.id, "203.0.113.2"),  # adopted by its ref
        "slot4.example.com": (None, "203.0.113.4"),  # pending: the next run adopts or retries it
    }
    log = [(a["action"], a["allowed"], a["result"]) for a in await actions(engine)]
    assert [x[0] for x in log].count("dns_conflict") == 1  # logged once, not every minute
    assert ("dns_delete", False, "refused: not a record this panel manages") in log
    assert ("dns_create", True, "failed: unavailable") in log
    assert {"dns_sync", "dns_adopt", "dns_forget"} <= {x[0] for x in log}
