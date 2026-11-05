"""Shared helpers for the ws_tls tests (no tests here): a throwaway CA and certificates made at run
time with ``cryptography`` (no key material in the repository), node names as the DNS loop leaves
them in ``cf_records``, agents of a given version, and the Reality world with transport ws_tls."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from test_reality_support import World, make_world

from proxyhub.acme.protocol import leaf_facts
from proxyhub.acme.store import save_issued
from proxyhub.vault import SecretValue


@dataclass(frozen=True)
class Cert:
    ca_pem: bytes
    chain_pem: str
    key_pem: SecretValue
    not_before: datetime
    not_after: datetime
    serial_hex: str
    fingerprint_sha256: str


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_cert(*names: str, valid_for: timedelta = timedelta(days=90), start: datetime | None = None) -> Cert:
    """A leaf for ``names`` under a fresh CA; ``chain_pem`` is the leaf and the CA (leaf first)."""
    start = start or datetime.now(UTC) - timedelta(hours=1)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("proxyhub ws_tls test CA"))
        .issuer_name(_name("proxyhub ws_tls test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + valid_for + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(names[0]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + valid_for)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        # Python 3.13's default context verifies strictly (VERIFY_X509_STRICT): key identifiers.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    chain = leaf.public_bytes(serialization.Encoding.PEM).decode() + ca_pem.decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    not_before, not_after, serial, fp = leaf_facts(chain)
    return Cert(ca_pem, chain, SecretValue(key_pem), not_before, not_after, serial, fp)


async def store_cert(world: World, domain: str, zone_id: str, cert: Cert) -> None:
    """The certificate as the ACME loop leaves it: chain and key in the vault, facts in the row."""
    async with world.engine.begin() as conn:
        await save_issued(
            conn,
            world.vault,
            domain=domain,
            zone_id=zone_id,
            chain_pem=cert.chain_pem,
            key_pem=cert.key_pem,
            not_before=cert.not_before,
            not_after=cert.not_after,
            serial_hex=cert.serial_hex,
            fingerprint_sha256=cert.fingerprint_sha256,
            now=datetime.now(UTC),
        )


async def zone(world: World, label: str, pool_state: str = "active") -> tuple[str, str]:
    """A pool zone (active by default: there is at most one); returns (zone id, name)."""
    name = await world.zone(label, pool_state)
    [row] = await world.rows("SELECT id FROM cf_zones WHERE name = :n", n=name)
    return str(row.id), name


async def name_record(world: World, zone_id: str, name: str, ip: str, node_id: uuid.UUID) -> None:
    """A node name the DNS loop created in Cloudflare (row with the record's Cloudflare id)."""
    await world.rows(
        "INSERT INTO cf_records (id, zone_id, cf_record_id, name, type, content, ttl, proxied, purpose, "
        "node_id) VALUES (:id, :z, :cf, :name, 'A', :ip, 60, false, 'slot', :node)",
        id=uuid.uuid4(),
        z=zone_id,
        cf=secrets.token_hex(16),
        name=name,
        ip=ip,
        node=node_id,
    )


async def agent(world: World, node_id: uuid.UUID, version: str = "0.3.0") -> None:
    await world.rows(
        "INSERT INTO agent_nodes (node_id, agent_version, registered_at) VALUES (:n, :v, now()) "
        "ON CONFLICT (node_id) DO UPDATE SET agent_version = :v",
        n=node_id,
        v=version,
    )


async def clean_wstls(world: World) -> None:
    await world.rows(
        "DELETE FROM cf_records WHERE zone_id IN (SELECT id FROM cf_zones WHERE name LIKE 'rtzone-%')"
    )
    await world.rows("DELETE FROM acme_certificates WHERE domain LIKE 'rtzone-%'")
    await world.rows("DELETE FROM secrets WHERE name LIKE 'acme:%'")
    await world.rows("DELETE FROM settings WHERE key = 'acme.state'")
    await world.rows("DELETE FROM actions_log WHERE actor = 'acme'")


@pytest.fixture(name="ws_world")
async def ws_world_fixture(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[World]:
    async for w in make_world(migrated_db, tmp_path, monkeypatch, "ws_tls"):
        await clean_wstls(w)
        try:
            yield w
        finally:
            await clean_wstls(w)
