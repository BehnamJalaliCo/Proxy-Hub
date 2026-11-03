"""Against the REAL Hetzner node account (marker ``hetzner``; runs only on the panel server, D10).

Environment:
  PROXYHUB_HETZNER_TEST_TOKEN_FILE   file (600) with the Read & Write token of the node project
                                     (the same project the owner added in the dashboard).
  PROXYHUB_LIVE_TEST_AGENT_DOMAIN    the agent site the deployed panel serves (e.g. agent.<domain>).
  PROXYHUB_LIVE_TEST_E2E=1           also run the end-to-end test through the DEPLOYED panel
                                     (its worker creates the node, the node registers through the
                                     production agent site). Optional:
  PROXYHUB_LIVE_TEST_COMPOSE_FILE    default /opt/proxy-hub/deploy/docker-compose.yml

Each test creates at most ONE cpx02 test server (about EUR 0.01 per started hour) and deletes it
through the delete gate; at the end nothing labelled purpose=test may be left in the project.
Nothing here deletes a server any other way: if a test fails half-way, rerun it or use
``python -m proxyhub.nodes retire`` (the same gates).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_fleet_support import assert_absent_from_database, make_vault
from test_live_support import NODE_IMAGE, LiveWorld, NoHetzner, api_client, clean_live_tables, good_settings

from proxyhub.accounts.hetzner_io import call_hetzner, observe_servers, read_offers
from proxyhub.accounts.service import store_observation, store_offers
from proxyhub.config import read_secret_file
from proxyhub.hetzner.client import HetznerReader
from proxyhub.nodes.executor import NodeExecutor
from proxyhub.nodes.image import read_versions_lock
from proxyhub.nodes.settings import get_live_settings
from proxyhub.pki.ca import build_ca_certificate, to_pem
from proxyhub.vault import hcloud_secret_name

pytestmark = pytest.mark.hetzner

TEST_SELECTOR = "managed-by=proxy-hub,purpose=test"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set")
    return value


@pytest.fixture
def token() -> str:
    return read_secret_file(Path(_env("PROXYHUB_HETZNER_TEST_TOKEN_FILE")))


@pytest.fixture
def agent_domain() -> str:
    return _env("PROXYHUB_LIVE_TEST_AGENT_DOMAIN")


def our_test_servers(token: str) -> list[Any]:
    return list(HetznerReader(token).raw.servers.get_all(label_selector=TEST_SELECTOR))


async def wait_gone(token: str, node_id: str, limit_s: float = 180) -> None:
    reader = HetznerReader(token)
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        left = await call_hetzner(lambda: reader.raw.servers.get_all(label_selector=f"node-id={node_id}"))
        if not left:
            return
        await asyncio.sleep(5)
    pytest.fail(f"server of node {node_id} still exists after {limit_s}s")


@pytest.fixture
async def real_world(
    migrated_db: str, tmp_path: Path, token: str, agent_domain: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveWorld]:
    """The test database with the REAL node account: token in the vault, a live guard observation
    and live prices read now with that token."""
    monkeypatch.setenv("PROXYHUB_AGENT_DOMAIN", agent_domain)
    get_live_settings.cache_clear()
    engine = create_async_engine(migrated_db)
    await clean_live_tables(engine)
    (tmp_path / "vault").mkdir()
    vault = make_vault(tmp_path / "vault")
    reader = HetznerReader(token)
    obs = await call_hetzner(lambda: observe_servers(reader))
    assert obs.guard.allowed, "the node project holds a server we do not manage (rule 4)"
    if any(s.labels.get("purpose") == "test" for s in obs.managed):
        pytest.fail("a test server of ours already exists; retire it first (python -m proxyhub.nodes retire)")
    offers = await call_hetzner(lambda: read_offers(reader))
    account_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
                "VALUES (:id, 'real-node-account', :s, 'active')"
            ),
            {"id": account_id, "s": hcloud_secret_name(account_id)},
        )
        await vault.put(conn, hcloud_secret_name(account_id), token)
        await store_observation(conn, account_id, obs, now)
        await store_offers(conn, account_id, offers, now)
    try:
        yield LiveWorld(engine, vault, tmp_path, account_id, token, NoHetzner())
    finally:
        await clean_live_tables(engine)
        await engine.dispose()
        get_live_settings.cache_clear()


async def test_executor_creates_and_retires_one_real_test_server(
    real_world: LiveWorld, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    """In-process executor, test database, real Hetzner: requested -> creating -> booting (a real
    cpx02 with our labels and user data) -> retiring -> deleted through can_delete + DeletePermit.
    The node cannot register here (its token lives in this test database, not the deployed panel's);
    the end-to-end READY test is the next one."""
    caplog.set_level(logging.DEBUG)
    world = real_world
    await world.settings(good_settings(node_cap_total=50, budget_cap_eur="100.00"))
    ca_key = ec.generate_private_key(ec.SECP256R1())
    executor = NodeExecutor(
        vault_factory=lambda: world.vault, ca_pem=lambda: to_pem(build_ca_certificate(ca_key))
    )
    node_id: uuid.UUID | None = None
    try:
        async with api_client(world, reader_factory=HetznerReader) as api:
            r = await api.post("/api/nodes/test", json={"location": "fsn1"})
            assert r.status_code == 202, r.text
            node_id = uuid.UUID(r.json()["node_id"])
            await executor.run_once(world.engine)
            node = await world.node(node_id)
            assert node["state"] == "booting", (node, await world.events(node_id))
            hetzner_id = node["hetzner_id"]
            server = HetznerReader(token).raw.servers.get_by_id(hetzner_id)
            assert dict(server.labels) == {
                "managed-by": "proxy-hub",
                "role": "node",
                "node-id": str(node_id),
                "purpose": "test",
            }
            assert server.server_type.name == "cpx02" and server.name == f"ph-test-{node_id.hex[:8]}"
            assert server.image is None or server.image.name == "ubuntu-24.04"
            listed = {n["id"]: n for n in (await api.get("/api/nodes")).json()}
            assert listed[str(node_id)]["ipv4"]  # our server's address
            # Second pass: nothing is created twice.
            await executor.run_once(world.engine)
            assert len(our_test_servers(token)) == 1

            r = await api.post(f"/api/nodes/{node_id}/retire")
            assert r.status_code == 202, r.text
        await executor.run_once(world.engine)
        node = await world.node(node_id)
        assert node["state"] == "deleted", (node, await world.events(node_id))
        await wait_gone(token, str(node_id))
    finally:
        if node_id is not None and (await world.node(node_id))["state"] != "deleted":
            async with api_client(world, reader_factory=HetznerReader) as api:
                await api.post(f"/api/nodes/{node_id}/retire")
            await executor.run_once(world.engine)
    assert our_test_servers(token) == []
    log = await world.rows(
        "SELECT action, allowed FROM actions_log WHERE actor = 'node_executor' ORDER BY id"
    )
    assert [tuple(r) for r in log] == [("node_create", True), ("node_delete", True)]
    await assert_absent_from_database(world.engine, token)
    assert token not in caplog.text and "#cloud-config" not in caplog.text


# --- end to end through the deployed panel ------------------------------------------------------


def compose(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """``python -m proxyhub.nodes ...`` inside the deployed api container (same gates as the API)."""
    file = os.environ.get("PROXYHUB_LIVE_TEST_COMPOSE_FILE", "/opt/proxy-hub/deploy/docker-compose.yml")
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("needs docker (the deployed panel's containers)")
    argv = [docker, "compose", "-p", "proxyhub", "-f", file, "exec", "-T", "api"]
    return subprocess.run(  # noqa: S603 - fixed argv
        [*argv, "python", "-m", "proxyhub.nodes", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


async def test_real_test_node_reaches_ready_through_the_deployed_agent_site(
    token: str, agent_domain: str
) -> None:
    if os.environ.get("PROXYHUB_LIVE_TEST_E2E") != "1":
        pytest.skip("set PROXYHUB_LIVE_TEST_E2E=1 to use the deployed panel (it creates one real node)")
    base = f"https://{agent_domain}"
    lock = read_versions_lock(NODE_IMAGE)
    async with httpx.AsyncClient(trust_env=False, timeout=30) as c:  # public ACME certificate
        probe = {"token": "A" * 43, "csr_pem": "x", "agent_version": "0.1.0", "telemt_version": None}
        r = await c.post(
            base + "/agent/v1/register", json=probe, headers={"X-ProxyHub-Agent-Proxy-Secret": "x"}
        )
        assert r.status_code in (401, 422), r.text  # reached the API with Caddy's secret (else 403)
        r = await c.get(base + "/agent/v1/desired", headers={"X-ProxyHub-Client-Cert-Sha256": "0" * 64})
        assert r.status_code == 401 and r.json() == {"error": "client_certificate_required"}
        r = await c.get(base + "/agent/v1/artifacts/proxyhub-agent")
        assert r.status_code == 200 and hashlib.sha256(r.content).hexdigest() == lock["AGENT_SHA256"]
        assert (await c.get(base + "/api/nodes")).status_code == 404

    assert our_test_servers(token) == [], "a test server already exists"
    created = compose("test-node")
    assert created.returncode == 0, created.stdout + created.stderr
    node_id = json.loads(created.stdout)["node_id"]
    try:
        ready = compose("wait", node_id, "ready", "--timeout", "1500", timeout=1600)
        assert ready.returncode == 0, (ready.stdout, compose("events", node_id).stdout)
        servers = our_test_servers(token)
        assert [s.labels.get("node-id") for s in servers] == [node_id]
        nodes = {n["id"]: n for n in json.loads(compose("list").stdout)}
        agent = nodes[node_id]["agent"]
        assert agent["registered"] and agent["telemt_version"] == lock["TELEMT_VERSION"]
    finally:
        retired = compose("retire", node_id)
        assert retired.returncode == 0 or "node_deleted" in retired.stdout, retired.stdout + retired.stderr
        done = compose("wait", node_id, "deleted", "--timeout", "600", timeout=700)
        assert done.returncode == 0, (done.stdout, compose("events", node_id).stdout)
    await wait_gone(token, node_id)
    assert our_test_servers(token) == []
    events = json.loads(compose("events", node_id).stdout)
    kinds = [e["kind"] for e in events]
    assert "server_created" in kinds and "server_deleted" in kinds and "agent_registered" in kinds
