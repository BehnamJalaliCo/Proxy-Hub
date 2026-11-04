"""The real Telemt 3.5.16 (the pinned release) runs what the panel publishes.

Path: owner settings -> the ``mtproto`` loop -> the node's desired state (agent API code) -> the Go
agent's own renderer (``proxyhub-agent render-desired``) -> the pinned Telemt binary runs that file
-> it listens on the settings' port -> the agent's report (applied ETag, Telemt up) -> the loop marks
the proxy live -> ``provide_links`` -> a fake-TLS handshake built ONLY from the published link (its
port, secret and domain) is accepted by Telemt, while a wrong secret or a wrong domain is not. Then
the node retires, the loop disables the secret, and Telemt reloads the re-rendered file (SIGHUP, as
the node's unit does) and keeps serving.

The only edits to the rendered file move Telemt's data directory and metrics port into the test's
temporary directory (the node uses /var/lib/telemt and 127.0.0.1:9090). Telemt's stdout/stderr go
to /dev/null as on the node (rule 7).

Skipped when the Go toolchain is missing or the release cannot be downloaded AND verified against
node-image/versions.lock (sha256 of the tarball and of the binary). ``PROXYHUB_TELEMT_BINARY`` may
point at an already downloaded binary (its sha256 is checked again). The binary is never committed.
Without outbound MTProto (this sandbox) Telemt still serves the fake-TLS handshake; reaching
Telegram itself through a node is checked on the panel server (runbook).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import signal
import subprocess
import tarfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_agent_support import (
    REPO,
    free_port,
    go_binaries,
    requires_go,
    reset_config_caches,
    run_go,
    use_secret_files,
    write_secret_files,
)
from test_mtproto_support import (
    agent_reports,
    clean,
    fake_tls_handshake_ok,
    make_node,
    register_agent,
    session_maker,
    set_node_state,
)

from proxyhub.agent_api.desired import build_desired_state, canonical_json, etag_for
from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.mtproto.links import provide_links
from proxyhub.mtproto.settings import MtprotoSettings, save_settings
from proxyhub.mtproto.urls import user_name_for
from proxyhub.reconcilers.mtproto import MtprotoReconciler

pytestmark = [pytest.mark.db, requires_go]

LOCK = REPO / "node-image" / "versions.lock"
NODE_DATA_DIR = "/var/lib/telemt"
NODE_METRICS = "127.0.0.1:9090"


def _lock() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in LOCK.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="session")
def telemt_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    lock = _lock()
    given = os.environ.get("PROXYHUB_TELEMT_BINARY")
    if given:
        path = Path(given)
        if not path.is_file() or _sha256(path.read_bytes()) != lock["TELEMT_BINARY_SHA256"]:
            pytest.fail("PROXYHUB_TELEMT_BINARY is not the pinned Telemt binary (sha256 differs)")
        return path
    try:
        tarball = httpx.get(lock["TELEMT_URL"], follow_redirects=True, timeout=120).raise_for_status().content
    except httpx.HTTPError as exc:
        pytest.skip(f"the pinned Telemt release could not be downloaded here ({type(exc).__name__})")
    assert _sha256(tarball) == lock["TELEMT_TARBALL_SHA256"], (
        "Telemt tarball sha256 differs from versions.lock"
    )
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        member = tar.getmember("telemt")
        extracted = tar.extractfile(member)
        assert extracted is not None
        binary = extracted.read()
    assert _sha256(binary) == lock["TELEMT_BINARY_SHA256"], "Telemt binary sha256 differs from versions.lock"
    path = tmp_path_factory.mktemp("telemt-bin") / "telemt"
    path.write_bytes(binary)
    path.chmod(0o755)
    return path


@dataclass
class World:
    engine: AsyncEngine
    seal_key: bytes
    tmp: Path
    node: uuid.UUID
    port: int
    metrics_port: int

    async def run_loop(self) -> None:
        assert await MtprotoReconciler().try_run_once(self.engine)

    async def render(self) -> tuple[Path, str]:
        """The node's desired state as served, rendered by the real agent; returns (file, ETag)."""
        async with session_maker(self.engine)() as s:
            body = canonical_json(await build_desired_state(s, self.seal_key, self.node))
        doc = self.tmp / "desired.json"
        doc.write_bytes(body)
        out = self.tmp / "telemt.toml"
        out.unlink(missing_ok=True)
        rendered = run_go(go_binaries().agent, "render-desired", str(self.node), doc, out)
        assert rendered.returncode == 0, rendered.stderr
        assert run_go(go_binaries().agent, "check-config", out).returncode == 0
        text = out.read_text()
        assert NODE_DATA_DIR in text and f'metrics_listen = "{NODE_METRICS}"' in text
        data = self.tmp / "data"
        (data / "cache").mkdir(parents=True, exist_ok=True)
        local = self.tmp / "run.toml"
        local.write_text(
            text.replace(NODE_DATA_DIR, str(data)).replace(NODE_METRICS, f"127.0.0.1:{self.metrics_port}")
        )
        local.chmod(0o600)
        return local, etag_for(body)


@pytest.fixture
async def world(migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    files = write_secret_files(tmp_path / "secrets")
    use_secret_files(monkeypatch, files)
    engine = create_async_engine(migrated_db)
    await clean(engine)
    port = free_port()
    async with engine.begin() as conn:
        await save_settings(conn, MtprotoSettings(port=port))
    node = await make_node(engine, slot=1, ipv4="203.0.113.50")
    try:
        yield World(engine, load_seal_key(files.master_key), tmp_path, node, port, free_port())
    finally:
        await clean(engine)
        await engine.dispose()
        reset_config_caches()


@pytest.fixture
def telemt(telemt_binary: Path) -> Iterator[list[subprocess.Popen[bytes]]]:
    procs: list[subprocess.Popen[bytes]] = []
    yield procs
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _wait_listening(port: int, proc: subprocess.Popen[bytes], seconds: float = 60) -> None:
    import socket

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        assert proc.poll() is None, f"Telemt exited with {proc.returncode}"
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    pytest.fail(f"Telemt did not listen on {port} within {seconds:.0f}s")


def _start(binary: Path, config: Path, cwd: Path) -> subprocess.Popen[bytes]:
    # As on the node: Telemt's own output goes nowhere (rule 7).
    return subprocess.Popen(  # noqa: S603 - the pinned, sha256-checked binary and our own config
        [str(binary), str(config)],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def handshake(port: int, secret: bytes, sni: str) -> bool:
    return await asyncio.to_thread(fake_tls_handshake_ok, port, secret, sni)


async def test_telemt_accepts_the_published_link_and_reloads_it_disabled_when_the_node_retires(
    world: World, telemt_binary: Path, telemt: list[subprocess.Popen[bytes]]
) -> None:
    await world.run_loop()
    config, etag = await world.render()
    proc = _start(telemt_binary, config, world.tmp / "data")
    telemt.append(proc)
    await asyncio.to_thread(_wait_listening, world.port, proc)
    async with httpx.AsyncClient(trust_env=False, timeout=5) as http:
        metrics = (await http.get(f"http://127.0.0.1:{world.metrics_port}/metrics")).text
    assert 'telemt_build_info{version="3.5.16"}' in metrics
    assert f'user="{user_name_for(world.node)}"' in metrics  # Telemt loaded the panel's user

    # Not published before the agent reports the applied state with Telemt up.
    async with world.engine.begin() as conn:
        assert await provide_links(conn) == []
    await register_agent(world.engine, world.node)
    await agent_reports(world.engine, world.node, applied_etag=etag)
    await world.run_loop()
    async with world.engine.begin() as conn:
        (link,) = await provide_links(conn)
    assert link.status == "active" and link.slot == 1

    query = parse_qs(urlsplit(link.url).query)
    assert query["server"] == ["203.0.113.50"] and query["port"] == [str(world.port)]
    secret_field = query["secret"][0]
    assert secret_field.startswith("ee")
    secret, domain = bytes.fromhex(secret_field[2:34]), bytes.fromhex(secret_field[34:]).decode("ascii")
    assert domain == "www.microsoft.com"
    port = int(query["port"][0])
    assert await handshake(port, secret, domain)
    assert not await handshake(port, os.urandom(16), domain)
    assert not await handshake(port, secret, "www.example.org")

    # The node retires: the loop disables the secret; the re-rendered file (user_enabled = false)
    # passes the agent's check and Telemt reloads it (SIGHUP, as the node's unit does) and keeps
    # serving. Telemt checks user_enabled at admission, after the TLS layer, and counts a connection
    # only once it reached a Telegram DC; that refusal is checked on the panel server (runbook).
    await set_node_state(world.engine, world.node, "retiring")
    await world.run_loop()
    config, _ = await world.render()
    assert f"[access.user_enabled]\n{user_name_for(world.node)} = false\n" in config.read_text()
    proc.send_signal(signal.SIGHUP)
    await asyncio.sleep(2)
    assert proc.poll() is None, "Telemt did not survive the reload of the panel's file"
    await asyncio.to_thread(_wait_listening, world.port, proc, 10)
    async with world.engine.begin() as conn:
        (link,) = await provide_links(conn)
    assert link.status == "expired"
