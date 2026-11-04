"""The Reality policy: which configs end, which scope follows a new target, and when a fresh config
is made (docs/10 section 3.14).

Pure decisions here (:func:`end_status`, :func:`retarget_decision`, :func:`creation_due`,
:func:`serves_reality`, :func:`eligible_for_new_configs`), database steps in :func:`end_configs`,
:func:`retarget_scopes`, :func:`ensure_keys` and :func:`create_configs`; the control loop
(proxyhub/reconcilers/reality.py) runs them in that order. Nothing here touches Hetzner: a config is
a user on servers that already exist, so the fleet caps and the kill switch (servers, money) are
not involved; the owner's own switch is ``RealitySettings.enabled``.

* A config ends as ``full`` (its data cap), ``expired`` (its time), or ``burned`` (its link can no
  longer work: an IP link whose server left service, a slot link whose domain is no longer the
  active one, a scope no server serves any more, or a target that failed and was replaced). The
  node also enforces cap and time by itself.
* Every scope's key carries the REALITY target and server name its links use (``sni=``). When the
  owner sets another target, :func:`retarget_scopes` moves each scope to it: at once when the scope
  has no active config, or when a node of the scope reports that the old target fails its check
  (those configs cannot work; they end as burned); otherwise once its active configs have ended,
  so links already posted keep working until their own end (no new config is made on the old
  target meanwhile). A switch gives the key a fresh short id; the key pair stays.
* A config is made only on a node whose agent checked the scope's current target and server name
  from the node itself and found TLS 1.3 + HTTP/2 there. A new scope's key is therefore made first
  (:func:`ensure_keys`): the node gets it, checks the target and reports, and the next round makes
  the config. The agent checks again whenever target or server name change, and every 6 hours.
* A scope (a slot, or a slotless node) keeps ``configs_per_node`` active configs, started half a
  lifetime apart (two per day by default), on its newest ready, healthy node.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy import exists, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.cloudflare.rotation import current_active_domain
from proxyhub.cloudflare.service import slot_name
from proxyhub.db.models import Node
from proxyhub.db.models_reality import RealityConfig, RealityKey
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.reality.keys import new_key_pair, new_short_id
from proxyhub.reality.settings import RealitySettings
from proxyhub.reality.store import (
    SERVE_STATES,
    NodeView,
    Scope,
    StatusView,
    all_statuses,
    is_ip_host,
    keys_by_scope,
    live_nodes,
    node_scope,
    target_verdict,
    telemt_clash,
)
from proxyhub.vault import Vault, delete_secret

S = NodeState
EndStatus = Literal["expired", "full", "burned"]
Retarget = Literal["keep", "wait", "switch", "switch_burn"]
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


def retarget_decision(
    *, key_target: tuple[str, str], settings: RealitySettings, active_configs: int, old_target_failing: bool
) -> Retarget:
    """What happens to a scope whose key may carry an older target than the settings.

    ``keep``: the key already has the settings' target. ``switch``: take the new target now (no
    active config uses the old one). ``switch_burn``: the old target fails from the scope's nodes, so
    its active configs cannot work: they end as burned and the new target is taken now. ``wait``:
    the old target works and links of it are active; no new config is made on it, and the switch
    follows once they have ended."""
    if key_target == (settings.target, settings.target_host):
        return "keep"
    if active_configs == 0:
        return "switch"
    if old_target_failing:
        return "switch_burn"
    return "wait"


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


def serves_reality(node: NodeView, status: StatusView | None, *, now: datetime, telemt_clashes: bool) -> bool:
    """A node can serve Reality: ready, with an IPv4, its agent manages Xray and reported lately
    without an Xray error, and its Telemt leaves Xray's ports alone. Such a node's scope gets a key
    (so the node can check the target)."""
    return (
        node.state == S.READY.value
        and node.ipv4 is not None
        and status is not None
        and status.enabled
        and status.reported_at >= now - REPORT_FRESH
        and status.apply_error is None
        and not telemt_clashes
    )


def eligible_for_new_configs(
    node: NodeView,
    status: StatusView | None,
    *,
    now: datetime,
    telemt_clashes: bool,
    target: tuple[str, str],
) -> bool:
    """A node may receive a new config: it serves Reality and its agent checked exactly this
    (target, server name) from the node and it passed (TLS 1.3 + HTTP/2). Not checked yet, failed,
    or checked for another target: no config."""
    return (
        serves_reality(node, status, now=now, telemt_clashes=telemt_clashes)
        and target_verdict(status, *target) is True
    )


scope_of = node_scope


def scope_secret_name(node: NodeView) -> str:
    """The vault name of a scope's private key: ``reality:key:slot:<N>`` or ``reality:key:node:<id>``."""
    kind, value = scope_of(node)
    return f"reality:key:{kind}:{value}"


def scope_label(slot: int | None, node_id: uuid.UUID | None) -> str:
    """The actions_log target of a key scope: ``slot:<N>`` or ``node:<uuid>``."""
    return f"slot:{slot}" if slot is not None else f"node:{node_id}"


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
class Retargeted:
    """A scope that took the settings' target this round."""

    key_id: int
    slot: int | None
    node_id: uuid.UUID | None
    old_target: str
    new_target: str
    # Configs of the old target that ended as burned with it (it failed from the scope's nodes).
    burned: tuple[int, ...]


async def retarget_scopes(
    conn: AsyncConnection, settings: RealitySettings, now: datetime
) -> list[Retargeted]:
    """Move every key scope whose target differs from the settings to the settings' target, as
    :func:`retarget_decision` says. Independent of any node being eligible: a scope whose old
    target fails gets out of that state here, and its nodes then check the new target."""
    wanted = (settings.target, settings.target_host)
    stale = sorted(
        (k for k in (await keys_by_scope(conn)).values() if k.target_pair != wanted), key=lambda k: k.id
    )
    if not stale:
        return []
    nodes = [n for n in await live_nodes(conn) if S(n.state) in SERVE_STATES]
    statuses = await all_statuses(conn)
    out: list[Retargeted] = []
    for k in stale:
        active = list(
            (
                await conn.execute(
                    select(RealityConfig.id)
                    .where(RealityConfig.key_id == k.id, RealityConfig.status == "active")
                    .order_by(RealityConfig.id)
                )
            )
            .scalars()
            .all()
        )
        failing = any(
            target_verdict(statuses.get(n.id), *k.target_pair) is False
            for n in nodes
            if node_scope(n) == k.scope
        )
        decision = retarget_decision(
            key_target=k.target_pair,
            settings=settings,
            active_configs=len(active),
            old_target_failing=failing,
        )
        if decision in ("keep", "wait"):
            continue
        burned: list[int] = []
        if decision == "switch_burn":
            burned = list(
                (
                    await conn.execute(
                        update(RealityConfig)
                        .where(RealityConfig.id.in_(active), RealityConfig.status == "active")
                        .values(status="burned", ended_at=now)
                        .returning(RealityConfig.id)
                    )
                )
                .scalars()
                .all()
            )
        await conn.execute(
            update(RealityKey)
            .where(RealityKey.id == k.id)
            .values(target=settings.target, server_name=settings.target_host, short_id=new_short_id())
        )
        out.append(
            Retargeted(
                key_id=k.id,
                slot=k.slot,
                node_id=k.node_id,
                old_target=k.target,
                new_target=settings.target,
                burned=tuple(sorted(int(i) for i in burned)),
            )
        )
    return out


async def forget_gone_node_keys(conn: AsyncConnection) -> list[uuid.UUID]:
    """Delete the vaulted private key of every node scope (a node without a slot, such as the
    owner's test node) whose node is gone for good and has no active config left. The key row stays
    for the config history; a slot's key is kept, the slot gets new servers. Returns the node ids."""
    rows = (
        await conn.execute(
            select(RealityKey.node_id, RealityKey.secret_name)
            .join(Node, Node.id == RealityKey.node_id)
            .where(
                Node.state == S.DELETED.value,
                ~exists().where(RealityConfig.key_id == RealityKey.id, RealityConfig.status == "active"),
            )
            .order_by(RealityKey.id)
        )
    ).all()
    return [r.node_id for r in rows if await delete_secret(conn, r.secret_name)]


class VaultFactory:
    """Opens the vault on first use only (most rounds make no key)."""

    def __init__(self) -> None:
        self._vault: Vault | None = None

    def __call__(self) -> Vault:
        if self._vault is None:
            self._vault = Vault.from_config()
        return self._vault


async def _serving_scopes(
    conn: AsyncConnection, now: datetime, statuses: dict[uuid.UUID, StatusView]
) -> dict[Scope, list[NodeView]]:
    by_scope: dict[Scope, list[NodeView]] = {}
    for node in await live_nodes(conn):
        if serves_reality(
            node, statuses.get(node.id), now=now, telemt_clashes=await telemt_clash(conn, node.id)
        ):
            by_scope.setdefault(scope_of(node), []).append(node)
    return by_scope


def _newest(nodes: list[NodeView]) -> NodeView:
    """The scope's newest ready node: the one the slot name points to (DNS picks it too)."""
    return max(nodes, key=lambda n: (n.ready_at or datetime.min.replace(tzinfo=UTC), str(n.id)))


@dataclass(frozen=True)
class NewKey:
    key_id: int
    slot: int | None
    node_id: uuid.UUID | None


async def ensure_keys(
    conn: AsyncConnection,
    settings: RealitySettings,
    now: datetime,
    vault_factory: VaultFactory | None = None,
) -> list[NewKey]:
    """A key pair (private key in the vault) with the settings' target for every scope that has a
    node able to serve Reality and no key yet. No config yet: its node first checks the target."""
    vaults = vault_factory or VaultFactory()
    by_scope = await _serving_scopes(conn, now, await all_statuses(conn))
    keys = await keys_by_scope(conn)
    made: list[NewKey] = []
    for scope in sorted(by_scope, key=str):
        if scope in keys:
            continue
        kind, value = scope
        node = _newest(by_scope[scope])
        pair = new_key_pair()
        name = scope_secret_name(node)
        await vaults().put(conn, name, pair.private)
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
        made.append(
            NewKey(key_id=int(new_id), slot=node.slot, node_id=None if node.slot is not None else node.id)
        )
    return made


@dataclass(frozen=True)
class Created:
    config_id: int
    node_id: uuid.UUID
    slot: int | None
    host: str
    expires_at: datetime


async def create_configs(conn: AsyncConnection, settings: RealitySettings, now: datetime) -> list[Created]:
    """At most one fresh config per due scope (and at most MAX_CREATIONS_PER_TICK in all), only on
    a key that carries the settings' target and only on a node that confirmed that target."""
    statuses = await all_statuses(conn)
    by_scope = await _serving_scopes(conn, now, statuses)
    if not by_scope:
        return []
    keys = await keys_by_scope(conn)
    domain = await current_active_domain(conn)
    created: list[Created] = []
    for scope in sorted(by_scope, key=str):
        if len(created) >= MAX_CREATIONS_PER_TICK:
            break
        key = keys.get(scope)
        if key is None:
            continue  # ensure_keys makes it; the node checks the target first
        if key.target_pair != (settings.target, settings.target_host):
            continue  # the scope moves to the new target once its configs ended (retarget_scopes)
        node = _newest(by_scope[scope])
        if not eligible_for_new_configs(
            node,
            statuses.get(node.id),
            now=now,
            telemt_clashes=await telemt_clash(conn, node.id),
            target=key.target_pair,
        ):
            continue  # its agent has not confirmed this target from the node (or it failed there)
        active_created = list(
            (
                await conn.execute(
                    select(RealityConfig.created_at).where(
                        RealityConfig.key_id == key.id, RealityConfig.status == "active"
                    )
                )
            )
            .scalars()
            .all()
        )
        if not creation_due(active_created, now=now, settings=settings):
            continue
        assert node.ipv4 is not None  # serves_reality
        host = slot_name(node.slot, domain) if node.slot is not None and domain else node.ipv4
        expires = now + timedelta(hours=settings.cap_hours)
        config_id = (
            await conn.execute(
                insert(RealityConfig)
                .values(
                    node_id=node.id,
                    slot=node.slot,
                    key_id=key.id,
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
            Created(config_id=int(config_id), node_id=node.id, slot=node.slot, host=host, expires_at=expires)
        )
    return created
