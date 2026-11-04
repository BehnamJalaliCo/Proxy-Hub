"""Live team: Hetzner client timeouts, the agent artifact route's refusals, the operator CLI, and
architecture rules of the node executor (CLAUDE.md rules 3, 5 and 8)."""

from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
from httpx import ASGITransport, AsyncClient
from test_agent_support import PROXY_HEADER, reset_config_caches, use_secret_files, write_secret_files
from test_live_support import LiveWorld, good_settings, live_world

from proxyhub.agent_api.runtime import get_agent_api_settings
from proxyhub.hetzner import client as hetzner_client
from proxyhub.hetzner.client import HetznerReader
from proxyhub.nodes import executor as ex
from proxyhub.reconcilers import fleet as fleet_loop

PANEL = Path(__file__).resolve().parents[1] / "proxyhub"
MUTATION_NAMES = {"create_server", "delete_server", "issue_create_permit", "issue_delete_permit"}
ALLOWED = {
    PANEL / "hetzner" / "mutations.py",  # the hcloud calls themselves
    PANEL / "safety" / "gates.py",  # the permits themselves
    PANEL / "nodes" / "executor.py",  # the single caller, test nodes only
}


# --- Hetzner client timeouts ------------------------------------------------------------------------


def test_hetzner_reader_sets_connect_and_read_timeouts() -> None:
    reader = HetznerReader("0" * 64)
    # hcloud 2.27.0 passes this to every requests call (ClientBase.request: kwargs.setdefault).
    assert reader.raw._client._timeout == (hetzner_client.CONNECT_TIMEOUT_S, hetzner_client.READ_TIMEOUT_S)
    assert (hetzner_client.CONNECT_TIMEOUT_S, hetzner_client.READ_TIMEOUT_S) == (10.0, 30.0)


@pytest.fixture
def silent_server() -> Iterator[int]:
    """A real TCP server that accepts connections and never answers."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
                held.append(conn)
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        stop.set()
        thread.join(2)
        for c in held:
            c.close()
        sock.close()


def test_a_silent_hetzner_endpoint_times_out(silent_server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Before this change hcloud waited forever on a silent connection (timeout None)."""
    monkeypatch.setattr(hetzner_client, "READ_TIMEOUT_S", 0.5)
    reader = HetznerReader("0" * 64)
    reader.raw._client._endpoint = f"http://127.0.0.1:{silent_server}"  # a real socket, not Hetzner
    reader.raw._client._retry_max_retries = 0  # one attempt; hcloud retries timeouts 5 times otherwise
    started = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        reader.raw.request("GET", "/servers")
    assert time.monotonic() - started < 5


# --- the agent artifact route refuses anything but the pinned binary --------------------------------


@pytest.fixture
async def agent_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, str]]:
    from proxyhub.api.app import app

    files = write_secret_files(tmp_path / "secrets")
    use_secret_files(monkeypatch, files)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://agent.test") as c:
        yield c, files.proxy_secret
    reset_config_caches()


async def test_artifact_route_refuses_a_missing_or_different_binary(
    agent_api: tuple[AsyncClient, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, secret = agent_api
    path = "/agent/v1/artifacts/proxyhub-agent"
    artifact = tmp_path / "proxyhub-agent"
    monkeypatch.setenv("PROXYHUB_AGENT_ARTIFACT_FILE", str(artifact))
    get_agent_api_settings.cache_clear()

    assert (await client.get(path)).status_code == 403  # only through the agent site (proxy secret)
    r = await client.get(path, headers={PROXY_HEADER: secret})
    assert r.status_code == 503 and r.json() == {"error": "artifact_missing"}
    artifact.write_bytes(b"\x7fELF not the pinned agent")
    r = await client.get(path, headers={PROXY_HEADER: secret})
    assert r.status_code == 503 and r.json() == {"error": "artifact_hash_mismatch"}
    assert b"ELF" not in r.content


def test_verified_artifact_is_reused_until_the_file_or_the_pin_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public route must not hash megabytes per request, yet never serve unverified bytes."""
    import hashlib

    from proxyhub.agent_api import artifacts
    from proxyhub.nodes.image import REPO_NODE_IMAGE
    from proxyhub.nodes.settings import get_live_settings

    pinned = os.urandom(4096)
    image = tmp_path / "node-image"
    image.mkdir()
    lock = (REPO_NODE_IMAGE / "versions.lock").read_text(encoding="ascii").splitlines()
    sha = hashlib.sha256(pinned).hexdigest()
    lock = [f"AGENT_SHA256={sha}" if line.startswith("AGENT_SHA256=") else line for line in lock]
    (image / "versions.lock").write_text("\n".join(lock) + "\n", encoding="ascii")
    monkeypatch.setenv("PROXYHUB_NODE_IMAGE_DIR", str(image))
    get_live_settings.cache_clear()
    binary = tmp_path / "proxyhub-agent"
    binary.write_bytes(pinned)
    os.utime(binary, ns=(1_000_000_000, 1_000_000_000))

    try:
        _check_artifact_reuse(artifacts, binary, pinned, sha)
    finally:
        get_live_settings.cache_clear()


def _check_artifact_reuse(artifacts: Any, binary: Path, pinned: bytes, sha: str) -> None:
    first = artifacts._load(binary)
    assert first.sha256 == sha and first.data == pinned
    assert artifacts._load(binary) is first  # served from memory, not hashed again

    # Same size, other bytes, new mtime: verified again and refused.
    binary.write_bytes(os.urandom(4096))
    os.utime(binary, ns=(2_000_000_000, 2_000_000_000))
    with pytest.raises(artifacts.ArtifactUnavailable) as info:
        artifacts._load(binary)
    assert info.value.code == "artifact_hash_mismatch"

    # The pinned bytes again: accepted again (a new verification, not the old object).
    binary.write_bytes(pinned)
    os.utime(binary, ns=(3_000_000_000, 3_000_000_000))
    again = artifacts._load(binary)
    assert again.data == pinned and again is not first


# --- operator CLI (python -m proxyhub.nodes), against the real database ------------------------------


@pytest.fixture
async def world(
    migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    async with live_world(migrated_db, tmp_path, monkeypatch) as w:
        yield w


def cli(*args: str) -> tuple[int, object]:
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "proxyhub.nodes", *args],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (done.stdout, done.stderr)
    return done.returncode, json.loads(lines[0])


@pytest.mark.db
@pytest.mark.redis
async def test_cli_uses_the_same_gates_as_the_api(world: LiveWorld) -> None:
    code, out = cli("test-node")  # no settings yet: kill switch on, no budget cap (node cap 10, D14)
    assert code == 1 and isinstance(out, dict) and out["error"] == "gate_refused"
    assert {"kill_switch_on", "budget_cap_not_set"} <= set(out["reasons"])
    await world.settings(good_settings())
    code, out = cli("test-node", "--location", "nbg1")
    assert code == 0 and isinstance(out, dict)
    node_id = out["node_id"]
    code, again = cli("test-node")
    assert code == 1 and again == {"error": "test_node_exists"}
    code, listed = cli("list")
    assert code == 0 and isinstance(listed, list) and listed[0]["id"] == node_id
    assert listed[0]["location"] == "nbg1" and listed[0]["state"] == "requested"
    code, waited = cli("wait", node_id, "requested", "--timeout", "5")
    assert code == 0 and waited == {"node_id": node_id, "state": "requested"}
    code, out = cli("retire", node_id)
    assert code == 0 and out == {"node_id": node_id, "state": "deleted"}
    code, events = cli("events", node_id)
    assert code == 0 and isinstance(events, list) and events[-1]["kind"] == "requested"
    code, out = cli("events", str(uuid.uuid4()))
    assert code == 1 and out == {"error": "not_found"}
    log = await world.rows("SELECT DISTINCT actor FROM actions_log WHERE action = 'test_node_request'")
    assert log == [("cli",)]


# --- architecture --------------------------------------------------------------------------------------


def _names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("hetzner.mutations"):
            found.add(f"import {node.module}")
        if isinstance(node, ast.Import) and any(a.name.endswith("hetzner.mutations") for a in node.names):
            found.add("import mutations")
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in MUTATION_NAMES:
                found.add(name)
    return found


def test_only_the_executor_reaches_server_mutations_and_permits() -> None:
    offenders = {}
    for path in PANEL.rglob("*.py"):
        if "migrations" in path.parts or path in ALLOWED:
            continue
        hits = _names(ast.parse(path.read_text(encoding="utf-8")))
        if hits:
            offenders[str(path.relative_to(PANEL))] = sorted(hits)
    assert offenders == {}
    assert _names(ast.parse((PANEL / "nodes" / "executor.py").read_text())) >= MUTATION_NAMES
    # Negative control.
    assert _names(ast.parse("from proxyhub.hetzner.mutations import delete_server\ndelete_server(r, p)\n"))


def test_executor_checks_the_owners_dry_run_for_slot_nodes() -> None:
    """The executor reads the owner's settings itself (fleet_dry_run) before any slot-node create
    or delete; it never consults (or could flip) the build-time switch of the fleet loop."""
    assert fleet_loop.FLEET_LIVE_ENABLED is True
    source = (PANEL / "nodes" / "executor.py").read_text()
    assert source.count('"fleet_dry_run"') == 2  # once for creations, once for deletions
    assert "_fleet_blocked(s)" in source
    used = {n.id if isinstance(n, ast.Name) else n.attr for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Name | ast.Attribute)}  # fmt: skip
    assert "FLEET_LIVE_ENABLED" not in used
    assert not hasattr(ex, "FleetNotLive")


def test_no_log_call_in_the_node_package_carries_user_data_or_tokens() -> None:
    """Rule 3: the user data holds the registration token; it may reach Hetzner, never a log."""
    for path in (PANEL / "nodes").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"log", "logger", "logging"}
            ):
                text = ast.unparse(node)
                for word in ("user_data", "token", "spec", "reveal", "UserData"):
                    assert word not in text, f"{path.name}: {text}"


# --- the worker loop ----------------------------------------------------------------------------------


def test_worker_runs_the_nodes_loop() -> None:
    from proxyhub.reconcilers.nodes import NodesReconciler
    from proxyhub.worker import reconcilers

    loops = {r.name: r for r in reconcilers()}
    assert isinstance(loops["nodes"], NodesReconciler) and loops["nodes"].interval_seconds == 10


@pytest.mark.db
async def test_nodes_loop_runs_under_its_advisory_lock(world: LiveWorld) -> None:
    from sqlalchemy import text

    from proxyhub.reconcilers.base import lock_key
    from proxyhub.reconcilers.nodes import NodesReconciler

    loop = NodesReconciler(world.executor())
    assert await loop.try_run_once(world.engine) is True
    async with world.engine.connect() as other:  # another worker holds the lock
        await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key("nodes")})
        assert await loop.try_run_once(world.engine) is False
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key("nodes")})
        await other.commit()
    assert await loop.try_run_once(world.engine) is True


# --- deploy wiring (the running behaviour is in test_live_agent_site.py) ---------------------------------

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


@pytest.mark.skipif(not DEPLOY.is_dir(), reason="deploy/ is not in this checkout")
def test_compose_wires_the_agent_site_secret_and_domain() -> None:
    import yaml

    services = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())["services"]
    caddy = services["caddy"]
    assert caddy["environment"]["AGENT_DOMAIN"] == "${AGENT_DOMAIN:-}"
    assert caddy["environment"]["PROXYHUB_AGENT_SITE"] == "${PROXYHUB_AGENT_SITE:-off}"
    assert caddy["secrets"] == ["agent_proxy_secret"]
    assert "./caddy:/etc/caddy/agent:ro" in caddy["volumes"]
    assert "/var/lib/proxy-hub/caddy-agent:/etc/caddy/agent-trust:ro" in caddy["volumes"]
    assert "agent_proxy_secret" in services["api"]["secrets"]
    assert "agent_proxy_secret" not in services["worker"]["secrets"]  # the worker never needs it
    for name in ("api", "worker"):
        assert services[name]["environment"]["PROXYHUB_AGENT_DOMAIN"] == "${AGENT_DOMAIN:-}"
        contexts = services[name]["build"]["additional_contexts"]
        assert contexts["agent"] == "../agent" and contexts["node-image"] == "../node-image"


@pytest.mark.skipif(not DEPLOY.is_dir(), reason="deploy/ is not in this checkout")
def test_deployer_exports_the_ca_and_enables_the_site_only_with_an_agent_domain() -> None:
    text = (DEPLOY / "deployer.sh").read_text()
    assert 'if [ -n "${AGENT_DOMAIN:-}" ]; then export_agent_ca || return 1; fi' in text
    assert "compose run --rm --no-deps -T api python -m proxyhub.pki ca-pem" in text
    assert 'PROXYHUB_AGENT_SITE="$(agent_site)"' in text and 'AGENT_DOMAIN="${AGENT_DOMAIN:-}"' in text
    caddyfile = (DEPLOY / "Caddyfile").read_text()
    assert "import /etc/caddy/agent/agent-{$PROXYHUB_AGENT_SITE:off}.caddy" in caddyfile
    off = (DEPLOY / "caddy" / "agent-off.caddy").read_text()
    assert all(line.startswith("#") for line in off.splitlines() if line.strip())  # nothing is served


def test_dockerfile_builds_the_pinned_agent_and_checks_its_hash() -> None:
    text = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "ARG GO_IMAGE=golang:1.24.7-bookworm@sha256:" in text
    assert "RUN bash /src/node-image/build-agent.sh /out/proxyhub-agent" in text
    assert "AGENT_SHA256" in text
    assert "COPY --from=agent-build /out/proxyhub-agent /app/artifacts/proxyhub-agent" in text
