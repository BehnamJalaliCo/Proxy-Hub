"""The LIVE fleet against the REAL Hetzner node account (marker ``hetzner``; panel server only, D10).

Same environment as tests/test_live_hetzner.py (PROXYHUB_HETZNER_TEST_TOKEN_FILE,
PROXYHUB_LIVE_TEST_AGENT_DOMAIN). One cpx02 slot server (about EUR 0.01 per started hour): the fleet
loop requests slot 1 in live mode, the executor creates it through the gates and a CreatePermit;
then the owner lowers the target to 0, the fleet loop retires it as excess and the executor deletes
it through can_delete on a live read and a DeletePermit. Nothing is deleted any other way.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from test_fleet_live_support import live_settings
from test_fleet_support import assert_absent_from_database
from test_live_hetzner import agent_domain, real_world, token, wait_gone  # noqa: F401 - fixtures
from test_live_support import LiveWorld, requires_node_image

from proxyhub.accounts.hetzner_io import call_hetzner, observe_servers
from proxyhub.accounts.service import store_observation
from proxyhub.hetzner.client import HetznerReader
from proxyhub.nodes.executor import NodeExecutor
from proxyhub.pki.ca import build_ca_certificate, to_pem
from proxyhub.reconcilers.fleet import FleetReconciler

pytestmark = [pytest.mark.hetzner, pytest.mark.db, pytest.mark.redis]


async def observe(world: LiveWorld, hcloud_token: str) -> None:
    """What the account guard loop does every 10 minutes: a live look at ALL servers (rule 4)."""
    reader = HetznerReader(hcloud_token)
    obs = await call_hetzner(lambda: observe_servers(reader))
    assert obs.guard.allowed, "the node project holds a server we do not manage (rule 4)"
    async with world.engine.begin() as conn:
        await store_observation(conn, world.account_id, obs, datetime.now(UTC))


@requires_node_image
async def test_the_live_fleet_creates_and_retires_one_real_slot_server(
    real_world: LiveWorld,  # noqa: F811
    token: str,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    world = real_world
    await world.settings(live_settings(target_slots=1, node_cap_total=50, budget_cap_eur="100.00"))
    ca_key = ec.generate_private_key(ec.SECP256R1())
    executor = NodeExecutor(
        vault_factory=lambda: world.vault, ca_pem=lambda: to_pem(build_ca_certificate(ca_key))
    )
    node_id: uuid.UUID | None = None
    try:
        assert await FleetReconciler().try_run_once(world.engine)
        (row,) = await world.rows("SELECT id, name, location FROM nodes WHERE slot = 1 AND NOT is_test")
        node_id = row.id
        await executor.run_once(world.engine)
        node = await world.node(node_id)
        assert node["state"] == "booting", (node, await world.events(node_id))
        server = HetznerReader(token).raw.servers.get_by_id(node["hetzner_id"])
        assert dict(server.labels) == {"managed-by": "proxy-hub", "role": "node", "node-id": str(node_id)}
        assert server.name == row.name and server.name.startswith("ph-s1-")
        assert server.datacenter.location.name == row.location
        ips = await world.rows("SELECT ip FROM ip_history WHERE node_id = :n", n=node_id)
        assert [str(r.ip) for r in ips] == [str(server.public_net.ipv4.ip)]  # our server's own address

        # The owner lowers the target to 0: the slot server is excess; the loop retires it once the
        # account guard has seen the server (a live look, as its loop does every 10 minutes).
        await observe(world, token)
        await world.settings(live_settings(target_slots=0, node_cap_total=50, budget_cap_eur="100.00"))
        assert await FleetReconciler().try_run_once(world.engine)
        node = await world.node(node_id)
        assert node["state"] == "retiring" and node["retire_reason"] == "excess"
        await executor.run_once(world.engine)
        assert (await world.node(node_id))["state"] == "deleted", await world.events(node_id)
        await wait_gone(token, str(node_id))
    finally:
        if node_id is not None and (await world.node(node_id))["state"] != "deleted":
            await observe(world, token)
            await world.settings(live_settings(target_slots=0, node_cap_total=50, budget_cap_eur="100.00"))
            for _ in range(3):
                await FleetReconciler().try_run_once(world.engine)
                await executor.run_once(world.engine)
    log = await world.rows(
        "SELECT actor, action, allowed FROM actions_log WHERE actor IN ('fleet', 'node_executor')"
    )
    acts = {(r.actor, r.action, r.allowed) for r in log}
    assert {("fleet", "node_request", True), ("node_executor", "node_create", True)} <= acts
    assert {("fleet", "node_retire", True), ("node_executor", "node_delete", True)} <= acts
    await assert_absent_from_database(world.engine, token)
    assert token not in caplog.text and "#cloud-config" not in caplog.text
