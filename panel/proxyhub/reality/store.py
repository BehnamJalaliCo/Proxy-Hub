"""Reality database access shared by the agent API, the policy loop, the links provider and the
dashboard API.

Key scopes: a node with a slot uses its slot's key (and the slot's configs), a node without one
(the owner's test node) its own. Every node of a scope serves all of the scope's active configs, so
a config posted as ``slot<N>.<domain>`` keeps working when slot N moves to a new server.

Transport ``ws_tls`` (the default, proxyhub/reality/settings.py): a node serves VLESS + WebSocket +
TLS for its scope's DNS name (``slot<N>.<domain>``, or a test node's ``t<8 hex>.<domain>``) with the
panel's wildcard certificate for the active domain (proxyhub/acme). Without the name, the
certificate, or an agent that knows the transport (0.3.0 on), the node gets no inbound at all and
no link is made or published for it (:func:`ws_tls_plan`).

XHTTP (transport ``reality`` with ``xhttp_enabled``): the section also carries ``xhttp`` (port and
the scope's path) for an agent from 0.4.0 on whose node's Telemt does not use the XHTTP port. The
agent then serves VLESS + XHTTP + REALITY there with the same key and users, and reports the port
while it runs it (``reality_node_status.xhttp_port``), which is what the links provider checks.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.acme.store import Material, material
from proxyhub.cloudflare.service import NodeNames, node_names
from proxyhub.db.models import Node
from proxyhub.db.models_agent import AgentNode, TelemtNodeConfig
from proxyhub.db.models_reality import RealityConfig, RealityKey, RealityNodeStatus, RealityUsage
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.reality.settings import PORT, XHTTP_PORT, load_reality_settings
from proxyhub.vault import Vault, VaultError

Db = AsyncConnection | AsyncSession
S = NodeState
log = logging.getLogger(__name__)

# Nodes that keep serving their scope's configs (a burned or draining node until it is gone: its
# clients may still reach it, and the slot's replacement serves the same configs).
SERVE_STATES: Final = frozenset({S.READY, S.UNHEALTHY, S.REPAIRING, S.BURNED, S.DRAINING})
XRAY_API_PORT: Final = 10085  # node-image/files/agent-xray.json; Telemt may not use it
# The first agent that renders the ws_tls inbound; an older one would refuse the whole document.
WS_TLS_AGENT: Final = (0, 3, 0)
# The first agent that renders the XHTTP inbound (an older one would refuse the "xhttp" key).
XHTTP_AGENT: Final = (0, 4, 0)
EMAIL_DOMAIN: Final = "proxyhub"
_EMAIL = re.compile(r"cfg-([1-9][0-9]{0,17})@proxyhub")


def config_email(config_id: int) -> str:
    """The Xray user label of a config: an id, never anything about a person."""
    return f"cfg-{config_id}@{EMAIL_DOMAIN}"


def config_id_from_email(email: str) -> int | None:
    m = _EMAIL.fullmatch(email)
    return int(m.group(1)) if m else None


def node_ipv4(value: object) -> str | None:
    """A node's IPv4 address as text (the column is INET), or None."""
    if value is None:
        return None
    try:
        ip = ipaddress.ip_interface(str(value)).ip
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


def is_ip_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class NodeView:
    id: uuid.UUID
    name: str
    slot: int | None
    state: str
    ipv4: str | None
    location: str
    ready_at: datetime | None


_NODE_COLS = (Node.id, Node.name, Node.slot, Node.state, Node.ipv4, Node.location, Node.ready_at)


def _node(r: Any) -> NodeView:
    return NodeView(
        id=r.id,
        name=r.name,
        slot=r.slot,
        state=r.state,
        ipv4=node_ipv4(r.ipv4),
        location=r.location,
        ready_at=r.ready_at,
    )


async def get_node(db: Db, node_id: uuid.UUID) -> NodeView | None:
    r = (await db.execute(select(*_NODE_COLS).where(Node.id == node_id))).first()
    return _node(r) if r is not None else None


async def live_nodes(db: Db) -> list[NodeView]:
    rows = (await db.execute(select(*_NODE_COLS).where(Node.state != S.DELETED.value))).all()
    return [_node(r) for r in rows]


Scope = tuple[str, int | uuid.UUID]


def node_scope(node: NodeView) -> Scope:
    """A node's key scope: ("slot", N) or, without a slot, ("node", its id)."""
    return ("slot", node.slot) if node.slot is not None else ("node", node.id)


@dataclass(frozen=True)
class KeyView:
    id: int
    slot: int | None
    node_id: uuid.UUID | None
    public_key: str
    short_id: str
    target: str
    server_name: str
    secret_name: str
    ws_path: str
    xhttp_path: str = "/"

    @property
    def scope(self) -> Scope:
        if self.slot is not None:
            return ("slot", self.slot)
        assert self.node_id is not None  # reality_keys one_scope check
        return ("node", self.node_id)

    @property
    def target_pair(self) -> tuple[str, str]:
        """(target, server name): what a node's target check must be about to count."""
        return self.target, self.server_name


_KEY_COLS = (
    RealityKey.id,
    RealityKey.slot,
    RealityKey.node_id,
    RealityKey.public_key,
    RealityKey.short_id,
    RealityKey.target,
    RealityKey.server_name,
    RealityKey.secret_name,
    RealityKey.ws_path,
    RealityKey.xhttp_path,
)


def _key(r: Any) -> KeyView:
    return KeyView(**r._mapping)


def scope_filter(slot: int | None, node_id: uuid.UUID) -> Any:
    return RealityKey.slot == slot if slot is not None else RealityKey.node_id == node_id


async def scope_key(db: Db, node: NodeView) -> KeyView | None:
    r = (await db.execute(select(*_KEY_COLS).where(scope_filter(node.slot, node.id)))).first()
    return _key(r) if r is not None else None


async def keys_by_scope(db: Db) -> dict[Scope, KeyView]:
    return {(k := _key(r)).scope: k for r in (await db.execute(select(*_KEY_COLS))).all()}


async def telemt_ports(db: Db, node_id: uuid.UUID) -> set[int]:
    ports = (
        await db.execute(select(TelemtNodeConfig.ports).where(TelemtNodeConfig.node_id == node_id))
    ).scalar_one_or_none()
    return set(ports or [])


async def telemt_clash(db: Db, node_id: uuid.UUID) -> bool:
    """True if the node's Telemt config uses Xray's port or API port (the agent would refuse it)."""
    return bool({PORT, XRAY_API_PORT} & await telemt_ports(db, node_id))


@dataclass(frozen=True)
class StatusView:
    node_id: uuid.UUID
    enabled: bool
    up: bool
    version: str | None
    uptime_s: float | None
    target_ok: bool | None
    apply_error: str | None
    users_reported: int
    reported_at: datetime
    # What the node's last target check was about (None with target_ok: never checked).
    checked_target: str | None = None
    checked_server_name: str | None = None
    # The port of the XHTTP inbound the agent says it runs (None: none).
    xhttp_port: int | None = None


_STATUS_COLS = (
    RealityNodeStatus.node_id,
    RealityNodeStatus.enabled,
    RealityNodeStatus.up,
    RealityNodeStatus.version,
    RealityNodeStatus.uptime_s,
    RealityNodeStatus.target_ok,
    RealityNodeStatus.apply_error,
    RealityNodeStatus.users_reported,
    RealityNodeStatus.reported_at,
    RealityNodeStatus.checked_target,
    RealityNodeStatus.checked_server_name,
    RealityNodeStatus.xhttp_port,
)


def target_verdict(status: StatusView | None, target: str, server_name: str) -> bool | None:
    """The node's target check for exactly this target and server name: True passed, False failed,
    None not checked yet. A result about another target (one the owner has since replaced) does
    not count either way."""
    if status is None or status.target_ok is None:
        return None
    if (status.checked_target, status.checked_server_name) != (target, server_name):
        return None
    return status.target_ok


async def node_status(db: Db, node_id: uuid.UUID) -> StatusView | None:
    r = (await db.execute(select(*_STATUS_COLS).where(RealityNodeStatus.node_id == node_id))).first()
    return StatusView(*r) if r is not None else None


async def all_statuses(db: Db) -> dict[uuid.UUID, StatusView]:
    return {r.node_id: StatusView(*r) for r in (await db.execute(select(*_STATUS_COLS))).all()}


def _utc_text(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _agent_at_least(version: str | None, least: tuple[int, int, int]) -> bool:
    m = re.match(r"([0-9]{1,5})\.([0-9]{1,5})\.([0-9]{1,5})", version or "")
    return m is not None and tuple(int(g) for g in m.groups()) >= least


def agent_supports_ws_tls(version: str | None) -> bool:
    """True for an agent version from 0.3.0 on (``X.Y.Z``, anything after it ignored)."""
    return _agent_at_least(version, WS_TLS_AGENT)


def agent_supports_xhttp(version: str | None) -> bool:
    """True for an agent version from 0.4.0 on (``X.Y.Z``, anything after it ignored)."""
    return _agent_at_least(version, XHTTP_AGENT)


async def agent_version(db: Db, node_id: uuid.UUID) -> str | None:
    return (
        await db.execute(select(AgentNode.agent_version).where(AgentNode.node_id == node_id))
    ).scalar_one_or_none()


@dataclass(frozen=True)
class WsTls:
    """What a node needs to serve VLESS + WS + TLS: its scope's DNS name and the certificate."""

    name: str
    # The name's A record points at exactly this node (links are made only there; the old server of
    # a slot keeps serving the name for clients that still have its address).
    own: bool
    cert: Material

    @property
    def check_pair(self) -> tuple[str, str]:
        """(target, server name) the agent's own TLS check of the inbound reports."""
        return f"{self.name}:{PORT}", self.name


async def ws_tls_plan(
    db: Db,
    node: NodeView,
    now: datetime,
    *,
    names: NodeNames | None = None,
    vault: Vault | None = None,
) -> WsTls | None:
    """The node's ws_tls material, or None: no DNS name for its scope in Cloudflare, no valid
    certificate for the active domain, or an agent older than 0.3.0."""
    names = names if names is not None else await node_names(db)
    name = names.scope_name(node.slot, node.id)
    if name is None or not agent_supports_ws_tls(await agent_version(db, node.id)):
        return None
    cert = await material(db, names.zone, now, vault)
    if cert is None:
        return None
    return WsTls(name=name, own=names.own_name(node.slot, node.id, node.ipv4) is not None, cert=cert)


async def xray_section(db: Db, node_id: uuid.UUID, vault: Vault | None = None) -> dict[str, Any] | None:
    """The desired state's ``xray`` section for one node, or None (then the key is left out, and an
    agent that runs Xray stops it). Present only when the node's agent reported that it manages
    Xray (an older agent refuses the key), the node still serves, its Telemt does not clash, and
    its scope has a key; for ``ws_tls`` also when :func:`ws_tls_plan` has the name and the
    certificate. Carries the scope's private key (``reality``) or the certificate's key
    (``ws_tls``): only for that node's own agent. The users are the scope's active configs of the
    transport the settings ask for."""
    node = await get_node(db, node_id)
    if node is None or S(node.state) not in SERVE_STATES:
        return None
    status = await node_status(db, node_id)
    if status is None or not status.enabled or await telemt_clash(db, node_id):
        return None
    key = await scope_key(db, node)
    if key is None:
        return None
    settings = await load_reality_settings(db)
    configs = (
        await db.execute(
            select(
                RealityConfig.id, RealityConfig.uuid, RealityConfig.expires_at, RealityConfig.data_cap_bytes
            )
            .where(
                RealityConfig.key_id == key.id,
                RealityConfig.status == "active",
                RealityConfig.transport == settings.transport,
            )
            .order_by(RealityConfig.id)
        )
    ).all()
    users = [
        {
            "id": str(c.uuid),
            "email": config_email(c.id),
            "expires_at": _utc_text(c.expires_at),
            "data_cap_bytes": c.data_cap_bytes,
        }
        for c in configs
    ]
    if settings.transport == "ws_tls":
        plan = await ws_tls_plan(db, node, datetime.now(UTC), vault=vault)
        if plan is None:
            return None
        return {
            "transport": "ws_tls",
            "port": PORT,
            "server_name": plan.name,
            "ws_path": key.ws_path,
            "tls_cert_chain": plan.cert.chain_pem,
            "tls_key": plan.cert.key_pem.reveal(),
            "users": users,
        }
    try:
        secret = await (vault or Vault.from_config()).get(db, key.secret_name)
    except VaultError as exc:
        secret, problem = None, type(exc).__name__
    else:
        problem = "missing"
    if secret is None:
        # Leave Xray out rather than fail the whole document: the node's Telemt and ad-tag changes
        # must still reach it. The agent then stops Xray and reports it down, so no config of this
        # scope is handed to posts (proxyhub/reality/links.py needs Xray up on a serving node).
        log.error("reality: the private key of key %s is unusable (%s); xray left out", key.id, problem)
        return None
    # The REALITY section has exactly the shape agents before 0.3.0 know (no "transport" key), and
    # the "xhttp" key only for an agent that knows it (0.4.0 on).
    section: dict[str, Any] = {
        "port": PORT,
        "private_key": secret.reveal(),
        "target": key.target,
        "server_names": [key.server_name],
        "short_ids": [key.short_id],
        "users": users,
    }
    if await serves_xhttp(db, node_id, settings.xhttp_enabled):
        section["xhttp"] = {"port": XHTTP_PORT, "path": key.xhttp_path}
    return section


async def serves_xhttp(db: Db, node_id: uuid.UUID, enabled: bool) -> bool:
    """Whether a REALITY node's section gets the XHTTP inbound: the owner's switch is on, its agent
    knows the key (0.4.0 on), and its Telemt does not listen on the XHTTP port."""
    return (
        enabled
        and agent_supports_xhttp(await agent_version(db, node_id))
        and XHTTP_PORT not in await telemt_ports(db, node_id)
    )


async def telemt_tls_domain(db: Db, node_id: uuid.UUID) -> str | None:
    """The node's own DNS name when its Telemt fake-TLS domain should be that name: the node serves
    ws_tls (its document's xray section, built by :func:`xray_section` itself) and the name's record
    points at this node. Then the SNI of its ``ee`` links matches the IP they lead to, and Telemt's
    TLS front is our own Xray with the real certificate. None: keep the owner's setting."""
    section = await xray_section(db, node_id)
    if section is None or section.get("transport") != "ws_tls":
        return None
    node = await get_node(db, node_id)
    if node is None:
        return None
    names = await node_names(db)
    own = names.own_name(node.slot, node.id, node.ipv4)
    return own if own == section.get("server_name") else None


@dataclass(frozen=True)
class XrayUserTotal:
    email: str
    total_octets: int


async def store_xray_report(
    db: Db,
    node_id: uuid.UUID,
    *,
    enabled: bool,
    up: bool,
    version: str | None,
    uptime_s: float | None,
    target_ok: bool | None,
    apply_error: str | None,
    users: Sequence[XrayUserTotal],
    now: datetime,
    checked: tuple[str, str] | None = None,
    xhttp_port: int | None = None,
) -> int:
    """Store one report's Xray section: node status, and per config the bytes this node counted.

    The agent sends cumulative totals; a total below the last one means the agent started counting
    again (its state was reset), so the new total is added rather than lost. Only configs of the
    node's own scope are accepted. ``checked`` is the (target, server name) the node's last target
    check was about, given exactly when ``target_ok`` is. Returns how many configs got usage. The
    caller commits.
    """
    if (target_ok is None) != (checked is None):
        raise ValueError("a target check result comes with the target it was about")
    status_values: dict[str, Any] = {
        "enabled": enabled,
        "up": up,
        "version": version,
        "uptime_s": uptime_s,
        "target_ok": target_ok,
        "checked_target": checked[0] if checked else None,
        "checked_server_name": checked[1] if checked else None,
        "apply_error": apply_error,
        "users_reported": len(users),
        "reported_at": now,
        "xhttp_port": xhttp_port,
    }
    await db.execute(
        insert(RealityNodeStatus)
        .values(node_id=node_id, **status_values)
        .on_conflict_do_update(index_elements=[RealityNodeStatus.node_id], set_=status_values)
    )
    node = await get_node(db, node_id)
    if node is None or not users:
        return 0
    key = await scope_key(db, node)
    if key is None:
        return 0
    reported = {cid: u.total_octets for u in users if (cid := config_id_from_email(u.email)) is not None}
    if not reported:
        return 0
    mine = set(
        (
            await db.execute(
                select(RealityConfig.id).where(
                    RealityConfig.key_id == key.id, RealityConfig.id.in_(list(reported))
                )
            )
        )
        .scalars()
        .all()
    )
    previous = {
        r.config_id: (r.total_bytes, r.last_reported)
        for r in (
            await db.execute(
                select(RealityUsage.config_id, RealityUsage.total_bytes, RealityUsage.last_reported).where(
                    RealityUsage.node_id == node_id, RealityUsage.config_id.in_(list(mine))
                )
            )
        ).all()
    }
    for cid in sorted(mine):
        value = reported[cid]
        total, last = previous.get(cid, (0, 0))
        total += value - last if value >= last else value
        await db.execute(
            insert(RealityUsage)
            .values(config_id=cid, node_id=node_id, total_bytes=total, last_reported=value, updated_at=now)
            .on_conflict_do_update(
                index_elements=[RealityUsage.config_id, RealityUsage.node_id],
                set_={"total_bytes": total, "last_reported": value, "updated_at": now},
            )
        )
    if mine:
        # A config's usage is the sum over every node of its scope (old and new server of a slot).
        total = (
            select(func.coalesce(func.sum(RealityUsage.total_bytes), 0))
            .where(RealityUsage.config_id == RealityConfig.id)
            .scalar_subquery()
        )
        await db.execute(
            update(RealityConfig).where(RealityConfig.id.in_(list(mine))).values(used_bytes=total)
        )
    return len(mine)


async def recent_and_active_configs(db: Db, since: datetime) -> list[Any]:
    """Active configs and those that ended after ``since``, with their key and node."""
    return list(
        (
            await db.execute(
                select(
                    RealityConfig.id,
                    RealityConfig.node_id,
                    RealityConfig.slot,
                    RealityConfig.uuid,
                    RealityConfig.host,
                    RealityConfig.created_at,
                    RealityConfig.expires_at,
                    RealityConfig.data_cap_bytes,
                    RealityConfig.used_bytes,
                    RealityConfig.status,
                    RealityConfig.ended_at,
                    RealityConfig.key_id,
                    RealityConfig.transport,
                    RealityKey.public_key,
                    RealityKey.short_id,
                    RealityKey.server_name,
                    RealityKey.ws_path,
                    RealityKey.xhttp_path,
                    Node.name.label("node_name"),
                    Node.location,
                )
                .join(RealityKey, RealityKey.id == RealityConfig.key_id)
                .join(Node, Node.id == RealityConfig.node_id)
                .where(
                    or_(
                        RealityConfig.status == "active",
                        and_(RealityConfig.ended_at.is_not(None), RealityConfig.ended_at >= since),
                    )
                )
                .order_by(RealityConfig.id.desc())
            )
        ).all()
    )
