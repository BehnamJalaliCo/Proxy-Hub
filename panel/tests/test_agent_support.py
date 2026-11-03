"""Shared helpers for the agent team's tests (no tests in this module).

Everything is real: PostgreSQL, the cryptography library, OpenSSL, the Go toolchain building the
agent's own code, uvicorn on a loopback port and (when available) Caddy in Docker. Nothing here
imitates Hetzner or Telegram (decision D10); the agent API does not talk to either.
"""

from __future__ import annotations

import asyncio
import functools
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.agent_api.runtime import get_agent_api_settings, reset_runtime_cache
from proxyhub.config import get_config

REPO = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO / "agent"
METRICS_FIXTURE = AGENT_DIR / "internal" / "metrics" / "testdata" / "telemt-3.5.16-metrics.txt"
PROXY_HEADER = "X-ProxyHub-Agent-Proxy-Secret"
FP_HEADER = "X-ProxyHub-Client-Cert-Sha256"
AGENT_TABLES = (
    "telemt_users",
    "telemt_node_configs",
    "agent_node_reports",
    "agent_nodes",
    "agent_certificates",
    "agent_registration_tokens",
    "agent_ca_certificates",
)

requires_go = pytest.mark.skipif(
    shutil.which("go") is None or not (AGENT_DIR / "go.mod").exists(),
    reason="needs the Go toolchain and agent/ (the panel container has neither)",
)


@dataclass(frozen=True)
class GoBinaries:
    agent: Path
    csr: Path


@functools.cache
def go_binaries() -> GoBinaries:
    """Build the real agent and the CSR helper once per test session."""
    out = Path(tempfile.mkdtemp(prefix="proxyhub-agent-bin-"))
    env = {**os.environ, "CGO_ENABLED": "0", "GOFLAGS": "-mod=readonly"}
    for name, pkg in (("proxyhub-agent", "./cmd/agent"), ("proxyhub-csr", "./cmd/proxyhub-csr")):
        subprocess.run(  # noqa: S603 - fixed argv, our own source tree
            ["go", "build", "-o", str(out / name), pkg],  # noqa: S607 - the Go toolchain from PATH
            cwd=AGENT_DIR,
            env=env,
            check=True,
            capture_output=True,
            timeout=600,
        )
    return GoBinaries(agent=out / "proxyhub-agent", csr=out / "proxyhub-csr")


def run_tool(*argv: str | Path, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run a program (a binary we built, or openssl/docker from PATH) by absolute path."""
    exe = shutil.which(str(argv[0])) or str(argv[0])
    return subprocess.run(  # noqa: S603 - fixed argv of test tools
        [exe, *(str(a) for a in argv[1:])], capture_output=True, text=True, timeout=timeout, check=False
    )


run_go = run_tool


def go_csr(tmp: Path, node_id: uuid.UUID, name: str = "node") -> tuple[Path, str]:
    """Run the agent's real key + CSR code; returns (key file, CSR PEM)."""
    key, csr = tmp / f"{name}.key", tmp / f"{name}.csr"
    result = run_go(go_binaries().csr, "-node-id", str(node_id), "-key-out", key, "-csr-out", csr)
    assert result.returncode == 0, result.stderr
    assert (key.stat().st_mode & 0o777) == 0o600
    return key, csr.read_text()


@dataclass(frozen=True)
class SecretFiles:
    ca_key: Path
    master_key: Path
    proxy_secret_file: Path
    proxy_secret: str


def write_secret_files(directory: Path) -> SecretFiles:
    """Secret files as deploy/ensure_secrets.sh makes them (OpenSSL when present)."""
    directory.mkdir(parents=True, exist_ok=True)
    ca_key = directory / "agent_ca_key"
    if shutil.which("openssl"):
        made = run_tool(
            "openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", ca_key
        )
        assert made.returncode == 0, made.stderr
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        ca_key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    master = directory / "master_key"
    master.write_text(secrets.token_hex(32))
    proxy_secret = secrets.token_hex(32)
    proxy_file = directory / "agent_proxy_secret"
    proxy_file.write_text(proxy_secret + "\n")
    for path in (ca_key, master, proxy_file):
        path.chmod(0o600)
    return SecretFiles(
        ca_key=ca_key, master_key=master, proxy_secret_file=proxy_file, proxy_secret=proxy_secret
    )


def use_secret_files(monkeypatch: pytest.MonkeyPatch, files: SecretFiles) -> None:
    monkeypatch.setenv("PROXYHUB_AGENT_CA_KEY_FILE", str(files.ca_key))
    monkeypatch.setenv("PROXYHUB_MASTER_KEY_FILE", str(files.master_key))
    monkeypatch.setenv("PROXYHUB_AGENT_PROXY_SECRET_FILE", str(files.proxy_secret_file))
    reset_config_caches()


def reset_config_caches() -> None:
    get_config.cache_clear()
    get_agent_api_settings.cache_clear()
    reset_runtime_cache()


async def clean_agent_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(AGENT_TABLES)}"))


async def create_node(engine: AsyncEngine, state: str = "booting") -> uuid.UUID:
    """A Hetzner account and a node row, each unique, as the fleet would have them."""
    account_id, node_id = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (:id, :name, 'agent-test-token-file-name', 'active')"
            ),
            {"id": account_id, "name": f"agent-test-{account_id}"},
        )
        await conn.execute(
            text(
                "INSERT INTO nodes (id, account_id, name, location, server_type, state, is_test) "
                "VALUES (:id, :acc, :name, 'fsn1', 'cpx12', :state, true)"
            ),
            {"id": node_id, "acc": account_id, "name": f"agent-test-{node_id}", "state": state},
        )
    return node_id


async def create_registration(engine: AsyncEngine, secret_name: str) -> uuid.UUID:
    reg_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mtproxy_registrations (id, host, port, secret_name, state) "
                "VALUES (:id, :host, 443, :name, 'registered')"
            ),
            {"id": reg_id, "host": f"n-{reg_id.hex[:8]}.proxy.example.com", "name": secret_name},
        )
    return reg_id


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def serve_panel() -> AsyncIterator[str]:
    """The real panel app under uvicorn on a random loopback port, in the test's event loop."""
    from proxyhub.api.app import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        sock.close()


def tls_server_material(directory: Path, hostname: str = "localhost") -> tuple[Path, Path, Path]:
    """A throwaway CA and a server certificate for ``hostname`` (the test Caddy's HTTPS side; in
    production Caddy uses a public ACME certificate). Returns (ca.pem, server.pem, server.key)."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent-test server CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem, cert_pem, key_pem = (
        directory / "server_ca.pem",
        directory / "server.pem",
        directory / "server.key",
    )
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_pem.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    key_pem.chmod(0o644)  # read by the Caddy container; a throwaway test key
    return ca_pem, cert_pem, key_pem
