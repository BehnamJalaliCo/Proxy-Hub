"""Against a REAL Hetzner node account (runs only on the panel server; decision D10).

Environment:
  PROXYHUB_HETZNER_TEST_TOKEN_FILE     file (mode 600) with a Read & Write token of a node project
                                       that holds no server we do not manage.
  PROXYHUB_HETZNER_TEST_RO_TOKEN_FILE  optional: a READ-ONLY token of the same project; confirms the
                                       write-access probe (docs: "to be confirmed on the server").
Nothing here creates or deletes a server. The only non-GET request is the write probe, which by
construction cannot change anything (PUT /ssh_keys/0 with an empty name).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import (
    ADMIN,
    CSRF,
    assert_absent_from_database,
    clean_fleet_tables,
    make_vault,
    new_token,
)

from proxyhub.accounts.hetzner_io import (
    HetznerFailure,
    call_hetzner,
    observe_servers,
    probe_write_access,
    read_offers,
    verify_token,
)
from proxyhub.api.deps import require_admin
from proxyhub.api.routes_accounts import get_vault
from proxyhub.config import read_secret_file
from proxyhub.hetzner.client import HetznerReader
from proxyhub.reconcilers.account_guard import AccountGuardReconciler
from proxyhub.reconcilers.pricing import PricingReconciler

pytestmark = pytest.mark.hetzner


def token_from(env: str) -> str:
    path = os.environ.get(env)
    if not path:
        pytest.skip(f"{env} not set")
    return read_secret_file(Path(path))


@pytest.fixture
def token() -> str:
    return token_from("PROXYHUB_HETZNER_TEST_TOKEN_FILE")


def test_live_prices_parse_like_the_reader(token: str) -> None:
    reader = HetznerReader(token)
    ours = {(o.server_type, o.location): o for o in read_offers(reader)}
    theirs = {(o.name, o.location): o for o in reader.server_type_offers()}
    assert ours.keys() == theirs.keys()
    for key, offer in theirs.items():
        assert ours[key].price == offer.price
    # Finding H1 (2026-11-03): CPX02 is orderable in fsn1, nbg1 and hel1.
    for loc in ("fsn1", "nbg1", "hel1"):
        assert ours[("cpx02", loc)].orderable, loc
    # The JSON shape the offline fixtures copy.
    raw = reader.raw.request("GET", "/server_types")
    first = raw["server_types"][0]
    assert {"name", "prices", "locations"} <= set(first)
    assert {"location", "price_hourly", "price_monthly", "included_traffic"} <= set(first["prices"][0])
    assert {"net", "gross"} <= set(first["prices"][0]["price_monthly"])
    assert {"name", "deprecation", "available"} <= set(first["locations"][0])


def test_verify_real_read_write_token(token: str) -> None:
    result = verify_token(token)
    assert result.servers.guard.allowed, "the node project holds a server we do not manage (rule 4)"
    assert result.access == "read_write"
    assert result.offers


def test_read_only_token_is_detected() -> None:
    ro = token_from("PROXYHUB_HETZNER_TEST_RO_TOKEN_FILE")
    assert probe_write_access(HetznerReader(ro)) == "read_only"
    assert verify_token(ro).access == "read_only"


async def test_unknown_token_is_rejected() -> None:
    with pytest.raises(HetznerFailure) as exc:
        await call_hetzner(lambda: observe_servers(HetznerReader(new_token())))
    assert exc.value.kind == "token_invalid"


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_fleet_tables(eng)
    yield eng
    await clean_fleet_tables(eng)
    await eng.dispose()


@pytest.mark.db
@pytest.mark.redis
async def test_add_real_account_end_to_end(
    token: str, engine: AsyncEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from proxyhub.api.app import app, lifespan

    caplog.set_level(logging.DEBUG)
    vault = make_vault(tmp_path)
    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    try:
        async with lifespan(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
                r = await c.post("/api/accounts", json={"name": "real-node-account", "token": f" {token}\n"})
                assert r.status_code == 201, r.text
                assert token not in r.text
                acc = r.json()
                assert acc["state"] == "active" and acc["servers_in_account"] is not None

                bad = await c.post("/api/accounts", json={"name": "bogus", "token": new_token()})
                assert bad.status_code == 400 and bad.json()["error"] == "token_invalid"

                guard = AccountGuardReconciler(vault_factory=lambda: vault)
                pricing = PricingReconciler(vault_factory=lambda: vault)
                assert await guard.try_run_once(engine)
                assert await pricing.try_run_once(engine)
                async with engine.connect() as conn:
                    ok = (
                        await conn.execute(
                            text(
                                "SELECT ok, guard_ok FROM hetzner_account_observations WHERE account_id = :a"
                            ),
                            {"a": acc["id"]},
                        )
                    ).one()
                    offers = (
                        await conn.execute(
                            text("SELECT count(*) FROM hetzner_offers WHERE account_id = :a"),
                            {"a": acc["id"]},
                        )
                    ).scalar_one()
                assert tuple(ok) == (True, True) and offers > 0

                assert (await c.post(f"/api/accounts/{acc['id']}/drain")).status_code == 200
                assert (await c.post(f"/api/accounts/{acc['id']}/retire")).json()["state"] == "retired"
    finally:
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(get_vault, None)
    await assert_absent_from_database(engine, token)
    assert token not in caplog.text
