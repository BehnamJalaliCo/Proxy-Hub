"""Helpers for the live-fleet tests (round 3; no tests in this module).

Everything is real: PostgreSQL, the fleet loop, the node executor. Nothing imitates Hetzner (D10):
the fleet loop never calls it, and the executor tests either stop before the first Hetzner call
(``NoHetzner`` fails the test if it is reached) or point a REAL hcloud client at a closed local port.
"""

from __future__ import annotations

import json
import socket
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hcloud import Client
from test_live_support import LiveWorld, good_settings

from proxyhub.hetzner.client import HetznerReader
from proxyhub.reconcilers.fleet import FleetReconciler
from proxyhub.settings_schema import FleetSettings

# Addresses in Hetzner's own ranges (our servers'; never a user's). Only used as row data here.
IP_A = "49.13.10.20"
IP_A_SAME_24 = "49.13.10.77"
IP_B = "116.203.40.5"


def live_settings(**overrides: Any) -> FleetSettings:
    """The owner turned the fleet live: dry-run off, kill switch off, both caps set (D32: 150 EUR)."""
    values: dict[str, Any] = {
        "dry_run": False,
        "kill_switch": False,
        "budget_cap_eur": Decimal("150.00"),
        "node_cap_total": 10,
        "creations_per_hour_cap": 3,
    }
    values.update(overrides)
    return good_settings(**values)


def ago(**kw: float) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


async def add_slot_node(
    world: LiveWorld,
    slot: int | None,
    *,
    state: str = "ready",
    location: str = "fsn1",
    ipv4: str | None = None,
    hetzner_id: int | None = None,
    created_at: datetime | None = None,
    ready_at: datetime | None = None,
    state_since: datetime | None = None,
    observed: bool = True,
    deleted_at: datetime | None = None,
    retire_reason: str | None = None,
    create_attempted: bool = True,
) -> uuid.UUID:
    """A slot node row as the executor leaves it, plus (``observed``) the account guard's last look
    at its server (labels managed-by=proxy-hub, role=node) so the delete gate can pass."""
    node_id = uuid.uuid4()
    created = created_at or ago(days=3)
    if state in ("ready", "unhealthy", "burned") and ready_at is None:
        ready_at = created + timedelta(minutes=5)
    await world.rows(
        "INSERT INTO nodes (id, account_id, name, slot, location, server_type, state, is_test, hetzner_id, "
        "ipv4, created_at, updated_at, ready_at, deleted_at) VALUES (:id, :acc, :name, :slot, :loc, 'cpx02', "
        ":state, false, :h, CAST(:ip AS inet), :c, :c, :ready, :deleted)",
        id=node_id,
        acc=world.account_id,
        name=f"ph-s{slot}-{node_id.hex[:8]}",
        slot=slot,
        loc=location,
        state=state,
        h=hetzner_id,
        ip=ipv4,
        c=created,
        ready=ready_at,
        deleted=deleted_at,
    )
    await world.rows(
        "INSERT INTO node_operations (node_id, requested_by, requested_at, state_since, create_attempts, "
        "create_attempted_at, delete_attempts, retire_requested_at, retire_reason) VALUES (:n, 'fleet', :c, "
        ":since, 1, :attempted, 0, :retired, :why)",
        n=node_id,
        c=created,
        since=state_since or ready_at or created,
        attempted=created if create_attempted else None,
        retired=created if retire_reason else None,
        why=retire_reason,
    )
    if observed and hetzner_id is not None:
        await world.rows(
            "INSERT INTO hetzner_account_servers (account_id, hetzner_id, name, labels, delete_protected, "
            "seen_at) VALUES (:acc, :h, :name, CAST(:labels AS jsonb), false, now())",
            acc=world.account_id,
            h=hetzner_id,
            name=f"ph-s{slot}-{node_id.hex[:8]}",
            labels=json.dumps({"managed-by": "proxy-hub", "role": "node", "node-id": str(node_id)}),
        )
    return node_id


async def full_fleet(world: LiveWorld, slots: int = 9, *, skip: tuple[int, ...] = ()) -> dict[int, uuid.UUID]:
    """Nine healthy slot nodes spread 3/3/3 over fsn1, nbg1, hel1 (as the planner places them)."""
    locs = ("fsn1", "nbg1", "hel1")
    out: dict[int, uuid.UUID] = {}
    for slot in range(1, slots + 1):
        if slot in skip:
            continue
        out[slot] = await add_slot_node(
            world,
            slot,
            location=locs[(slot - 1) % 3],
            ipv4=f"49.12.{slot}.{10 + slot}",
            hetzner_id=5000 + slot,
        )
    return out


async def set_verdict(
    world: LiveWorld, node_id: uuid.UUID, verdict: str, at: datetime | None = None, *, serving: bool = True
) -> None:
    """The probes' verdict for a node. ``serving``: the node's Telemt config exists and its agent's
    newest report says Telemt is up, so a "blocked" verdict is about Iran, not a closed port."""
    await world.rows(
        "INSERT INTO probe_node_state (node_id, verdict, verdict_at, verdict_reasons) VALUES "
        "(:n, :v, :at, '[]') ON CONFLICT (node_id) DO UPDATE SET verdict = :v, verdict_at = :at",
        n=node_id,
        v=verdict,
        at=at or datetime.now(UTC),
    )
    await world.rows(
        "INSERT INTO telemt_node_configs (node_id, ports, tls_domain, mode_classic, mode_secure, mode_tls, "
        "updated_at) VALUES (:n, '{8443}', 'www.microsoft.com', false, false, true, now()) "
        "ON CONFLICT (node_id) DO NOTHING",
        n=node_id,
    )
    await world.rows(
        "INSERT INTO agent_node_reports (node_id, at, apply_ok, telemt_up, users_reported) "
        "VALUES (:n, now(), true, :up, 1)",
        n=node_id,
        up=serving,
    )


async def run_fleet(world: LiveWorld) -> None:
    assert await FleetReconciler().try_run_once(world.engine)


async def slot_rows(world: LiveWorld) -> list[Any]:
    return await world.rows(
        "SELECT n.id, n.name, n.slot, n.location, n.state, o.retire_reason, o.requested_by FROM nodes n "
        "LEFT JOIN node_operations o ON o.node_id = n.id WHERE NOT n.is_test ORDER BY n.slot, n.created_at"
    )


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def closed_port_reader(token: str) -> HetznerReader:
    """A REAL hcloud client whose API endpoint is a local port nothing listens on: every Hetzner
    call fails the way an unreachable API does (no imitation of Hetzner's answers)."""
    reader = HetznerReader(token)
    reader._client = Client(token=token, api_endpoint=f"http://127.0.0.1:{closed_port()}/v1", timeout=(2, 2))
    return reader
