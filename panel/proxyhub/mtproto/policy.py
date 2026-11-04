"""MTProto policy: what every node's desired Telemt state must hold (docs/11 E1-E10).

One run (inside the ``mtproto`` control loop's transaction):

1. Every node that holds a server and is coming up or in service (booting, registering, ready,
   unhealthy, repairing) gets the Telemt node settings from the owner's ``mtproto`` settings (port,
   fake-TLS domain, modes) and ONE long-lived Telemt user, its public proxy secret (16 random bytes,
   sealed). Nodes are configured before they are ready on purpose: once a node has Telemt settings,
   the node executor only calls it ready when its agent reports Telemt up, so "ready" means "the
   proxy runs" and a node never flaps ready -> unhealthy -> ready when its proxy is added.
2. A ready node's proxy becomes *live* when its agent reports that it applied exactly the desired
   state the panel would serve now (which holds the user) and Telemt is up. Only live proxies are
   published or registered.
3. Every enabled Telemt user of a node that is retiring or deleted is disabled.

It changes nothing else: never another node setting than the four above (the sponsor's tags are
kept), never a user it did not create (except disabling users of retiring/deleted nodes), never a
node's lifecycle. Burned and draining nodes keep what they have. No secret or tag goes into
``actions_log`` (rule 3).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.agent_api.desired import (
    DesiredStateError,
    build_desired_state,
    canonical_json,
    create_telemt_user,
    etag_for,
    set_telemt_node_config,
)
from proxyhub.agent_api.sealing import SealError
from proxyhub.db.models import Node
from proxyhub.db.models_agent import AgentNode, AgentNodeReport, TelemtNodeConfig, TelemtUser
from proxyhub.db.models_mtproto import MtprotoProxy
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.mtproto.settings import MtprotoSettings
from proxyhub.mtproto.urls import user_name_for

ACTOR = "mtproto"
S = NodeState
CONFIGURE_STATES = (S.BOOTING, S.REGISTERING, S.READY, S.UNHEALTHY, S.REPAIRING)
RETIRED_STATES = (S.RETIRING, S.DELETED)
# A run touches at most this many nodes (the node cap is 10; this only bounds a pathological table).
MAX_NODES_PER_RUN = 100

log = logging.getLogger(__name__)


@dataclass
class PolicyOutcome:
    configured: list[uuid.UUID] = field(default_factory=list)
    created: list[uuid.UUID] = field(default_factory=list)
    live: list[uuid.UUID] = field(default_factory=list)
    disabled: dict[uuid.UUID, int] = field(default_factory=dict)
    failed: dict[uuid.UUID, str] = field(default_factory=dict)  # node -> error type, retried next pass

    @property
    def changed(self) -> bool:
        return bool(self.configured or self.created or self.live or self.disabled)


def _wanted(settings: MtprotoSettings) -> dict[str, Any]:
    return {
        "ports": [settings.port],
        "tls_domain": settings.tls_domain,
        "tls": settings.mode_tls,
        "secure": settings.mode_secure,
        "classic": settings.mode_classic,
    }


async def _ensure_config(
    session: AsyncSession, node: Any, settings: MtprotoSettings, out: PolicyOutcome
) -> None:
    # Core select (not session.get): build_desired_state reads the row through the session later,
    # and an ORM object loaded here would be stale after the upsert below.
    current = (
        await session.execute(
            select(
                TelemtNodeConfig.ports,
                TelemtNodeConfig.tls_domain,
                TelemtNodeConfig.mode_tls,
                TelemtNodeConfig.mode_secure,
                TelemtNodeConfig.mode_classic,
                TelemtNodeConfig.ad_tag,
            ).where(TelemtNodeConfig.node_id == node.id)
        )
    ).first()
    want = _wanted(settings)
    if current is not None:
        have = {
            "ports": list(current.ports),
            "tls_domain": current.tls_domain,
            "tls": current.mode_tls,
            "secure": current.mode_secure,
            "classic": current.mode_classic,
        }
        if have == want:
            return
    # The node-wide sponsor tag (if anyone set one) is kept as it is.
    await set_telemt_node_config(
        session, node.id, ad_tag=current.ad_tag if current is not None else None, **want
    )
    out.configured.append(node.id)
    await log_action(
        session,
        actor=ACTOR,
        action="mtproto_config",
        target=node.name,
        dry_run=False,
        allowed=True,
        reasons=["created" if current is None else "settings_changed"],
        context={
            "node_id": str(node.id),
            "port": settings.port,
            "tls_domain": settings.tls_domain,
            "modes": {
                "tls": settings.mode_tls,
                "secure": settings.mode_secure,
                "classic": settings.mode_classic,
            },
        },
        result="Telemt settings written to the node's desired state",
    )


async def _ensure_user(
    session: AsyncSession, seal_key: bytes, node: Any, now: datetime, out: PolicyOutcome
) -> MtprotoProxy:
    proxy = await session.get(MtprotoProxy, node.id)
    name = proxy.user_name if proxy is not None else user_name_for(node.id)
    existing = (
        await session.execute(
            select(TelemtUser.id).where(TelemtUser.node_id == node.id, TelemtUser.name == name)
        )
    ).scalar_one_or_none()
    if existing is None:
        # A new node, or (only by hand) its user row was removed: a fresh secret, never the old one.
        await create_telemt_user(session, seal_key, node.id, name)
        out.created.append(node.id)
        await log_action(
            session,
            actor=ACTOR,
            action="mtproto_user_created",
            target=node.name,
            dry_run=False,
            allowed=True,
            reasons=["one_proxy_per_node" if proxy is None else "user_missing"],
            context={"node_id": str(node.id), "user": name},
            result="long-lived proxy secret created (sealed; never logged)",
        )
    if proxy is None:
        proxy = MtprotoProxy(node_id=node.id, user_name=name, created_at=now, updated_at=now)
        session.add(proxy)
    elif existing is None:
        proxy.created_at, proxy.live_at, proxy.disabled_at, proxy.updated_at = now, None, None, now
    await session.flush()
    return proxy


async def _check_live(
    session: AsyncSession, seal_key: bytes, node: Any, proxy: MtprotoProxy, now: datetime, out: PolicyOutcome
) -> None:
    agent = (
        await session.execute(
            select(AgentNode.last_applied_etag, AgentNode.last_apply_ok).where(AgentNode.node_id == node.id)
        )
    ).first()
    if agent is None or agent.last_apply_ok is not True or agent.last_applied_etag is None:
        return
    telemt_up = (
        await session.execute(
            select(AgentNodeReport.telemt_up)
            .where(AgentNodeReport.node_id == node.id)
            .order_by(AgentNodeReport.at.desc(), AgentNodeReport.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if telemt_up is not True:
        return
    doc = await build_desired_state(session, seal_key, node.id)
    if etag_for(canonical_json(doc)) != agent.last_applied_etag:
        return  # the agent has not applied the current state (with this user) yet
    proxy.live_at = now
    proxy.updated_at = now
    out.live.append(node.id)
    await log_action(
        session,
        actor=ACTOR,
        action="mtproto_proxy_live",
        target=node.name,
        dry_run=False,
        allowed=True,
        reasons=["agent_applied", "telemt_up"],
        context={"node_id": str(node.id), "user": proxy.user_name},
        result="the node runs its public MTProto proxy",
    )


async def _disable_retired(session: AsyncSession, now: datetime, out: PolicyOutcome) -> None:
    rows = (
        await session.execute(
            select(TelemtUser, Node.name, Node.state)
            .join(Node, Node.id == TelemtUser.node_id)
            .where(Node.state.in_([s.value for s in RETIRED_STATES]), TelemtUser.enabled.is_(True))
            .order_by(TelemtUser.node_id, TelemtUser.name)
            .limit(MAX_NODES_PER_RUN * 10)
        )
    ).all()
    nodes: dict[uuid.UUID, tuple[str, str]] = {}
    for user, node_name, state in rows:
        user.enabled = False
        user.updated_at = now
        out.disabled[user.node_id] = out.disabled.get(user.node_id, 0) + 1
        nodes[user.node_id] = (node_name, state)
    for node_id, (node_name, state) in nodes.items():
        proxy = await session.get(MtprotoProxy, node_id)
        if proxy is not None and proxy.disabled_at is None:
            proxy.disabled_at = now
            proxy.updated_at = now
        await log_action(
            session,
            actor=ACTOR,
            action="mtproto_users_disabled",
            target=node_name,
            dry_run=False,
            allowed=True,
            reasons=[f"node_{state}"],
            context={"node_id": str(node_id), "users": out.disabled[node_id]},
            result="proxy secrets of a node leaving service disabled",
        )


async def apply_policy(
    session: AsyncSession,
    *,
    seal_key: bytes | None,
    settings: MtprotoSettings | None,
    now: datetime,
) -> PolicyOutcome:
    """One idempotent pass. Without valid settings or the sealing key nothing is configured or
    created (fail closed), but users of retiring/deleted nodes are still disabled."""
    out = PolicyOutcome()
    # Disabling comes first: a node that cannot be configured below must never keep the users of a
    # retiring or deleted node enabled.
    await _disable_retired(session, now, out)
    if settings is not None and seal_key is not None:
        nodes = (
            await session.execute(
                select(Node.id, Node.name, Node.state)
                .where(Node.state.in_([s.value for s in CONFIGURE_STATES]))
                .order_by(Node.created_at, Node.id)
                .limit(MAX_NODES_PER_RUN)
            )
        ).all()
        for node in nodes:
            # One SAVEPOINT per node: a node whose secret does not open (SealError) or that hits a
            # desired-state limit is skipped and retried next pass; the other nodes still go on.
            try:
                async with session.begin_nested():
                    await _ensure_config(session, node, settings, out)
                    proxy = await _ensure_user(session, seal_key, node, now, out)
                    if node.state == S.READY.value and proxy.live_at is None:
                        await _check_live(session, seal_key, node, proxy, now, out)
            except (SealError, DesiredStateError) as exc:  # only the type is logged, never values
                out.failed[node.id] = type(exc).__name__
                log.warning("mtproto: node %s skipped this pass (%s)", node.name, type(exc).__name__)
    await session.flush()
    return out
