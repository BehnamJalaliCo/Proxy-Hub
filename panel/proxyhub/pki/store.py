"""The CA certificate in the database: created on first use, deterministically, from the CA key."""

from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.db.models_agent import AgentCaCertificate
from proxyhub.pki.ca import build_ca_certificate, certificate_matches_key, spki_sha256, to_pem


class CaMismatchError(RuntimeError):
    """The stored CA certificate does not belong to the CA key (database or key file tampered)."""


async def ensure_ca_certificate(session: AsyncSession, key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
    """Return the CA certificate for ``key``, creating and storing it on first use.

    Concurrent first uses are harmless: every caller builds the same bytes and the insert ignores
    a row that already exists. The caller commits.
    """
    spki = spki_sha256(key.public_key())
    pem = (
        await session.execute(
            select(AgentCaCertificate.cert_pem).where(AgentCaCertificate.spki_sha256 == spki)
        )
    ).scalar_one_or_none()
    if pem is None:
        cert = build_ca_certificate(key)
        await session.execute(
            insert(AgentCaCertificate)
            .values(spki_sha256=spki, cert_pem=to_pem(cert), not_after=cert.not_valid_after_utc)
            .on_conflict_do_nothing(index_elements=["spki_sha256"])
        )
        return cert
    cert = x509.load_pem_x509_certificate(pem.encode("ascii"))
    if not certificate_matches_key(cert, key):
        raise CaMismatchError("stored agent CA certificate does not match the CA key")
    return cert
