"""Real Cloudflare (decision D10: never imitated).

* ``cloudflare`` tests need a real token for a TEST zone: ``PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE``
  (a 0600 file holding a token with Zone:Read + DNS:Edit) and ``PROXYHUB_CLOUDFLARE_TEST_ZONE``
  (the zone name). They run on the panel server. They create, update and delete only records named
  ``ph-test-<random>`` that this test database owns, and prove that records the panel does not own
  (one plain, one even carrying the marker) are left exactly as they were.
* The random-token test calls api.cloudflare.com without any credential; it is opt-in together
  with the other real public-API calls (``PROXYHUB_GLOBALPING_TESTS=1``).
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF, assert_absent_from_database, make_vault
from test_net_support import actions, clean_net_tables

from proxyhub.api.deps import require_admin
from proxyhub.api.routes_cloudflare import cloudflare_vault
from proxyhub.cloudflare import service
from proxyhub.cloudflare.client import CloudflareClient, DnsRecord, RecordSpec
from proxyhub.cloudflare.records import MARKER, Desired, has_marker, managed_comment
from proxyhub.config import read_secret_file
from proxyhub.vault import SecretValue, Vault

TOKEN_FILE = os.environ.get("PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE")
TEST_ZONE = os.environ.get("PROXYHUB_CLOUDFLARE_TEST_ZONE", "")
READONLY_TOKEN_FILE = os.environ.get("PROXYHUB_CLOUDFLARE_TEST_READONLY_TOKEN_FILE")


def _token() -> SecretValue:
    assert TOKEN_FILE, "PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE"
    path = Path(TOKEN_FILE)
    assert path.stat().st_mode & 0o077 == 0, "the token file must be 0600"
    return SecretValue(read_secret_file(path))


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


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[cloudflare_vault] = lambda: vault
    try:
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(cloudflare_vault, None)


def _find(records: list[DnsRecord], name: str) -> list[DnsRecord]:
    return [r for r in records if r.name == name]


@pytest.mark.cloudflare
@pytest.mark.db
async def test_real_token_check_and_managed_record_lifecycle(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    token = _token()
    tag = secrets.token_hex(4)
    ours_txt = f"ph-test-{tag}-txt.{TEST_ZONE}"
    ours_a = f"ph-test-{tag}-a.{TEST_ZONE}"
    foreign = f"ph-test-{tag}-foreign.{TEST_ZONE}"
    marked_foreign = f"ph-test-{tag}-marked.{TEST_ZONE}"
    created_by_test: list[tuple[str, str]] = []
    async with CloudflareClient(token) as cf:
        check = await service.check_token(cf)
        assert check.missing == [], check.missing
        zone = next(z for z in check.zones if z.name == TEST_ZONE)
        assert check.dns_edit[zone.id] is True
        async with engine.begin() as conn:
            await service.store_zones(conn, check.zones, check.dns_edit, datetime.now(UTC))
        try:
            # Two records this database does not own: a plain one, and one that even carries the marker.
            plain = await cf.create_record(
                zone.id, RecordSpec(foreign, "TXT", '"owner record"', 300, False, "owner's own record")
            )
            created_by_test.append((zone.id, plain.id))
            marked = await cf.create_record(
                zone.id,
                RecordSpec(marked_foreign, "TXT", '"other panel"', 300, False, managed_comment(uuid.uuid4())),
            )
            created_by_test.append((zone.id, marked.id))
            before = {r.id: r for r in await cf.list_dns_records(zone.id) if r.id in (plain.id, marked.id)}

            def want(txt: str, ip: str) -> list[Desired]:
                return [
                    Desired(ours_txt, "TXT", txt, purpose="test"),
                    Desired(ours_a, "A", ip, purpose="test"),
                    Desired(foreign, "TXT", "taken", purpose="test"),  # conflict: never created
                    Desired(marked_foreign, "TXT", "taken", purpose="test"),  # conflict as well
                ]

            known: set[tuple[str, str, str]] = set()
            report = await service.reconcile_zone(
                engine,
                cf,
                zone.id,
                want('"v1"', "192.0.2.1"),
                purpose="test",
                actor="test",
                max_api_calls=10,
                known_conflicts=known,
            )
            assert sorted(report.done) == sorted([f"create:{ours_txt}", f"create:{ours_a}"]), report
            assert {n for _, n, _ in report.conflicts} == {foreign, marked_foreign}
            listed = await cf.list_dns_records(zone.id)
            (txt,) = _find(listed, ours_txt)
            (a_rec,) = _find(listed, ours_a)
            assert has_marker(txt.comment) and has_marker(a_rec.comment)
            assert (a_rec.content, a_rec.ttl, a_rec.proxied) == ("192.0.2.1", 60, False)

            # Running again changes nothing.
            again = await service.reconcile_zone(
                engine,
                cf,
                zone.id,
                want('"v1"', "192.0.2.1"),
                purpose="test",
                actor="test",
                max_api_calls=10,
                known_conflicts=known,
            )
            assert again.done == [] and again.api_calls == 0

            report = await service.reconcile_zone(
                engine,
                cf,
                zone.id,
                want('"v2"', "192.0.2.2"),
                purpose="test",
                actor="test",
                max_api_calls=10,
                known_conflicts=known,
            )
            assert sorted(report.done) == sorted([f"update:{ours_txt}", f"update:{ours_a}"]), report
            (a_rec,) = _find(await cf.list_dns_records(zone.id), ours_a)
            assert a_rec.content == "192.0.2.2"

            report = await service.reconcile_zone(
                engine, cf, zone.id, [], purpose="test", actor="test", max_api_calls=10, known_conflicts=known
            )
            assert sorted(report.done) == sorted([f"delete:{ours_txt}", f"delete:{ours_a}"]), report
            after_list = await cf.list_dns_records(zone.id)
            assert _find(after_list, ours_txt) == [] and _find(after_list, ours_a) == []
            after = {r.id: r for r in after_list if r.id in (plain.id, marked.id)}
            assert after == before, "a record the panel does not own was changed"
        finally:
            for zone_id, record_id in created_by_test:
                await cf.delete_record(zone_id, record_id)  # the test's own setup records
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM cf_records"))).scalar_one() == 0
    log = [(a["action"], a["allowed"]) for a in await actions(engine)]
    for expected in ("dns_create", "dns_update", "dns_delete"):
        assert (expected, True) in log
    assert ("dns_conflict", False) in log
    assert token.reveal() not in caplog.text
    await assert_absent_from_database(engine, token.reveal())


@pytest.mark.cloudflare
@pytest.mark.db
@pytest.mark.redis
async def test_real_put_token_over_the_api(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    token = _token().reveal()
    r = await client.put("/api/cloudflare/token", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_set"] and body["verified"] and body["checked_at"].endswith("Z")
    assert TEST_ZONE in {z["name"] for z in body["zones"]}
    assert token not in r.text and token not in caplog.text
    assert token not in repr(await actions(engine))
    await assert_absent_from_database(engine, token)
    async with engine.connect() as conn:
        assert (await vault.get(conn, service.TOKEN_SECRET)).reveal() == token  # type: ignore[union-attr]


@pytest.mark.cloudflare
async def test_real_token_without_dns_edit_is_reported() -> None:
    if not READONLY_TOKEN_FILE:
        pytest.skip("PROXYHUB_CLOUDFLARE_TEST_READONLY_TOKEN_FILE (a Zone:Read-only token) not set")
    token = SecretValue(read_secret_file(Path(READONLY_TOKEN_FILE)))
    async with CloudflareClient(token) as cf:
        check = await service.check_token(cf)
    assert check.missing == ["DNS:Edit"]


@pytest.mark.globalping
@pytest.mark.db
@pytest.mark.redis
async def test_real_cloudflare_refuses_a_random_token_and_nothing_leaks(
    client: AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    """api.cloudflare.com itself answers (no credential needed): 401 code 1000 on verify, 403 code
    9109 on zones. The token-shaped value never reaches a log, the response, actions_log or the DB."""
    caplog.set_level(logging.DEBUG)
    token = secrets.token_urlsafe(30)[:40]
    r = await client.put("/api/cloudflare/token", json={"token": token})
    assert r.status_code == 400 and r.json()["error"] == "cf_token_invalid", r.text
    assert "api.cloudflare.com" in caplog.text  # the real API was called
    assert token not in r.text and token not in caplog.text
    assert MARKER not in caplog.text
    log = await actions(engine)
    assert [(a["action"], a["allowed"], a["reasons"]["reasons"]) for a in log] == [
        ("cloudflare_token_set", False, ["cf_token_invalid"])
    ]
    assert token not in repr(log)
    await assert_absent_from_database(engine, token)
    assert (await client.get("/api/cloudflare/status")).json()["token_set"] is False
