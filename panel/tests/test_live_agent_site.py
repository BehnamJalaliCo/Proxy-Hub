"""The COMMITTED Caddy configuration (deploy/Caddyfile + deploy/caddy/agent-*.caddy) in the pinned
Caddy image, in front of the real panel (uvicorn), with the real Go agent and the real executor.

* ``caddy validate`` passes with the agent site off (AGENT_DOMAIN unset: no agent site, so no
  certificate is ever requested for it) and on.
* Running: the owner site still answers 404 for /agent/*; on the agent site, registration and the
  agent binary work without a client certificate, every other /agent/v1 route needs a verified one,
  and headers a client forges are removed; the binary served is exactly AGENT_SHA256.
* The unmodified agent registers with its one-time token (booting -> registering through the
  registration hook), reports, and the executor moves the node to ready; retiring it closes its
  identity, so the agent is refused afterwards.

For the running test only, Caddy's global block gets test ports, loopback binding and its local CA
(PANEL_DOMAIN=panel.localhost, reached on 127.0.0.1 with that name as SNI and Host;
AGENT_DOMAIN=localhost): no ACME, nothing listens publicly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from test_agent_e2e import CADDY_DIGEST, _docker_image
from test_agent_support import (
    FP_HEADER,
    PROXY_HEADER,
    SecretFiles,
    free_port,
    go_binaries,
    go_csr,
    requires_go,
    run_tool,
    serve_panel,
    use_secret_files,
    write_secret_files,
)
from test_live_support import (
    DEPLOY,
    NODE_IMAGE,
    LiveWorld,
    api_client,
    good_settings,
    live_world,
    requires_node_image,
)

from proxyhub.agent_api.runtime import get_agent_api_settings
from proxyhub.agent_api.tokens import issue_registration_token
from proxyhub.nodes.image import read_versions_lock

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_go, requires_node_image]

PANEL_HOST = "panel.localhost"


def caddy_image() -> str:
    image = _docker_image()
    if image is None:
        pytest.skip("needs Docker and the pinned Caddy image (not pulled by tests)")
    assert CADDY_DIGEST in image
    return image


def caddy_run(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return run_tool("docker", "run", "--rm", "--network", "none", *args, timeout=timeout)


def mounts(trust: Path | None = None, secret: Path | None = None, caddyfile: Path | None = None) -> list[str]:
    out = [
        "-v", f"{caddyfile or DEPLOY / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
        "-v", f"{DEPLOY / 'caddy'}:/etc/caddy/agent:ro",
    ]  # fmt: skip
    if trust is not None:
        out += ["-v", f"{trust}:/etc/caddy/agent-trust:ro"]
    if secret is not None:
        out += ["-v", f"{secret}:/run/secrets/agent_proxy_secret:ro"]
    return out


def export_ca(files: SecretFiles, out_dir: Path) -> Path:
    """The deployer's step: python -m proxyhub.pki ca-pem (public certificate, derived from the key)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "agent_ca.pem"
    env = {**os.environ, "PROXYHUB_AGENT_CA_KEY_FILE": str(files.ca_key)}
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "proxyhub.pki", "ca-pem", "--out", str(out)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert out.stat().st_mode & 0o777 == 0o644
    x509.load_pem_x509_certificate(out.read_bytes())
    return out


def caddy_cli(command: str, env: list[str], volumes: list[str]) -> subprocess.CompletedProcess[str]:
    """``caddy validate`` or ``caddy adapt`` of the committed Caddyfile (no network)."""
    args = ["caddy", command, "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
    return caddy_run(*env, *volumes, caddy_image(), *args)


def test_committed_config_is_valid_with_the_agent_site_off_and_on(tmp_path: Path) -> None:
    off_env = ["-e", "PANEL_DOMAIN=panel.example.com"]
    off = caddy_cli("validate", off_env, mounts())
    assert off.returncode == 0, off.stderr[-2000:]
    adapted = caddy_cli("adapt", off_env, mounts())
    assert adapted.returncode == 0, adapted.stderr[-2000:]
    config = json.loads(adapted.stdout)
    hosts = [
        h for r in config["apps"]["http"]["servers"]["srv0"]["routes"] for m in r["match"] for h in m["host"]
    ]
    assert hosts == ["panel.example.com"]  # no agent site: Caddy asks no certificate for it
    assert "client_authentication" not in adapted.stdout and "agent-trust" not in adapted.stdout

    trust = tmp_path / "trust"
    files = write_secret_files(tmp_path / "secrets")
    export_ca(files, trust)
    on_env = [*off_env, "-e", "AGENT_DOMAIN=agent.example.com", "-e", "PROXYHUB_AGENT_SITE=on"]
    on = caddy_cli("validate", on_env, mounts(trust, files.proxy_secret_file))
    assert on.returncode == 0, on.stderr[-2000:]
    adapted = caddy_cli("adapt", on_env, mounts(trust, files.proxy_secret_file))
    text = adapted.stdout
    config = json.loads(text)
    hosts = sorted(
        h for r in config["apps"]["http"]["servers"]["srv0"]["routes"] for m in r["match"] for h in m["host"]
    )
    assert hosts == ["agent.example.com", "panel.example.com"]
    assert '"mode":"verify_if_given"' in text.replace(" ", "")
    assert "/etc/caddy/agent-trust/agent_ca.pem" in text and "{file./run/secrets/agent_proxy_secret}" in text
    assert '"upstreams":[{"dial":"api:8000"}]' in text.replace(" ", "")  # production upstream by default
    # The agent site never logs requests (rule 7 spirit), like the owner site.
    assert text.count('"output":"discard"') >= 1


# --- the running site -------------------------------------------------------------------------------


@dataclass
class Site:
    port: int
    owner_url: str
    agent_url: str
    server_ca: Path
    container: str
    files: SecretFiles
    agent_ca: Path
    artifact_sha: str | None

    def client(self, **kw: Any) -> httpx.AsyncClient:
        ctx = ssl.create_default_context(cafile=str(self.server_ca))
        if "cert" in kw:
            ctx.load_cert_chain(str(kw.pop("cert")))
        return httpx.AsyncClient(verify=ctx, trust_env=False, timeout=10, **kw)


def patched_caddyfile(tmp: Path, http_port: int, https_port: int) -> Path:
    text = (DEPLOY / "Caddyfile").read_text()
    head = "{\n\tadmin off\n"
    assert text.startswith(head)
    test_globals = (
        f"\t# TEST ONLY (test_live_agent_site.py): ports, loopback, local CA.\n"
        f"\thttp_port {http_port}\n\thttps_port {https_port}\n\tdefault_bind 127.0.0.1\n"
        "\tlocal_certs\n\tskip_install_trust\n"
    )
    out = tmp / "Caddyfile"
    out.write_text(head + test_globals + text[len(head) :])
    return out


def build_artifact(tmp: Path) -> tuple[Path | None, str | None]:
    """The agent built exactly as panel/Dockerfile builds it (needs the pinned Go toolchain)."""
    lock = read_versions_lock(NODE_IMAGE)
    go = subprocess.run(  # noqa: S603
        [shutil.which("go") or "go", "env", "GOVERSION"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if go != f"go{lock['AGENT_GO_VERSION']}":
        return None, None
    out = tmp / "artifacts" / "proxyhub-agent"
    built = run_tool("bash", NODE_IMAGE / "build-agent.sh", out, timeout=600)
    assert built.returncode == 0, built.stderr
    return out, built.stdout.split()[0]


@asynccontextmanager
async def running_site(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Site]:
    image = caddy_image()
    files = write_secret_files(tmp / "secrets")
    use_secret_files(monkeypatch, files)
    artifact, artifact_sha = build_artifact(tmp)
    if artifact is not None:
        monkeypatch.setenv("PROXYHUB_AGENT_ARTIFACT_FILE", str(artifact))
        get_agent_api_settings.cache_clear()
    trust = tmp / "trust"
    agent_ca = export_ca(files, trust)
    http_port, https_port = free_port(), free_port()
    caddyfile = patched_caddyfile(tmp, http_port, https_port)
    for path in (caddyfile, trust, agent_ca, files.proxy_secret_file):
        path.chmod(0o755 if path.is_dir() else 0o644)  # read by the container; throwaway material
    async with serve_panel() as panel:
        upstream = panel.removeprefix("http://")
        name = f"proxyhub-live-site-{uuid.uuid4().hex[:8]}"
        started = await asyncio.to_thread(
            run_tool,
            "docker", "run", "-d", "--rm", "--name", name, "--network", "host",
            "-e", f"PANEL_DOMAIN={PANEL_HOST}", "-e", "AGENT_DOMAIN=localhost",
            "-e", "PROXYHUB_AGENT_SITE=on",
            "-e", f"PROXYHUB_AGENT_UPSTREAM={upstream}",
            *mounts(trust, files.proxy_secret_file, caddyfile), image,
            "caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile",
        )  # fmt: skip
        assert started.returncode == 0, started.stderr
        try:
            server_ca = tmp / "caddy_local_root.crt"
            for _ in range(100):
                got = await asyncio.to_thread(
                    run_tool, "docker", "cp", f"{name}:/data/caddy/pki/authorities/local/root.crt", server_ca
                )
                if got.returncode == 0:
                    break
                await asyncio.sleep(0.2)
            else:
                logs = await asyncio.to_thread(run_tool, "docker", "logs", name)
                pytest.fail(f"caddy did not start: {logs.stderr[-3000:]}")
            site = Site(
                port=https_port,
                owner_url=f"https://127.0.0.1:{https_port}",  # with SNI and Host panel.localhost
                agent_url=f"https://localhost:{https_port}",
                server_ca=server_ca,
                container=name,
                files=files,
                agent_ca=agent_ca,
                artifact_sha=artifact_sha,
            )
            async with site.client() as c:
                for _ in range(100):
                    try:
                        await c.get(site.agent_url + "/")
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(0.2)
            yield site
        finally:
            await asyncio.to_thread(run_tool, "docker", "rm", "-f", name)


@pytest.fixture
async def world(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    async with live_world(migrated_db, tmp_path, monkeypatch) as w:
        yield w


async def run_agent(cfg: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        str(go_binaries().agent), "-config", str(cfg), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
    )  # fmt: skip
    out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    return proc.returncode or 0, (out + err).decode()


def agent_config(site: Site, base: Path, node_id: uuid.UUID, token: str) -> Path:
    (base / "etc").mkdir(parents=True)
    (base / "telemt").mkdir()
    shutil.copy(site.agent_ca, base / "etc" / "ca.pem")
    token_file = base / "etc" / "registration_token"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    cfg = {
        "panel_url": site.agent_url,
        "node_id": str(node_id),
        "ca_file": str(base / "etc" / "ca.pem"),
        "server_ca_file": str(site.server_ca),
        "token_file": str(token_file),
        "state_dir": str(base / "state"),
        "telemt_config": str(base / "telemt" / "telemt.toml"),
        "telemt_data_dir": "/var/lib/telemt",
        "metrics_listen": f"127.0.0.1:{free_port()}",  # nothing listens: Telemt is not configured here
        "telemt_version": "3.5.16",
    }
    path = base / "etc" / "agent.json"
    path.write_text(json.dumps(cfg))
    return path


def fingerprint(identity: Path) -> str:
    cert = x509.load_pem_x509_certificate(identity.read_bytes())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


async def test_agent_site_end_to_end(
    world: LiveWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await world.settings(good_settings())
    outputs: list[str] = []
    async with running_site(tmp_path, monkeypatch) as site:
        token = await _flow(world, site, tmp_path, outputs)
        logs = await asyncio.to_thread(run_tool, "docker", "logs", site.container)
    secrets_seen = [token, site.files.proxy_secret]
    for text in [*outputs, caplog.text, logs.stdout, logs.stderr]:
        for value in secrets_seen:
            assert value not in text
    from test_fleet_support import assert_absent_from_database

    await assert_absent_from_database(world.engine, token)  # only its SHA-256 is stored


async def _flow(world: LiveWorld, site: Site, tmp: Path, outputs: list[str]) -> str:
    node_id = await world.add_node(state="booting")
    _, csr = go_csr(tmp, node_id, "probe")

    async with site.client() as c:
        # The owner's site never serves the agent API.
        owner = {"headers": {"Host": PANEL_HOST}, "extensions": {"sni_hostname": PANEL_HOST}}
        assert (
            await c.get(site.owner_url + "/", **owner)
        ).status_code == 502  # the owner site proxies (no api here)
        for path in ("/agent/v1/register", "/agent/v1/desired", "/agent/v1/artifacts/proxyhub-agent"):
            assert (await c.get(site.owner_url + path, **owner)).status_code == 404, path
        assert (await c.post(site.owner_url + "/agent/v1/register", json={}, **owner)).status_code == 404

        # Registration needs no client certificate; the request reaches the API with the real proxy
        # secret (a forged one is replaced; without the secret the API would answer 403).
        probe = {"token": "A" * 43, "csr_pem": csr, "agent_version": "0.1.0", "telemt_version": "3.5.16"}
        for headers in ({}, {PROXY_HEADER: "x" * 64, FP_HEADER: "0" * 64}):
            r = await c.post(site.agent_url + "/agent/v1/register", json=probe, headers=headers)
            assert r.status_code == 401 and r.json() == {"error": "invalid_token"}, r.text

        # Every other agent route needs a verified certificate: Caddy answers itself.
        for method, path in (
            ("GET", "/agent/v1/desired"),
            ("POST", "/agent/v1/report"),
            ("POST", "/agent/v1/renew"),
        ):
            r = await c.request(
                method, site.agent_url + path, headers={FP_HEADER: "0" * 64, PROXY_HEADER: "x" * 64}
            )
            assert r.status_code == 401 and r.json() == {"error": "client_certificate_required"}, (
                path,
                r.text,
            )
        for path in ("/", "/api/nodes", "/agent/v2/x", "/healthz"):
            assert (await c.get(site.agent_url + path)).status_code == 404, path

        # The agent binary: exactly AGENT_SHA256, without a client certificate.
        r = await c.get(site.agent_url + "/agent/v1/artifacts/proxyhub-agent")
        if site.artifact_sha is None:
            assert r.status_code == 503 and r.json()["error"] in (
                "artifact_missing",
                "artifact_hash_mismatch",
            )
        else:
            lock = read_versions_lock(NODE_IMAGE)
            assert r.status_code == 200, r.text
            assert hashlib.sha256(r.content).hexdigest() == lock["AGENT_SHA256"] == site.artifact_sha
            assert r.headers["x-proxyhub-artifact-sha256"] == lock["AGENT_SHA256"]
            assert (
                r.headers["cache-control"] == "no-store"
                and "server" not in r.headers
                and "via" not in r.headers
            )

    # The real agent: one-time token -> certificate (registering), desired state, report.
    async with world.sessions() as s:
        token = await issue_registration_token(s, node_id)
        await s.commit()
    cfg = agent_config(site, tmp / "node", node_id, token)
    code, out = await run_agent(cfg, "once")
    outputs.append(out)
    assert code == 0, out
    assert (await world.node(node_id))["state"] == "registering"  # the registration hook
    await world.executor().run_once(world.engine)
    node = await world.node(node_id)
    assert node["state"] == "ready" and node["ready_at"] is not None, await world.events(node_id)
    assert world.no_hetzner.calls == 0

    # A forged fingerprint header next to a real certificate: Caddy sets the verified one.
    identity = tmp / "node" / "state" / "identity.pem"
    async with site.client(cert=identity) as c:
        r = await c.get(
            site.agent_url + "/agent/v1/desired", headers={FP_HEADER: "f" * 64, PROXY_HEADER: "x"}
        )
        assert r.status_code == 200 and r.json()["node_id"] == str(node_id)
        assert r.headers["cache-control"] == "no-store"
        assert (await c.get(site.agent_url + "/agent/v1/nope")).status_code == 404  # proxied, API 404
    fp = fingerprint(identity)
    assert await world.rows(
        "SELECT count(*) FROM agent_certificates WHERE fingerprint_sha256 = :f", f=fp
    ) == [(1,)]

    async with api_client(world) as api:
        listed = {n["id"]: n for n in (await api.get("/api/nodes")).json()}
        agent = listed[str(node_id)]["agent"]
        assert agent["registered"] is True and agent["telemt_version"] == "3.5.16" and agent["last_seen_at"]
        # Retire: no server was created for this row, so it closes without Hetzner, and the node's
        # identity is closed with it.
        assert (await api.post(f"/api/nodes/{node_id}/retire")).status_code == 202
    await world.executor().run_once(world.engine)
    assert (await world.node(node_id))["state"] == "deleted"
    revoked = await world.rows("SELECT revoked_reason FROM agent_certificates WHERE node_id = :n", n=node_id)
    assert revoked == [("node_deleted",)]
    code, out = await run_agent(cfg, "once")
    outputs.append(out)
    assert code != 0  # refused from now on
    kinds = [k for k, _ in await world.events(node_id)]
    assert "agent_registered" in kinds and kinds.count("state") == 4  # registering, ready, retiring, deleted
    return token
