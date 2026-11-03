"""The panel's own certificate authority for node agents (docs/10 section 3.5, M2.1).

The CA private key is a secret file (``config.agent_ca_key_file``, EC P-256, created by
deploy/ensure_secrets.sh). The CA *certificate* is derived from that key **deterministically**:
fixed subject, fixed validity window, a serial computed from the public key, and an RFC 6979
deterministic ECDSA signature. The same key therefore always yields the byte-identical certificate,
so the copy stored in the database, the copy Caddy trusts for mTLS and the copy baked into every
node's cloud-init can never drift apart, and a lost database row is rebuilt exactly.

Nothing here logs or returns key material; error messages name files, never their content.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from proxyhub.config import read_secret_file

CA_COMMON_NAME = "Proxy-Hub node agent CA"
CA_ORGANIZATION = "Proxy-Hub"
# Fixed so the certificate is reproducible from the key alone (rebuilt, never re-dated).
CA_NOT_BEFORE = datetime(2026, 1, 1, tzinfo=UTC)
CA_NOT_AFTER = datetime(2046, 1, 1, tzinfo=UTC)
_SERIAL_DOMAIN = b"proxyhub/agent-ca/serial/v1"


class CaKeyError(RuntimeError):
    """The CA key file is unusable (missing, unreadable, wrong algorithm). Never carries key bytes."""


def spki_sha256(public_key: ec.EllipticCurvePublicKey) -> str:
    """SHA-256 (hex) of the DER SubjectPublicKeyInfo: a stable public identifier of a key."""
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def load_ca_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """Load the CA private key (PEM, EC P-256) from its secret file."""
    try:
        pem = read_secret_file(path)
    except Exception as exc:  # SecretFileError, PermissionError, UnicodeDecodeError ...
        raise CaKeyError(f"agent CA key unreadable: {path} ({type(exc).__name__})") from None
    try:
        key = serialization.load_pem_private_key(pem.encode("ascii") + b"\n", password=None)
    except (ValueError, TypeError, UnicodeEncodeError):
        raise CaKeyError(f"agent CA key is not an unencrypted PEM private key: {path}") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise CaKeyError(f"agent CA key must be EC P-256: {path}")
    return key


def ca_serial(public_key: ec.EllipticCurvePublicKey) -> int:
    """Positive serial (< 2**152, RFC 5280 limit is 20 octets) derived from the public key."""
    digest = hashlib.sha256(_SERIAL_DOMAIN + bytes.fromhex(spki_sha256(public_key))).digest()
    return int.from_bytes(digest[:19], "big") | 1


def ca_subject() -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, CA_ORGANIZATION),
            x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME),
        ]
    )


def build_ca_certificate(key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
    """Self-signed CA certificate, byte-identical for the same key (deterministic ECDSA)."""
    public_key = key.public_key()
    name = ca_subject()
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(ca_serial(public_key))
        .not_valid_before(CA_NOT_BEFORE)
        .not_valid_after(CA_NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )
    return builder.sign(key, hashes.SHA256(), ecdsa_deterministic=True)


def certificate_matches_key(cert: x509.Certificate, key: ec.EllipticCurvePrivateKey) -> bool:
    cert_key = cert.public_key()
    return isinstance(cert_key, ec.EllipticCurvePublicKey) and spki_sha256(cert_key) == spki_sha256(
        key.public_key()
    )


def to_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
