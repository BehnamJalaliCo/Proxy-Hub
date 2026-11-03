"""Agent PKI with real cryptography: the deterministic CA, signing CSRs made by the Go agent's own
code, and cross-checks with OpenSSL and with the agent's own certificate check."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from test_agent_support import go_binaries, go_csr, requires_go, run_go, run_tool, write_secret_files

from proxyhub.config import SecretFileError
from proxyhub.pki.ca import (
    CA_NOT_AFTER,
    CA_NOT_BEFORE,
    CaKeyError,
    build_ca_certificate,
    ca_serial,
    load_ca_key,
    spki_sha256,
    to_pem,
)
from proxyhub.pki.issue import (
    CLOCK_SKEW,
    LEAF_LIFETIME,
    CsrError,
    node_id_from_certificate,
    node_uri,
    parse_csr,
    renewal_time,
    sign_node_csr,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _ca(tmp_path: Path) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = load_ca_key(write_secret_files(tmp_path / "secrets").ca_key)
    return key, build_ca_certificate(key)


def test_ca_certificate_is_deterministic(tmp_path: Path) -> None:
    files = write_secret_files(tmp_path / "secrets")
    first = build_ca_certificate(load_ca_key(files.ca_key))
    second = build_ca_certificate(load_ca_key(files.ca_key))  # key re-read from the file
    assert first.public_bytes(serialization.Encoding.DER) == second.public_bytes(serialization.Encoding.DER)
    other = build_ca_certificate(load_ca_key(write_secret_files(tmp_path / "other").ca_key))
    assert other.serial_number != first.serial_number
    assert other.public_bytes(serialization.Encoding.DER) != first.public_bytes(serialization.Encoding.DER)


def test_ca_certificate_profile(tmp_path: Path) -> None:
    key, ca = _ca(tmp_path)
    assert ca.subject == ca.issuer
    assert ca.not_valid_before_utc == CA_NOT_BEFORE and ca.not_valid_after_utc == CA_NOT_AFTER
    assert ca.serial_number == ca_serial(key.public_key()) and 0 < ca.serial_number < 2**159
    basic = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical and basic.value.ca and basic.value.path_length == 0
    usage = ca.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical and usage.value.key_cert_sign and usage.value.crl_sign
    assert not usage.value.digital_signature
    ca_pub = ca.public_key()
    assert isinstance(ca_pub, ec.EllipticCurvePublicKey)
    ca_pub.verify(ca.signature, ca.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))  # self-signed
    assert spki_sha256(ca_pub) == spki_sha256(key.public_key())


def test_ca_key_file_checks(tmp_path: Path) -> None:
    with pytest.raises(CaKeyError):
        load_ca_key(tmp_path / "missing")
    rsa_file = tmp_path / "rsa"
    rsa_file.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    with pytest.raises(CaKeyError, match="P-256"):
        load_ca_key(rsa_file)
    p384 = tmp_path / "p384"
    p384.write_bytes(
        ec.generate_private_key(ec.SECP384R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    with pytest.raises(CaKeyError, match="P-256"):
        load_ca_key(p384)
    good = write_secret_files(tmp_path / "secrets").ca_key
    good.chmod(0o666)
    with pytest.raises(CaKeyError) as info:
        load_ca_key(good)
    assert isinstance(info.value.__context__, SecretFileError) or "unreadable" in str(info.value)
    # The error names the file and the reason, never the key material.
    assert "BEGIN" not in str(info.value)


def test_pki_cli_writes_the_same_ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from proxyhub.config import get_config
    from proxyhub.pki.__main__ import main

    files = write_secret_files(tmp_path / "secrets")
    monkeypatch.setenv("PROXYHUB_AGENT_CA_KEY_FILE", str(files.ca_key))
    get_config.cache_clear()
    try:
        out = tmp_path / "agent_ca.pem"
        assert main(["ca-pem", "--out", str(out)]) == 0
        assert out.read_text() == to_pem(build_ca_certificate(load_ca_key(files.ca_key)))
        assert out.stat().st_mode & 0o777 == 0o644
    finally:
        get_config.cache_clear()


@requires_go
def test_sign_csr_made_by_the_go_agent(tmp_path: Path) -> None:
    ca_key, ca = _ca(tmp_path)
    node_id = uuid.uuid4()
    key_file, csr_pem = go_csr(tmp_path, node_id)
    issued = sign_node_csr(parse_csr(csr_pem, node_id), node_id=node_id, ca_key=ca_key, ca_cert=ca, now=NOW)
    cert = issued.cert

    assert cert.issuer == ca.subject
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == str(node_id)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.UniformResourceIdentifier) == [node_uri(node_id)]
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert cert.not_valid_before_utc == NOW - CLOCK_SKEW
    assert cert.not_valid_after_utc == NOW + LEAF_LIFETIME
    assert (
        issued.fingerprint_sha256 == hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    )
    assert node_id_from_certificate(cert) == node_id
    # The certificate carries exactly the key the Go agent generated.
    node_key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    assert isinstance(node_key, ec.EllipticCurvePrivateKey)
    assert spki_sha256(node_key.public_key()) == spki_sha256(cert.public_key())  # type: ignore[arg-type]
    ca_pub = ca.public_key()
    assert isinstance(ca_pub, ec.EllipticCurvePublicKey)
    ca_pub.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))
    assert (
        renewal_time(issued.not_before, issued.not_after)
        == issued.not_before + (issued.not_after - issued.not_before) * 2 / 3
    )

    # The agent's own check (Go crypto/x509, client-auth chain) accepts it.
    (tmp_path / "ca.pem").write_text(to_pem(ca))
    (tmp_path / "cert.pem").write_text(issued.pem)
    ok = run_go(
        go_binaries().csr, "-verify", "-node-id", str(node_id), "-key", key_file,
        "-cert", tmp_path / "cert.pem", "-ca", tmp_path / "ca.pem",
    )  # fmt: skip
    assert ok.returncode == 0, ok.stderr
    # ... and refuses it for another node, or when the panel returns another CA.
    wrong = run_go(
        go_binaries().csr, "-verify", "-node-id", str(uuid.uuid4()), "-key", key_file,
        "-cert", tmp_path / "cert.pem", "-ca", tmp_path / "ca.pem",
    )  # fmt: skip
    assert wrong.returncode != 0
    _, other_ca = _ca(tmp_path / "other")
    (tmp_path / "other_ca.pem").write_text(to_pem(other_ca))
    swapped = run_go(
        go_binaries().csr, "-verify", "-node-id", str(node_id), "-key", key_file,
        "-cert", tmp_path / "cert.pem", "-ca", tmp_path / "ca.pem", "-returned-ca", tmp_path / "other_ca.pem",
    )  # fmt: skip
    assert swapped.returncode != 0


@requires_go
@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command")
def test_openssl_agrees(tmp_path: Path) -> None:
    ca_key, ca = _ca(tmp_path)
    node_id = uuid.uuid4()
    _, csr_pem = go_csr(tmp_path, node_id)
    issued = sign_node_csr(
        parse_csr(csr_pem, node_id), node_id=node_id, ca_key=ca_key, ca_cert=ca, now=datetime.now(UTC)
    )
    (tmp_path / "ca.pem").write_text(to_pem(ca))
    (tmp_path / "leaf.pem").write_text(issued.pem)
    ca_file, leaf_file = tmp_path / "ca.pem", tmp_path / "leaf.pem"
    verify = run_tool("openssl", "verify", "-CAfile", ca_file, "-purpose", "sslclient", leaf_file)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    server = run_tool("openssl", "verify", "-CAfile", ca_file, "-purpose", "sslserver", leaf_file)
    assert server.returncode != 0  # client authentication only
    fp = run_tool("openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", leaf_file)
    assert fp.returncode == 0
    assert fp.stdout.strip().split("=", 1)[1].replace(":", "").lower() == issued.fingerprint_sha256


@requires_go
def test_csr_refusals(tmp_path: Path) -> None:
    node_id = uuid.uuid4()
    _, csr_pem = go_csr(tmp_path, node_id)
    with pytest.raises(CsrError) as wrong_node:
        parse_csr(csr_pem, uuid.uuid4())
    assert wrong_node.value.code == "csr_wrong_node"

    der = x509.load_pem_x509_csr(csr_pem.encode()).public_bytes(serialization.Encoding.DER)
    tampered = bytearray(der)
    tampered[-5] ^= 0x01  # inside the signature
    tampered_pem = (
        "-----BEGIN CERTIFICATE REQUEST-----\n"
        + __import__("base64").encodebytes(bytes(tampered)).decode()
        + "-----END CERTIFICATE REQUEST-----\n"
    )
    with pytest.raises(CsrError) as bad:
        parse_csr(tampered_pem, node_id)
    assert bad.value.code in {"csr_bad_signature", "csr_unparseable"}

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(node_id))]))
        .sign(rsa_key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    with pytest.raises(CsrError) as not_p256:
        parse_csr(rsa_csr, node_id)
    assert not_p256.value.code == "csr_key_not_p256"

    with pytest.raises(CsrError) as junk:
        parse_csr("-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n-----END CERTIFICATE REQUEST-----\n", node_id)
    assert junk.value.code == "csr_unparseable"
    with pytest.raises(CsrError) as huge:
        parse_csr("A" * 5000, node_id)
    assert huge.value.code == "csr_too_large"


def test_renewal_point_is_two_thirds() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert renewal_time(start, start + timedelta(days=30)) == start + timedelta(days=20)
