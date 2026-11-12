"""The ACME loop's bookkeeping on a real PostgreSQL, without a CA: which certificates are forgotten
when a domain has more than one zone row, and what a failing challenge cleanup may not change.
The order itself runs against Pebble in tests/test_acme_real.py; here the CA's side is a stand-in
that answers at once, so the database steps around it are what is tested."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_reality_support import World
from test_wstls_support import Cert, make_cert, store_cert, ws_world_fixture  # noqa: F401

from proxyhub.acme.protocol import AcmeError, Challenge, Issued
from proxyhub.acme.service import AcmeOptions, order_certificate
from proxyhub.acme.store import cert_row, chain_secret, key_secret
from proxyhub.cloudflare import TOKEN_SECRET
from proxyhub.cloudflare.client import CloudflareClient
from proxyhub.reality.settings import RealitySettings, save_reality_settings
from proxyhub.reconcilers.acme import AcmeReconciler
from proxyhub.vault import SecretValue

pytestmark = pytest.mark.db

TWIN = "rtzone-twin.example"


@dataclass
class _Order:
    challenges: list[Challenge]


class _Ca:
    """Stands in for the CA's answers: every order has one DNS-01 challenge and is issued at once
    (or fails with ``error``)."""

    def __init__(self, cert: Cert, error: Exception | None = None) -> None:
        self.cert, self.error = cert, error
        self.orders: list[str] = []

    def __call__(self, directory_url: str, key: SecretValue, **kwargs: Any) -> _Ca:
        return self

    def register(self) -> str:
        return "https://acme.invalid/acct/1"

    def new_order(self, domain: str) -> _Order:
        self.orders.append(domain)
        return _Order([Challenge(domain, f"_acme-challenge.{domain}", "token-value")])

    def finalize(self, order: _Order, timeout: timedelta) -> Issued:
        if self.error is not None:
            raise self.error
        c = self.cert
        return Issued(c.chain_pem, c.key_pem, c.not_before, c.not_after, c.serial_hex, c.fingerprint_sha256)

    def close(self) -> None:
        return None


class _Dns:
    """Challenge records that are always published; ``clear`` may fail after its work is done."""

    def __init__(self, clear_error: Exception | None = None) -> None:
        self.clear_error = clear_error
        self.published: list[str] = []
        self.cleared = 0

    async def publish(self, challenges: Sequence[Challenge]) -> None:
        self.published.extend(c.record_name for c in challenges)

    async def clear(self) -> bool:
        self.cleared += 1
        if self.clear_error is not None:
            raise self.clear_error
        return True


def _options() -> AcmeOptions:
    # No resolver to wait for: the stand-in's records count as seen at once.
    return AcmeOptions(directory_url="https://acme.invalid/directory", resolvers=())


@pytest.fixture(name="acme_world")
async def acme_world_fixture(ws_world: World) -> AsyncIterator[World]:
    async with ws_world.engine.begin() as conn:
        await ws_world.vault.put(conn, TOKEN_SECRET, SecretValue("cf-token-for-this-test-only-0000000000"))
        await conn.exec_driver_sql(
            "INSERT INTO cf_state (id, verified, missing) VALUES (1, true, '[]') "
            "ON CONFLICT (id) DO UPDATE SET verified = true"
        )
    try:
        yield ws_world
    finally:
        await ws_world.rows("DELETE FROM secrets WHERE name = :n", n=TOKEN_SECRET)
        await ws_world.rows("DELETE FROM cf_state")


async def _zone_row(world: World, name: str, status: str, pool_state: str) -> str:
    zone_id = uuid.uuid4().hex
    await world.rows(
        "INSERT INTO cf_zones (id, name, status, pool_state, seen_at) VALUES (:id, :n, :s, :p, now())",
        id=zone_id,
        n=name,
        s=status,
        p=pool_state,
    )
    return zone_id


def _loop(world: World, ca: _Ca, dns: _Dns) -> AcmeReconciler:
    return AcmeReconciler(
        vault_factory=lambda: world.vault,
        # Cloudflare is never called: no challenge record of ours is left in any zone.
        client_factory=lambda token: CloudflareClient(token, base_url="http://127.0.0.1:9"),
        options=_options,
        session_factory=ca,
        dns_factory=lambda engine, cf, zid: dns,
    )


async def _forgotten(world: World) -> list[Any]:
    return await world.rows(
        "SELECT target, reasons FROM actions_log WHERE action = 'acme_certificate_forgotten' ORDER BY id"
    )


@pytest.mark.parametrize("gone_first", [True, False])
async def test_a_re_added_domain_keeps_its_certificate(acme_world: World, gone_first: bool) -> None:
    """The domain left Cloudflare once (its old zone row is gone and deleted) and came back with a
    new zone id: the certificate stays, whichever row the database returns last."""
    world = acme_world
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(transport="reality"))  # nothing is ordered
    rows = [("gone", "deleted"), ("active", "active")]
    ids = [await _zone_row(world, TWIN, s, p) for s, p in (rows if gone_first else rows[::-1])]
    live = ids[1] if gone_first else ids[0]
    await store_cert(world, TWIN, live, make_cert(f"*.{TWIN}"))
    async with world.engine.begin() as conn:
        await _loop(world, _Ca(make_cert(f"*.{TWIN}")), _Dns()).run_once(conn)
    async with world.engine.connect() as conn:
        assert await cert_row(conn, TWIN) is not None
        assert await world.vault.get(conn, chain_secret(TWIN)) is not None
        assert await world.vault.get(conn, key_secret(TWIN)) is not None
    assert await _forgotten(world) == []


async def test_a_domain_whose_every_row_left_is_forgotten(acme_world: World) -> None:
    world = acme_world
    async with world.engine.begin() as conn:
        await save_reality_settings(conn, RealitySettings(transport="reality"))
    zone_id = await _zone_row(world, TWIN, "gone", "standby")
    await _zone_row(world, TWIN, "active", "deleted")
    await store_cert(world, TWIN, zone_id, make_cert(f"*.{TWIN}"))
    async with world.engine.begin() as conn:
        await _loop(world, _Ca(make_cert(f"*.{TWIN}")), _Dns()).run_once(conn)
    async with world.engine.connect() as conn:
        assert await cert_row(conn, TWIN) is None
        assert await world.vault.get(conn, key_secret(TWIN)) is None
    [log] = await _forgotten(world)
    assert (log.target, log.reasons["reasons"]) == (f"*.{TWIN}", ["pool_deleted"])


@pytest.mark.parametrize("gone_first", [True, False])
async def test_a_renewal_of_a_re_added_domain_completes(acme_world: World, gone_first: bool) -> None:
    """ws_tls: the certificate of the re-added active domain is due for renewal. The run renews it
    and returns (it used to delete the row in its own open transaction and then wait forever on it
    in the order's first write)."""
    world = acme_world
    rows = [("gone", "deleted"), ("active", "active")]
    ids = [await _zone_row(world, TWIN, s, p) for s, p in (rows if gone_first else rows[::-1])]
    live = ids[1] if gone_first else ids[0]
    old = make_cert(f"*.{TWIN}", valid_for=timedelta(days=20))
    await store_cert(world, TWIN, live, old)
    ca, dns = _Ca(make_cert(f"*.{TWIN}")), _Dns()
    async with world.engine.begin() as conn:
        await asyncio.wait_for(_loop(world, ca, dns).run_once(conn), timeout=30)
    async with world.engine.connect() as conn:
        row = await cert_row(conn, TWIN)
    assert ca.orders == [TWIN] and dns.cleared == 1
    assert row is not None and row.serial_hex == ca.cert.serial_hex and row.zone_id == live
    assert await _forgotten(world) == []


async def test_a_failing_cleanup_keeps_the_issued_certificate(acme_world: World) -> None:
    world = acme_world
    zone_id = await _zone_row(world, "rtzone-clean.example", "active", "active")
    ca, dns = _Ca(make_cert("*.rtzone-clean.example")), _Dns(clear_error=RuntimeError("pool timeout"))
    outcome = await order_certificate(
        world.engine,
        dns,
        zone_id=zone_id,
        domain="rtzone-clean.example",
        options=_options(),
        vault=world.vault,
        now=datetime.now(UTC),
        session_factory=ca,
    )
    assert outcome.error is None and outcome.issued is not None and dns.cleared == 1
    async with world.engine.connect() as conn:
        row = await cert_row(conn, "rtzone-clean.example")
        key = await world.vault.get(conn, key_secret("rtzone-clean.example"))
    assert row is not None and row.not_after is not None and row.retry_at is None
    assert key is not None and key.reveal() == ca.cert.key_pem.reveal()


async def test_a_failing_cleanup_keeps_the_orders_own_error(acme_world: World) -> None:
    world = acme_world
    zone_id = await _zone_row(world, "rtzone-clean.example", "active", "active")
    ca = _Ca(make_cert("*.rtzone-clean.example"), error=AcmeError("acme_unauthorized"))
    outcome = await order_certificate(
        world.engine,
        _Dns(clear_error=RuntimeError("pool timeout")),
        zone_id=zone_id,
        domain="rtzone-clean.example",
        options=_options(),
        vault=world.vault,
        now=datetime.now(UTC),
        session_factory=ca,
    )
    assert outcome.issued is None and outcome.error == "acme_unauthorized"
