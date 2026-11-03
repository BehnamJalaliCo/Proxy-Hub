"""Signing node agent CSRs into short-lived client certificates (docs/10 section 3.5).

The node generates its own EC P-256 key and sends only a CSR; the private key never leaves the
node. Whatever the CSR asks for, the certificate content is decided here: CN = node id, one SAN URI
``urn:proxyhub:node:<id>``, client-auth only, 30 days, renewed at two thirds of its lifetime.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

LEAF_LIFETIME = timedelta(days=30)
# Accept a node whose clock is a little behind the panel's.
CLOCK_SKEW = timedelta(minutes=5)
NODE_URI_PREFIX = "urn:proxyhub:node:"
LEAF_ORGANIZATION = "Proxy-Hub node"
MAX_CSR_PEM_CHARS = 4096


class CsrError(ValueError):
    """A CSR was refused. ``code`` is a short machine-readable reason (safe to return and log)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class IssuedCertificate:
    cert: x509.Certificate
    pem: str
    fingerprint_sha256: str
    serial_hex: str
    not_before: datetime
    not_after: datetime


def node_uri(node_id: uuid.UUID) -> str:
    return f"{NODE_URI_PREFIX}{node_id}"


def fingerprint_sha256(cert: x509.Certificate) -> str:
    """Hex SHA-256 of the DER certificate: the same value Caddy exposes as
    ``{http.request.tls.client.fingerprint}`` for a verified client certificate."""
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def renewal_time(not_before: datetime, not_after: datetime) -> datetime:
    """When a certificate should be replaced: two thirds of the way through its validity."""
    return not_before + (not_after - not_before) * 2 / 3


def parse_csr(csr_pem: str, node_id: uuid.UUID) -> x509.CertificateSigningRequest:
    """Parse and check a node CSR: valid self-signature, EC P-256 key, CN equal to the node id."""
    if not isinstance(csr_pem, str) or len(csr_pem) > MAX_CSR_PEM_CHARS:
        raise CsrError("csr_too_large")
    try:
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise CsrError("csr_unparseable") from None
    if not csr.is_signature_valid:
        raise CsrError("csr_bad_signature")
    key = csr.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise CsrError("csr_key_not_p256")
    common_names = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if len(common_names) != 1 or common_names[0].value != str(node_id):
        raise CsrError("csr_wrong_node")
    return csr


def sign_node_csr(
    csr: x509.CertificateSigningRequest,
    *,
    node_id: uuid.UUID,
    ca_key: ec.EllipticCurvePrivateKey,
    ca_cert: x509.Certificate,
    now: datetime,
    lifetime: timedelta | None = None,
) -> IssuedCertificate:
    public_key = csr.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise CsrError("csr_key_not_p256")
    not_before = now - CLOCK_SKEW
    not_after = now + (lifetime if lifetime is not None else LEAF_LIFETIME)
    if not_after > ca_cert.not_valid_after_utc:
        raise CsrError("ca_expires_too_soon")
    builder = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, LEAF_ORGANIZATION),
                    x509.NameAttribute(NameOID.COMMON_NAME, str(node_id)),
                ]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(node_uri(node_id))]), critical=False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
    )
    cert = builder.sign(ca_key, hashes.SHA256())
    return IssuedCertificate(
        cert=cert,
        pem=cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        fingerprint_sha256=fingerprint_sha256(cert),
        serial_hex=format(cert.serial_number, "x"),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
    )


def node_id_from_certificate(cert: x509.Certificate) -> uuid.UUID:
    """The node id a certificate we issued speaks for (from its SAN URI)."""
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    uris = [
        u for u in san.get_values_for_type(x509.UniformResourceIdentifier) if u.startswith(NODE_URI_PREFIX)
    ]
    if len(uris) != 1:
        raise ValueError("certificate does not name exactly one node")
    return uuid.UUID(uris[0][len(NODE_URI_PREFIX) :])
