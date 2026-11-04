"""The Reality policy: which configs end, and when a fresh one is made (docs/10 section 3.14).

Pure decisions here (:func:`end_status`, :func:`creation_due`, :func:`eligible_for_new_configs`),
database steps in :func:`end_configs` and :func:`create_configs`; the control loop
(proxyhub/reconcilers/reality.py) runs them. Nothing here touches Hetzner: a config is a user on
servers that already exist, so the fleet caps and the kill switch (servers, money) are not
involved; the owner's own switch is ``RealitySettings.enabled``.

* A config ends as ``full`` (its data cap), ``expired`` (its time), or ``burned`` (its link can no
  longer work: an IP link whose server left service, a slot link whose domain is no longer the
  active one, or a scope no server serves any more). The node also enforces cap and time by itself.
* A scope (a slot, or a slotless node) keeps ``configs_per_node`` active configs, started half a
  lifetime apart (two per day by default), on its newest ready, healthy node.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.cloudflare.rotation import current_active_domain
from proxyhub.cloudflare.service import slot_name
from proxyhub.db.models_reality import RealityConfig, RealityKey
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.reality.keys import new_key_pair, new_short_id
from proxyhub.reality.settings import RealitySettings
from proxyhub.reality.store import (
    SERVE_STATES,
    NodeView,
    StatusView,
    all_statuses,
    is_ip_host,
    live_nodes,
    telemt_clash,
)
from proxyhub.vault import Vault

S = NodeState
EndStatus = Literal["expired", "full", "burned"]
# An IP link only works on its own server, while that server is in service.
IP_SERVING: Final = frozenset({S.READY, S.UNHEALTHY, S.REPAIRING})
# A node's agent must have reported this recently for the node to get a new config.
REPORT_FRESH: Final = timedelta(minutes=10)
MAX_CREATIONS_PER_TICK: Final = 20


@dataclass(frozen=True)
class ActiveConfig:
    id: int
    node_id: uuid.UUID
    slot: int | None
    key_id: int
    host: str
    created_at: datetime
    expires_at: datetime
    data_cap_bytes: int
    used_bytes: int


def end_status(
    c: ActiveConfig,
    *,
    now: datetime,
    own_node_state: str | None,
    scope_served: bool,
    active_domain: str | None,
) -> EndStatus | None:
    """Why an active config ends now, or None while it still works."""
    if c.used_bytes >= c.data_cap_bytes:
        return "full"
    if now >= c.expires_at:
        return "expired"
    if not scope_served:
        return "burned"
    if is_ip_host(c.host):
        if own_node_state is None or S(own_node_state) not in IP_SERVING:
            return "burned"
    elif c.slot is None or active_domain is None or c.host != slot_name(c.slot, active_domain):
        return "burned"
    return None


def creation_due(active_created: Sequence[datetime], *, now: datetime, settings: RealitySettings) -> bool:
    """A scope gets a fresh config when it has none, when two or more are missing, or when its
    newest one is half a lifetime old (with two per scope: one every 12 hours by default)."""
    n, want = len(active_created), settings.configs_per_node
    if n >= want:
        return False
    if n == 0 or n <= want - 2:
        return True
    stagger = timedelta(hours=settings.cap_hours) / want
    return now - max(active_created) >= stagger


def eligible_for_new_configs(
    node: NodeView, status: StatusView | None, *, now: datetime, telemt_clashes: bool
) -> bool:
    """A node may receive a new config: ready, with an IPv4, its agent manages Xray and reported
    lately without an Xray error, and the REALITY target did not fail its check from there."""
    return (
        node.state == S.READY.value
        and node.ipv4 is not None
        and status is not None
        and status.enabled
        and status.reported_at >= now - REPORT_FRESH
        and status.apply_error is None
        and status.target_ok is not False
        and not telemt_clashes
    )


def _newest_ready(node: NodeView) -> tuple[datetime, str]:
    return (node.ready_at or datetime.min.replace(tzinfo=UTC), str(node.id))


def scope_of(node: NodeView) -> tuple[str, int | uuid.UUID]:
    return ("slot", node.slot) if node.slot is not None else ("node", node.id)


def scope_secret_name(node: NodeView) -> str:
    kind, value = scope_of(node)
    return f"reality:key:{kind}:{value}"


# --- database steps ------------------------------------------------------------------------------


async def _active(conn: AsyncConnection) -> list[ActiveConfig]:
    rows = (
        await conn.execute(
            select(
                RealityConfig.id,
                RealityConfig.node_id,
                RealityConfig.slot,
                RealityConfig.key_id,
                RealityConfig.host,
                RealityConfig.created_at,
                RealityConfig.expires_at,
                RealityConfig.data_cap_bytes,
                RealityConfig.used_bytes,
            ).where(RealityConfig.status == "active")
        )
    ).all()
    return [ActiveConfig(*r) for r in rows]


async def end_configs(conn: AsyncConnection, now: datetime) -> dict[int, EndStatus]:
    """End every active config that no longer works. Returns config id -> new status."""
    configs = await _active(conn)
    if not configs:
        return {}
    nodes = {n.id: n for n in await live_nodes(conn)}
    keys = {
        k.id: (k.slot, k.node_id)
        for k in (await conn.execute(select(RealityKey.id, RealityKey.slot, RealityKey.node_id))).all()
    }
    serving_slots = {n.slot for n in nodes.values() if n.slot is not None and S(n.state) in SERVE_STATES}
    serving_nodes = {n.id for n in nodes.values() if S(n.state) in SERVE_STATES}
    domain = await current_active_domain(conn)
    ended: dict[int, EndStatus] = {}
    for c in configs:
        key_slot, key_node = keys.get(c.key_id, (None, None))
        served = key_slot in serving_slots if key_slot is not None else key_node in serving_nodes
        own = nodes.get(c.node_id)
        why = end_status(
            c, now=now, own_node_state=own.state if own else None, scope_served=served, active_domain=domain
        )
        if why is not None:
            ended[c.id] = why
    for status in ("full", "expired", "burned"):
        ids = [i for i, s in ended.items() if s == status]
        if ids:
            await conn.execute(
                update(RealityConfig)
                .where(RealityConfig.id.in_(ids), RealityConfig.status == "active")
                .values(status=status, ended_at=now)
            )
    return ended


@dataclass(frozen=True)
class Created:
    config_id: int
    node_id: uuid.UUID
    slot: int | None
    host: str
    expires_at: datetime
    key_created: bool


async def _scope_key(
    conn: AsyncConnection, node: NodeView, settings: RealitySettings, vault_factory: VaultFactory
) -> tuple[int, bool]:
    kind, value = scope_of(node)
    where = RealityKey.slot == value if kind == "slot" else RealityKey.node_id == value
    key_id = (await conn.execute(select(RealityKey.id).where(where))).scalar_one_or_none()
    if key_id is not None:
        return int(key_id), False
    pair = new_key_pair()
    name = scope_secret_name(node)
    await vault_factory().put(conn, name, pair.private)
    new_id = (
        await conn.execute(
            insert(RealityKey)
            .values(
                slot=value if kind == "slot" else None,
                node_id=value if kind == "node" else None,
                public_key=pair.public,
                short_id=new_short_id(),
                target=settings.target,
                server_name=settings.target_host,
                secret_name=name,
            )
            .returning(RealityKey.id)
        )
    ).scalar_one()
    return int(new_id), True


class VaultFactory:
    """Opens the vault on first use only (ending configs needs no key material)."""

    def __init__(self) -> None:
        self._vault: Vault | None = None

    def __call__(self) -> Vault:
        if self._vault is None:
            self._vault = Vault.from_config()
        return self._vault


async def create_configs(
    conn: AsyncConnection,
    settings: RealitySettings,
    now: datetime,
    vault_factory: VaultFactory | None = None,
) -> list[Created]:
    """At most one fresh config per due scope (and at most MAX_CREATIONS_PER_TICK in all)."""
    vaults = vault_factory or VaultFactory()
    statuses = await all_statuses(conn)
    by_scope: dict[tuple[str, int | uuid.UUID], list[NodeView]] = {}
    for node in await live_nodes(conn):
        if eligible_for_new_configs(
            node, statuses.get(node.id), now=now, telemt_clashes=await telemt_clash(conn, node.id)
        ):
            by_scope.setdefault(scope_of(node), []).append(node)
    if not by_scope:
        return []
    domain = await current_active_domain(conn)
    created: list[Created] = []
    for scope in sorted(by_scope, key=str):
        if len(created) >= MAX_CREATIONS_PER_TICK:
            break
        # The scope's newest ready node: the one the slot name points to (DNS picks it too).
        node = max(
            by_scope[scope], key=lambda n: (n.ready_at or datetime.min.replace(tzinfo=now.tzinfo), str(n.id))
        )
        key_id, key_created = await _scope_key(conn, node, settings, vaults)
        active_created = list(
            (
                await conn.execute(
                    select(RealityConfig.created_at).where(
                        RealityConfig.key_id == key_id, RealityConfig.status == "active"
                    )
                )
            )
            .scalars()
            .all()
        )
        if not creation_due(active_created, now=now, settings=settings):
            continue
        assert node.ipv4 is not None  # eligible_for_new_configs
        host = slot_name(node.slot, domain) if node.slot is not None and domain else node.ipv4
        expires = now + timedelta(hours=settings.cap_hours)
        config_id = (
            await conn.execute(
                insert(RealityConfig)
                .values(
                    node_id=node.id,
                    slot=node.slot,
                    key_id=key_id,
                    uuid=uuid.uuid4(),
                    host=host,
                    created_at=now,
                    expires_at=expires,
                    data_cap_bytes=settings.cap_bytes,
                    used_bytes=0,
                    status="active",
                )
                .returning(RealityConfig.id)
            )
        ).scalar_one()
        created.append(
            Created(
                config_id=int(config_id),
                node_id=node.id,
                slot=node.slot,
                host=host,
                expires_at=expires,
                key_created=key_created,
            )
        )
    return created
