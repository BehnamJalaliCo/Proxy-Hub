"""End to end with the real Go agent against the real panel (uvicorn on a random loopback port).

1. Without Docker: the agent's own key/CSR code, registration over real HTTP, the agent's own
   certificate check, the desired state fetched over HTTP and rendered by the agent binary into a
   Telemt config that passes its invariant check.
2. With Docker and the pinned Caddy image present: the production trust path. Caddy terminates
   TLS, verifies the node's client certificate against the panel CA and forwards the fingerprint
   plus the proxy secret; the unmodified agent binary registers with its one-time token, pulls the
   desired state (ETag), writes telemt.toml, scrapes Telemt's metrics exposition, reports, renews
   its certificate, and a forged fingerprint header is refused.

The metrics endpoint serves agent/internal/metrics/testdata (Telemt 3.5.16's own exposition
format); Telemt itself is not run here (it is run on a real node, docs/10 M2.3).
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import shutil
import ssl
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_agent_support import (
    FP_HEADER,
    METRICS_FIXTURE,
    PROXY_HEADER,
    SecretFiles,
    clean_agent_tables,
    create_node,
    create_registration,
    free_port,
    go_binaries,
    go_csr,
    requires_go,
    reset_config_caches,
    run_go,
    run_tool,
    serve_panel,
    tls_server_material,
    use_secret_files,
    write_secret_files,
)

from proxyhub.agent_api.desired import create_telemt_user, reveal_secret, set_telemt_node_config
from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.agent_api.tokens import issue_registration_token
from proxyhub.db.session import get_engine
from proxyhub.pki.ca import build_ca_certificate, load_ca_key, to_pem

pytestmark = [pytest.mark.db, requires_go]

# The same image deploy/docker-compose.yml pins; a registry mirror of the SAME digest is accepted.
CADDY_DIGEST = "sha256:e70b164da81ef698b3ad19af799ea6086ce6d9e48ed4079eb9fe26f6fbf14ee5"
CADDY_IMAGES = (f"caddy:2-alpine@{CADDY_DIGEST}", f"mirror.gcr.io/library/caddy@{CADDY_DIGEST}")

# Reference Caddy site for agents (proposed for deploy/Caddyfile; see the report): mTLS optional at
# the TLS layer so a new node can register with its token, the verified client-certificate
# fingerprint and the proxy secret set by Caddy itself (overwriting anything the client sent).
CADDYFILE = """\
{{
	admin off
	auto_https off
	persist_config off
}}

https://localhost:{port} {{
	bind 127.0.0.1
	tls /etc/proxyhub-test/server.pem /etc/proxyhub-test/server.key {{
		client_auth {{
			mode verify_if_given
			trust_pool file /etc/proxyhub-test/agent_ca.pem
		}}
	}}
	handle /agent/v1/* {{
		reverse_proxy 127.0.0.1:{upstream} {{
			header_up X-ProxyHub-Agent-Proxy-Secret {{file./etc/proxyhub-test/agent_proxy_secret}}
			header_up X-ProxyHub-Client-Cert-Sha256 {{http.request.tls.client.fingerprint}}
		}}
	}}
	handle {{
		respond 404
	}}
}}
"""


@dataclass
class World:
    engine: AsyncEngine
    files: SecretFiles
    tmp: Path
    panel_url: str
    node_id: uuid.UUID
    alice_reg: uuid.UUID

    async def rows(self, sql: str, **params: Any) -> list[Any]:
        async with self.engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result.all()) if result.returns_rows else []

    async def token(self) -> str:
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            token = await issue_registration_token(session, self.node_id)
            await session.commit()
            return token

    async def secret(self, name: str) -> str:
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            return await reveal_secret(session, load_seal_key(self.files.master_key), self.node_id, name)


@pytest.fixture
async def world(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    files = write_secret_files(tmp_path / "secrets")
    use_secret_files(monkeypatch, files)
    engine = create_async_engine(migrated_db)
    await clean_agent_tables(engine)
    node_id = await create_node(engine)
    alice_reg = await create_registration(engine, "alice")
    seal = load_seal_key(files.master_key)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        await set_telemt_node_config(
            session, node_id, ports=[443], tls_domain="cdn.example.com", ad_tag="a" * 32
        )
        await create_telemt_user(session, seal, node_id, "alice", registration_id=alice_reg)
        await create_telemt_user(
            session,
            seal,
            node_id,
            "sponsor_b",
            ad_tag="b" * 32,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        await session.commit()
    try:
        async with serve_panel() as url:
            yield World(engine, files, tmp_path, url, node_id, alice_reg)
    finally:
        await engine.dispose()
        await get_engine().dispose()
        reset_config_caches()


def fp_of(cert_pem: str) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


async def test_go_csr_registers_over_http_and_desired_state_renders(world: World) -> None:
    key_file, csr = go_csr(world.tmp, world.node_id)
    token = await world.token()
    secret_header = {PROXY_HEADER: world.files.proxy_secret}
    async with httpx.AsyncClient(base_url=world.panel_url, trust_env=False) as client:
        resp = await client.post(
            "/agent/v1/register",
            json={"token": token, "csr_pem": csr, "agent_version": "0.1.0", "telemt_version": "3.5.16"},
            headers=secret_header,
        )
        assert resp.status_code == 200, resp.text
        again = await client.post(
            "/agent/v1/register",
            json={"token": token, "csr_pem": csr, "agent_version": "0.1.0", "telemt_version": None},
            headers=secret_header,
        )
        assert again.status_code == 401
        bundle = resp.json()
        (world.tmp / "cert.pem").write_text(bundle["cert_pem"])
        (world.tmp / "ca.pem").write_text(to_pem(build_ca_certificate(load_ca_key(world.files.ca_key))))
        (world.tmp / "returned_ca.pem").write_text(bundle["ca_pem"])
        check = run_go(
            go_binaries().csr, "-verify", "-node-id", str(world.node_id), "-key", key_file,
            "-cert", world.tmp / "cert.pem", "-ca", world.tmp / "ca.pem",
            "-returned-ca", world.tmp / "returned_ca.pem",
        )  # fmt: skip
        assert check.returncode == 0, check.stderr

        desired = await client.get(
            "/agent/v1/desired", headers={**secret_header, FP_HEADER: fp_of(bundle["cert_pem"])}
        )
        assert desired.status_code == 200
    doc_file = world.tmp / "desired.json"
    doc_file.write_bytes(desired.content)
    out = world.tmp / "telemt.toml"
    rendered = run_go(go_binaries().agent, "render-desired", str(world.node_id), doc_file, out)
    assert rendered.returncode == 0, rendered.stderr
    assert out.stat().st_mode & 0o777 == 0o600
    assert run_go(go_binaries().agent, "check-config", out).returncode == 0
    toml = out.read_text()
    for line in ("use_middle_proxy = true", "beobachten = false", 'log_level = "silent"', "enabled = false"):
        assert line in toml
    assert f'alice = "{await world.secret("alice")}"' in toml
    assert f'sponsor_b = "{"b" * 32}"' in toml  # per-user sponsor tag
    # A desired state for another node is refused by the agent.
    wrong = run_go(go_binaries().agent, "render-desired", str(uuid.uuid4()), doc_file, world.tmp / "x.toml")
    assert wrong.returncode != 0 and not (world.tmp / "x.toml").exists()


# --- the production trust path through Caddy --------------------------------------------------


def _docker_image() -> str | None:
    if shutil.which("docker") is None:
        return None
    candidates = (
        [os.environ["PROXYHUB_TEST_CADDY_IMAGE"]] if os.environ.get("PROXYHUB_TEST_CADDY_IMAGE") else []
    )
    for image in (*candidates, *CADDY_IMAGES):
        if CADDY_DIGEST not in image:
            continue
        if run_tool("docker", "image", "inspect", image).returncode == 0:
            return image
    return None


@asynccontextmanager
async def caddy(world: World, upstream_port: int) -> AsyncIterator[tuple[int, Path]]:
    image = _docker_image()
    if image is None:
        pytest.skip("needs Docker and the pinned Caddy image (not pulled by tests)")
    conf = world.tmp / "caddy"
    conf.mkdir()
    conf.chmod(0o755)
    server_ca, _, _ = tls_server_material(conf)
    (conf / "agent_ca.pem").write_text(to_pem(build_ca_certificate(load_ca_key(world.files.ca_key))))
    shutil.copy(world.files.proxy_secret_file, conf / "agent_proxy_secret")
    for f in conf.iterdir():
        f.chmod(0o644)  # read by the container; throwaway test material
    port = free_port()
    (conf / "Caddyfile").write_text(CADDYFILE.format(port=port, upstream=upstream_port))
    name = f"proxyhub-agent-e2e-{uuid.uuid4().hex[:8]}"
    started = await asyncio.to_thread(
        run_tool,
        "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
        "-v", f"{conf}:/etc/proxyhub-test:ro", image,
        "caddy", "run", "--config", "/etc/proxyhub-test/Caddyfile", "--adapter", "caddyfile",
    )  # fmt: skip
    assert started.returncode == 0, started.stderr
    try:
        ctx = ssl.create_default_context(cafile=str(server_ca))
        async with httpx.AsyncClient(verify=ctx, trust_env=False, timeout=2) as client:
            for _ in range(100):
                try:
                    await client.get(f"https://localhost:{port}/")
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(0.2)
            else:
                logs = await asyncio.to_thread(run_tool, "docker", "logs", name)
                pytest.fail(f"caddy did not start: {logs.stdout}{logs.stderr}")
        yield port, server_ca
    finally:
        await asyncio.to_thread(run_tool, "docker", "rm", "-f", name)


class _Metrics(http.server.BaseHTTPRequestHandler):
    body = b""
    paths: list[str] = []  # noqa: RUF012 - shared between handler instances on purpose

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        if self.path != "/metrics":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: Any) -> None:  # no request log (it would hold the peer address)
        return


@contextmanager
def metrics_server() -> Iterator[tuple[int, list[str]]]:
    _Metrics.body = METRICS_FIXTURE.read_bytes()
    _Metrics.paths = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Metrics)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], _Metrics.paths
    finally:
        server.shutdown()
        server.server_close()


async def run_agent(*args: str | Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        str(go_binaries().agent),
        *[str(a) for a in args],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    return proc.returncode or 0, (out + err).decode()


def agent_config(world: World, port: int, server_ca: Path, metrics_port: int, name: str) -> tuple[Path, Path]:
    base = world.tmp / name
    (base / "etc").mkdir(parents=True)
    (base / "etc" / "ca.pem").write_text(to_pem(build_ca_certificate(load_ca_key(world.files.ca_key))))
    (base / "telemt").mkdir()
    cfg = {
        "panel_url": f"https://localhost:{port}",
        "node_id": str(world.node_id),
        "ca_file": str(base / "etc" / "ca.pem"),
        "server_ca_file": str(server_ca),
        "token_file": str(base / "etc" / "registration_token"),
        "state_dir": str(base / "state"),
        "telemt_config": str(base / "telemt" / "telemt.toml"),
        "telemt_data_dir": "/var/lib/telemt",
        "metrics_listen": f"127.0.0.1:{metrics_port}",
        "telemt_version": "3.5.16",
    }
    path = base / "etc" / "agent.json"
    path.write_text(json.dumps(cfg))
    return path, base


async def test_agent_through_caddy_mtls(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = int(world.panel_url.rsplit(":", 1)[1])
    logs: list[str] = []
    async with caddy(world, upstream) as (port, server_ca):
        with metrics_server() as (metrics_port, scraped):
            await _through_caddy(world, monkeypatch, port, server_ca, metrics_port, scraped, logs)
    secrets_seen = [await world.secret("alice"), await world.secret("sponsor_b"), world.files.proxy_secret]
    for output in logs:
        for value in secrets_seen:
            assert value not in output


async def _through_caddy(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    port: int,
    server_ca: Path,
    metrics_port: int,
    scraped: list[str],
    logs: list[str],
) -> None:
    async def agent(*args: str | Path) -> int:
        code, out = await run_agent(*args)
        logs.append(out)
        return code

    cfg, base = agent_config(world, port, server_ca, metrics_port, "node")
    token = await world.token()
    token_file = base / "etc" / "registration_token"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)

    # First cycle: register (one-time token), pull, apply, scrape, report.
    assert await agent("-config", cfg, "once") == 0, logs[-1]
    assert not token_file.exists(), "the used token file must be removed"
    identity = base / "state" / "identity.pem"
    assert identity.stat().st_mode & 0o777 == 0o600
    telemt_toml = base / "telemt" / "telemt.toml"
    assert telemt_toml.stat().st_mode & 0o777 == 0o600
    assert run_go(go_binaries().agent, "check-config", telemt_toml).returncode == 0
    toml = telemt_toml.read_text()
    assert f'alice = "{await world.secret("alice")}"' in toml
    assert f'sponsor_b = "{await world.secret("sponsor_b")}"' in toml
    assert set(scraped) == {"/metrics"}  # nothing else is ever requested from Telemt

    served = await world.rows("SELECT last_served_etag FROM agent_nodes WHERE node_id = :n", n=world.node_id)
    node = await world.rows(
        "SELECT last_applied_etag, last_apply_ok, telemt_version FROM agent_nodes WHERE node_id = :n",
        n=world.node_id,
    )
    assert node == [(served[0][0], True, "3.5.16")]
    stats = await world.rows(
        "SELECT current_connections, total_octets FROM mtproxy_stats WHERE registration_id = :r",
        r=world.alice_reg,
    )
    assert stats == [(37, 1048576 + 52428800)]  # parsed from Telemt's own exposition format
    issued = await world.rows("SELECT issued_via FROM agent_certificates WHERE node_id = :n", n=world.node_id)
    assert issued == [("register",)]

    # Second cycle: unchanged desired state (304), the file is not rewritten, another report.
    mtime = telemt_toml.stat().st_mtime_ns
    assert await agent("-config", cfg, "once") == 0, logs[-1]
    assert telemt_toml.stat().st_mtime_ns == mtime
    reports = await world.rows("SELECT count(*) FROM agent_node_reports WHERE node_id = :n", n=world.node_id)
    assert reports == [(2,)]

    # The same token can not register a second identity.
    cfg2, base2 = agent_config(world, port, server_ca, metrics_port, "intruder")
    (base2 / "etc" / "registration_token").write_text(token)
    (base2 / "etc" / "registration_token").chmod(0o600)
    assert await agent("-config", cfg2, "register") != 0
    assert "invalid_token" in logs[-1]

    # Headers a client forges are overwritten by Caddy: no client certificate means no identity,
    # and the real proxy secret replaces a forged one (otherwise the answer would be 403).
    fp = fp_of(identity.read_text())
    ctx = ssl.create_default_context(cafile=str(server_ca))
    async with httpx.AsyncClient(verify=ctx, trust_env=False) as client:
        forged = await client.get(f"https://localhost:{port}/agent/v1/desired", headers={FP_HEADER: fp})
        assert forged.status_code == 401
        forged = await client.get(
            f"https://localhost:{port}/agent/v1/desired", headers={FP_HEADER: fp, PROXY_HEADER: "x" * 64}
        )
        assert forged.status_code == 401
        assert (await client.get(f"https://localhost:{port}/api/healthz")).status_code == 404

    # Renewal: with a short certificate lifetime the next cycle is past two thirds and renews,
    # and the first use of the new certificate retires the old one.
    monkeypatch.setattr("proxyhub.pki.issue.LEAF_LIFETIME", timedelta(seconds=40))
    await world.rows("DELETE FROM agent_certificates WHERE node_id = :n", n=world.node_id)
    token_file.write_text(await world.token())
    token_file.chmod(0o600)
    identity.unlink()
    assert await agent("-config", cfg, "register") == 0, logs[-1]
    assert await agent("-config", cfg, "once") == 0, logs[-1]
    certs = await world.rows(
        "SELECT issued_via, revoked_reason FROM agent_certificates WHERE node_id = :n ORDER BY id",
        n=world.node_id,
    )
    assert certs == [("register", "superseded"), ("renew", None)], logs[-1]
    assert "certificate renewed" in logs[-1]
