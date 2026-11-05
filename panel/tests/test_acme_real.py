"""ACME against Pebble, Let's Encrypt's own test CA (decision D10: no imitation of an ACME server).

Pebble (``ghcr.io/letsencrypt/pebble``) runs the RFC 8555 protocol and validates DNS-01 for real
against ``pebble-challtestsrv``, a real DNS server with a DNS-over-HTTPS endpoint and a management
API that holds the TXT records. The panel's own code runs unchanged: account, order, the DoH
propagation check, validation, finalization, the vault, the backoff, the control loop. Only where
the TXT records go differs (the challenge test server instead of Cloudflare, whose path is covered
by the planner tests and the real Cloudflare tests). Both images are pinned by digest and are not
pulled by tests: ``docker pull`` them first (CI does).
"""

from __future__ import annotations

import json
import shutil
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from test_agent_support import free_port, run_tool
from test_fleet_live_support import closed_port
from test_fleet_support import assert_absent_from_database
from test_reality_support import World
from test_wstls_support import make_cert, store_cert, ws_world_fixture, zone  # noqa: F401

from proxyhub.acme.doh import DohResolver
from proxyhub.acme.protocol import Challenge
from proxyhub.acme.service import AcmeOptions, order_certificate
from proxyhub.acme.store import ACCOUNT_KEY_SECRET, cert_row, chain_secret, key_secret, material
from proxyhub.cloudflare import TOKEN_SECRET
from proxyhub.cloudflare.client import CloudflareClient
from proxyhub.reality.settings import RealitySettings, save_reality_settings
from proxyhub.reconcilers.acme import AcmeReconciler
from proxyhub.vault import SecretValue

pytestmark = pytest.mark.db

PEBBLE_DIGEST = "sha256:ddf230642b1a584f519f32e347de1b05a6e4c1f6c35c1863b33effeab5f78199"
CHALLTESTSRV_DIGEST = "sha256:12ce21884def456bcf9786542113949e1f19dc7738d2c70e156c2d0c38a1405b"
PEBBLE = f"ghcr.io/letsencrypt/pebble@{PEBBLE_DIGEST}"
CHALLTESTSRV = f"ghcr.io/letsencrypt/pebble-challtestsrv@{CHALLTESTSRV_DIGEST}"


def _have_images() -> bool:
    if shutil.which("docker") is None:
        return False
    return all(
        run_tool("docker", "image", "inspect", image).returncode == 0 for image in (PEBBLE, CHALLTESTSRV)
    )


@dataclass(frozen=True)
class Pebble:
    directory: str
    ca_file: str  # Pebble's HTTPS endpoint (its own test "minica")
    roots: str  # Pebble's management API: the root its certificates chain to
    challtestsrv: str  # management API of the DNS server Pebble validates against
    doh: str
    doh_ctx: ssl.SSLContext

    def options(self, **over: object) -> AcmeOptions:
        base: dict[str, object] = {
            "directory_url": self.directory,
            "ca_file": self.ca_file,
            "resolvers": (self.doh,),
            "doh_verify": self.doh_ctx,
            "propagation_timeout_s": 30.0,
            "propagation_interval_s": 0.5,
            "finalize_timeout": timedelta(seconds=60),
        }
        base.update(over)
        return AcmeOptions(**base)  # type: ignore[arg-type]


def _wait_https(url: str, ca: ssl.SSLContext, what: str) -> None:
    for _ in range(150):
        try:
            httpx.get(url, verify=ca, timeout=2, trust_env=False)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    pytest.fail(f"{what} did not start")


@pytest.fixture(scope="module")
def pebble(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Pebble]:
    if not _have_images():
        pytest.skip("needs Docker and the pinned Pebble and challtestsrv images (docker pull them)")
    work = tmp_path_factory.mktemp("pebble")
    work.chmod(0o755)
    dns, doh, mgmt, acme, pmgmt = (free_port() for _ in range(5))
    # The DoH endpoint's certificate (throwaway, made now) and the context that trusts it.
    doh_cert = make_cert("localhost")
    (work / "doh.pem").write_text(doh_cert.chain_pem)
    (work / "doh.key").write_text(doh_cert.key_pem.reveal())
    (work / "pebble.json").write_text(
        json.dumps(
            {
                "pebble": {
                    "listenAddress": f"127.0.0.1:{acme}",
                    "managementListenAddress": f"127.0.0.1:{pmgmt}",
                    "certificate": "test/certs/localhost/cert.pem",
                    "privateKey": "test/certs/localhost/key.pem",
                    "httpPort": 5002,
                    "tlsPort": 5001,
                    "ocspResponderURL": "",
                    "externalAccountBindingRequired": False,
                    "domainBlocklist": ["blocked-domain.example"],
                    "retryAfter": {"authz": 1, "order": 1},
                    "keyAlgorithm": "ecdsa",
                }
            }
        )
    )
    for f in work.iterdir():
        f.chmod(0o644)  # read by the containers; throwaway test material
    tag = uuid.uuid4().hex[:8]
    srv, ca_name = f"proxyhub-challtestsrv-{tag}", f"proxyhub-pebble-{tag}"
    started = run_tool(
        "docker", "run", "-d", "--rm", "--name", srv, "--network", "host", "-v", f"{work}:/wg:ro",
        CHALLTESTSRV, "-dnsserver", f"127.0.0.1:{dns}", "-doh", f"127.0.0.1:{doh}",
        "-doh-cert", "/wg/doh.pem", "-doh-cert-key", "/wg/doh.key", "-management", f"127.0.0.1:{mgmt}",
        "-http01", "", "-https01", "", "-tlsalpn01", "", "-defaultIPv6", "",
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    try:
        started = run_tool(
            "docker", "run", "-d", "--rm", "--name", ca_name, "--network", "host", "-v", f"{work}:/wg:ro",
            # No random sleeps before validation, no random nonce rejections, no reused authorizations.
            "-e", "PEBBLE_VA_NOSLEEP=1", "-e", "PEBBLE_WFE_NONCEREJECT=0", "-e", "PEBBLE_AUTHZREUSE=0",
            PEBBLE, "-config", "/wg/pebble.json", "-dnsserver", f"127.0.0.1:{dns}",
        )  # fmt: skip
        assert started.returncode == 0, started.stderr
        try:
            minica = work / "pebble.minica.pem"
            copied = run_tool("docker", "cp", f"{ca_name}:/test/certs/pebble.minica.pem", minica)
            assert copied.returncode == 0, copied.stderr
            ctx = ssl.create_default_context(cafile=None, cadata=doh_cert.ca_pem.decode())
            _wait_https(f"https://localhost:{acme}/dir", ssl.create_default_context(cafile=minica), "pebble")
            _wait_https(f"https://localhost:{doh}/dns-query", ctx, "the DoH server")
            yield Pebble(
                directory=f"https://localhost:{acme}/dir",
                ca_file=str(minica),
                roots=f"https://localhost:{pmgmt}/roots/0",
                challtestsrv=f"http://127.0.0.1:{mgmt}",
                doh=f"https://localhost:{doh}/dns-query",
                doh_ctx=ctx,
            )
        finally:
            run_tool("docker", "rm", "-f", ca_name)
    finally:
        run_tool("docker", "rm", "-f", srv)


class ChallTestSrvDns:
    """The challenge records in pebble-challtestsrv: the real DNS server Pebble validates against."""

    def __init__(self, base: str, *, value: str | None = None, publish: bool = True) -> None:
        self.base, self.value, self.do_publish = base, value, publish
        self.published: list[str] = []
        self.cleared = 0

    async def publish(self, challenges: Sequence[Challenge]) -> None:
        if not self.do_publish:
            return
        async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
            for ch in challenges:
                body = {"host": ch.record_name + ".", "value": self.value or ch.value}
                (await c.post(f"{self.base}/set-txt", json=body)).raise_for_status()
                self.published.append(ch.record_name)

    async def clear(self) -> bool:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
            for name in self.published:
                (await c.post(f"{self.base}/clear-txt", json={"host": name + "."})).raise_for_status()
        self.cleared += 1
        return True


@pytest.fixture(name="acme_world")
async def acme_world_fixture(ws_world: World) -> AsyncIterator[World]:
    yield ws_world


async def test_a_wildcard_certificate_is_ordered_over_dns01_and_kept_in_the_vault(
    pebble: Pebble, acme_world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world = acme_world
    caplog.set_level("DEBUG")
    zone_id, domain = await zone(world, "acme")
    dns = ChallTestSrvDns(pebble.challtestsrv)
    now = datetime.now(UTC)
    outcome = await order_certificate(
        world.engine,
        dns,
        zone_id=zone_id,
        domain=domain,
        options=pebble.options(),
        vault=world.vault,
        now=now,
    )
    assert outcome.error is None and outcome.issued is not None, outcome.error
    assert dns.published == [f"_acme-challenge.{domain}"] and dns.cleared == 1
    async with world.engine.connect() as conn:
        row = await cert_row(conn, domain)
        chain = await world.vault.get(conn, chain_secret(domain))
        key = await world.vault.get(conn, key_secret(domain))
        account = await world.vault.get(conn, ACCOUNT_KEY_SECRET)
        served = await material(conn, domain, datetime.now(UTC), world.vault)
    assert row is not None and chain is not None and key is not None and account is not None
    assert row.attempts == 0 and row.last_error is None and row.zone_id == zone_id
    assert served is not None and served.chain_pem == chain.reveal() == outcome.issued.chain_pem
    certs = x509.load_pem_x509_certificates(chain.reveal().encode())
    leaf = certs[0]
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [f"*.{domain}"]
    assert row.not_after == leaf.not_valid_after_utc and row.not_after > now + timedelta(days=30)
    # The key in the vault is the leaf's (ECDSA P-256), and the chain leads to Pebble's root.
    private = load_pem_private_key(key.reveal().encode(), password=None)
    assert isinstance(private, ec.EllipticCurvePrivateKey) and private.curve.name == "secp256r1"
    assert private.public_key().public_numbers() == leaf.public_key().public_numbers()  # type: ignore[union-attr]
    pebble_ctx = ssl.create_default_context(cafile=pebble.ca_file)
    async with httpx.AsyncClient(verify=pebble_ctx, trust_env=False) as client:
        root = x509.load_pem_x509_certificate((await client.get(pebble.roots)).content)
    for child, parent in zip([*certs, root][:-1], [*certs, root][1:], strict=True):
        child.verify_directly_issued_by(parent)
    # Neither key is anywhere in the database in clear, nor in a log line.
    for secret in (key.reveal(), account.reveal()):
        line = secret.splitlines()[1]
        await assert_absent_from_database(world.engine, line)
        assert line not in caplog.text
    [log] = await world.rows("SELECT action, target FROM actions_log WHERE actor = 'acme'")
    assert (log.action, log.target) == ("acme_certificate_issued", f"*.{domain}")
    # The challenge record is gone again from the DNS server.
    async with DohResolver(pebble.doh, verify=pebble.doh_ctx) as r:
        assert (await r.txt(f"_acme-challenge.{domain}")).values == frozenset()

    # A renewal: the same account (no second registration), a new certificate and key.
    [state] = await world.rows("SELECT value FROM settings WHERE key = 'acme.state'")
    again = await order_certificate(
        world.engine,
        ChallTestSrvDns(pebble.challtestsrv),
        zone_id=zone_id,
        domain=domain,
        options=pebble.options(),
        vault=world.vault,
        now=datetime.now(UTC),
    )
    assert again.issued is not None and again.issued.serial_hex != outcome.issued.serial_hex
    assert again.issued.key_pem.reveal() != key.reveal()
    [state2] = await world.rows("SELECT value FROM settings WHERE key = 'acme.state'")
    assert state2.value["accounts"] == state.value["accounts"] and len(state.value["accounts"]) == 1


async def test_failed_orders_are_counted_and_wait_longer_each_time(pebble: Pebble, acme_world: World) -> None:
    world = acme_world
    zone_id, domain = await zone(world, "fail")
    now = datetime.now(UTC)
    # 1. The record never shows up at the resolver: the CA is not even asked to validate.
    silent = ChallTestSrvDns(pebble.challtestsrv, publish=False)
    out = await order_certificate(
        world.engine,
        silent,
        zone_id=zone_id,
        domain=domain,
        options=pebble.options(propagation_timeout_s=2.0),
        vault=world.vault,
        now=now,
    )
    assert out.issued is None and out.error == "dns_not_propagated"
    assert out.retry_at is not None and out.retry_at - now == timedelta(hours=1)
    # 2. A wrong value (and no propagation check): Pebble's real validation refuses it.
    wrong = ChallTestSrvDns(pebble.challtestsrv, value="not-the-key-authorization")
    out = await order_certificate(
        world.engine,
        wrong,
        zone_id=zone_id,
        domain=domain,
        options=pebble.options(resolvers=()),
        vault=world.vault,
        now=now,
    )
    assert out.issued is None and out.error is not None and out.error.startswith("acme_"), out.error
    assert out.retry_at is not None and out.retry_at - now == timedelta(hours=2) and wrong.cleared == 1
    # 3. A directory nobody serves.
    out = await order_certificate(
        world.engine,
        wrong,
        zone_id=zone_id,
        domain=domain,
        options=pebble.options(directory_url=f"https://localhost:{closed_port()}/dir"),
        vault=world.vault,
        now=now,
    )
    assert out.error == "acme_unreachable" and out.retry_at is not None
    async with world.engine.connect() as conn:
        row = await cert_row(conn, domain)
        served = await material(conn, domain, now, world.vault)
    assert row is not None and row.attempts == 3 and row.last_error == "acme_unreachable" and served is None
    actions = await world.rows("SELECT action, reasons FROM actions_log WHERE actor = 'acme' ORDER BY id")
    assert [a.action for a in actions] == ["acme_order_failed"] * 3
    # Without any certificate the owner is told (once a day); the alert time is kept.
    [state] = await world.rows("SELECT value FROM settings WHERE key = 'acme.state'")
    assert "alert_at" in state.value
    # A renewal that fails with weeks left is retried quietly.
    await world.rows("DELETE FROM settings WHERE key = 'acme.state'")
    other_id, other = await zone(world, "quiet", "standby")
    await store_cert(world, other, other_id, make_cert(f"*.{other}"))
    out = await order_certificate(
        world.engine,
        wrong,
        zone_id=other_id,
        domain=other,
        options=pebble.options(directory_url=f"https://localhost:{closed_port()}/dir"),
        vault=world.vault,
        now=now,
    )
    assert out.error == "acme_unreachable"
    quiet = await world.rows("SELECT value->>'alert_at' AS at FROM settings WHERE key = 'acme.state'")
    assert all(r.at is None for r in quiet)
    async with world.engine.connect() as conn:
        assert await material(conn, other, now, world.vault) is not None  # the old one still serves


async def test_the_loop_orders_once_renews_late_and_forgets_a_deleted_domain(
    pebble: Pebble, acme_world: World
) -> None:
    world = acme_world
    zone_id, domain = await zone(world, "loop")
    async with world.engine.begin() as conn:
        await world.vault.put(conn, TOKEN_SECRET, SecretValue("cf-token-for-this-test-only-0000000000"))
        await conn.exec_driver_sql(
            "INSERT INTO cf_state (id, verified, missing) VALUES (1, true, '[]') "
            "ON CONFLICT (id) DO UPDATE SET verified = true"
        )
    dns = ChallTestSrvDns(pebble.challtestsrv)
    loop = AcmeReconciler(
        vault_factory=lambda: world.vault,
        # Cloudflare is never called here (no challenge record of ours is left in any zone).
        client_factory=lambda token: CloudflareClient(token, base_url=f"http://127.0.0.1:{closed_port()}"),
        options=pebble.options,
        dns_factory=lambda engine, cf, zid: dns,
    )
    try:
        async with world.engine.begin() as conn:
            await loop.run_once(conn)
        async with world.engine.connect() as conn:
            first = await cert_row(conn, domain)
        assert first is not None and first.issued_at is not None and dns.published
        async with world.engine.begin() as conn:
            await loop.run_once(conn)  # 89 days left: nothing to do
        async with world.engine.connect() as conn:
            assert (await cert_row(conn, domain)).issued_at == first.issued_at  # type: ignore[union-attr]
        # Fewer than 30 days left: renewed.
        await world.rows(
            "UPDATE acme_certificates SET not_after = now() + interval '29 days' WHERE domain = :d", d=domain
        )
        async with world.engine.begin() as conn:
            await loop.run_once(conn)
        async with world.engine.connect() as conn:
            renewed = await cert_row(conn, domain)
        assert renewed is not None and renewed.serial_hex != first.serial_hex
        # The REALITY transport needs no certificate: nothing is ordered for it.
        async with world.engine.begin() as conn:
            await save_reality_settings(conn, RealitySettings(transport="reality"))
        await world.rows(
            "UPDATE acme_certificates SET not_after = now() + interval '1 day' WHERE domain = :d", d=domain
        )
        async with world.engine.begin() as conn:
            await loop.run_once(conn)
        async with world.engine.connect() as conn:
            assert (await cert_row(conn, domain)).serial_hex == renewed.serial_hex  # type: ignore[union-attr]
        # The domain is deleted from the pool: its certificate and key leave the vault.
        await world.rows("UPDATE cf_zones SET pool_state = 'deleted' WHERE id = :z", z=zone_id)
        async with world.engine.begin() as conn:
            await loop.run_once(conn)
        async with world.engine.connect() as conn:
            assert await cert_row(conn, domain) is None
            assert await world.vault.get(conn, chain_secret(domain)) is None
            assert await world.vault.get(conn, key_secret(domain)) is None
        forgotten = await world.rows(
            "SELECT target FROM actions_log WHERE action = 'acme_certificate_forgotten'"
        )
        assert [f.target for f in forgotten] == [f"*.{domain}"]
    finally:
        await world.rows("DELETE FROM secrets WHERE name = :n", n=TOKEN_SECRET)
        await world.rows("DELETE FROM cf_state")


def test_pebble_images_are_pinned_in_ci() -> None:
    ci = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text()
    for image in (PEBBLE, CHALLTESTSRV):
        assert f"docker pull {image}" in ci, image
