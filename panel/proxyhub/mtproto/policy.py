"""MTProto policy: what every node's desired Telemt state must hold (docs/11 E1-E10).

One run (inside the ``mtproto`` control loop's transaction):

1. Every node that holds a server and is coming up or in service (booting, registering, ready,
   unhealthy, repairing) gets the Telemt node settings from the owner's ``mtproto`` settings (port,
   fake-TLS domain, modes) and ONE long-lived Telemt user, its public proxy secret (16 random bytes,
   sealed). The fake-TLS domain is the node's OWN DNS name while the node serves that name with our
   certificate (VLESS + WS + TLS on 443; proxyhub/reality/store.py ``telemt_tls_domain``): then
   the SNI of its ``ee`` links belongs to the IP they lead to, and Telemt's TLS front is our own
   Xray. Otherwise the owner's ``tls_domain`` setting (the fallback, e.g. while the node has no
   name or no certificate yet). Its ``ee`` links follow the node's domain, so they change with it.
   Nodes are configured before they are ready on purpose: once a node has Telemt settings,
   the node executor only calls it ready when its agent reports Telemt up, so "ready" means "the
   proxy runs" and a node never flaps ready -> unhealthy -> ready when its proxy is added.
2. **Extras (decision D49).** A post carries ``links_per_post`` MTProto links (the posting setting
   ``max_mtproto``): one per node first, and while fewer nodes run, EXTRA links on the same nodes.
   Each extra is one more Telemt user of the node (``mt_<node>_x<n>``, its own random sealed secret,
   exactly like the primary) on the node's port and host, with the primary's sponsor tag and
   registration (so it needs no registration of its own and is never offered for one). A node keeps
   :func:`links_per_node` links in all (at most :data:`MAX_LINKS_PER_NODE`). Extras are only ever
   added: one is never removed while its node lives, even when more nodes come (a posted link must
   keep working); new posts just stop picking them (proxyhub/posts/links.py). They go with their
   node (disabled with it below; the rows go when the node is purged). Every pass the extras take
   the primary's current sponsor tag and registration (set, cleared or re-registered).
3. A ready node's proxies become *live* when its agent reports that it applied exactly the desired
   state the panel would serve now (which holds the users) and Telemt is up. Only live proxies are
   published or registered (the primary only).
4. Every enabled Telemt user of a node that is retiring or deleted is disabled.

It changes nothing else: never another node setting than the four above (the sponsor's tags are
kept), never a user it did not create (except disabling users of retiring/deleted nodes; an extra's
tag follows its node's primary), never a node's lifecycle. Burned and draining nodes keep what they
have. No secret or tag goes into ``actions_log`` (rule 3).
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
from proxyhub.db.models_mtproto import MAX_LINKS_PER_NODE, MtprotoProxy
from proxyhub.fleet.actions import log_action
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.mtproto.settings import MtprotoSettings
from proxyhub.mtproto.urls import extra_user_name, user_name_for
from proxyhub.reality.store import telemt_tls_domain

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
    extras: dict[uuid.UUID, int] = field(default_factory=dict)  # node -> extra users made this pass
    tags_synced: dict[uuid.UUID, int] = field(default_factory=dict)  # node -> extras given the tag

    @property
    def changed(self) -> bool:
        return bool(
            self.configured or self.created or self.live or self.disabled or self.extras or self.tags_synced
        )


def links_per_node(links_per_post: int, *, slotted: int, unslotted: int, has_slot: bool) -> int:
    """How many public links (the primary + extras) a node keeps (decision D49): enough that the
    nodes a post draws from offer ``links_per_post`` links between them, at most
    :data:`MAX_LINKS_PER_NODE`. The MAIN channel posts only slot nodes' links, so the slot nodes
    share a post between them. A node without a slot (a test node, test channel only) adds extras
    only while no slot node runs; otherwise the slot nodes already fill the post."""
    pool = slotted if has_slot else (unslotted if slotted == 0 else 0)
    if links_per_post <= 1 or pool <= 0:
        return 1
    return max(1, min(MAX_LINKS_PER_NODE, -(-links_per_post // pool)))


def _wanted(settings: MtprotoSettings, tls_domain: str) -> dict[str, Any]:
    return {
        "ports": [settings.port],
        "tls_domain": tls_domain,
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
    own_name = await telemt_tls_domain(session, node.id)
    want = _wanted(settings, own_name or settings.tls_domain)
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
            "tls_domain": want["tls_domain"],
            "tls_domain_source": "node_name" if own_name else "setting",
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
    proxy = (
        await session.execute(
            select(MtprotoProxy).where(MtprotoProxy.node_id == node.id, MtprotoProxy.ordinal == 0)
        )
    ).scalar_one_or_none()
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
        proxy = MtprotoProxy(node_id=node.id, user_name=name, ordinal=0, created_at=now, updated_at=now)
        session.add(proxy)
    elif existing is None:
        proxy.created_at, proxy.live_at, proxy.disabled_at, proxy.updated_at = now, None, None, now
    await session.flush()
    return proxy


async def _user(session: AsyncSession, node_id: uuid.UUID, name: str) -> TelemtUser | None:
    return (
        await session.execute(
            select(TelemtUser).where(TelemtUser.node_id == node_id, TelemtUser.name == name)
        )
    ).scalar_one_or_none()


async def _ensure_extras(
    session: AsyncSession,
    seal_key: bytes,
    node: Any,
    primary: MtprotoProxy,
    wanted_links: int,
    now: datetime,
    out: PolicyOutcome,
) -> list[MtprotoProxy]:
    """The node's extras (decision D49): every one it has (never removed), plus new ones until the
    node keeps ``wanted_links`` links. A new extra (or one whose user was removed by hand) gets a
    fresh secret and the primary's sponsor tag and registration."""
    have = {
        p.ordinal: p
        for p in (
            await session.execute(
                select(MtprotoProxy)
                .where(MtprotoProxy.node_id == node.id, MtprotoProxy.ordinal > 0)
                .order_by(MtprotoProxy.ordinal)
            )
        )
        .scalars()
        .all()
    }
    head = await _user(session, node.id, primary.user_name)
    made: list[str] = []
    for ordinal in sorted(set(range(1, wanted_links)) | set(have)):
        proxy = have.get(ordinal)
        name = proxy.user_name if proxy is not None else extra_user_name(node.id, ordinal)
        if await _user(session, node.id, name) is None:
            await create_telemt_user(
                session,
                seal_key,
                node.id,
                name,
                ad_tag=head.ad_tag if head is not None else None,
                registration_id=head.registration_id if head is not None else None,
            )
            made.append(name)
            if proxy is not None:  # its user was removed by hand: a new secret, never the old one
                proxy.created_at, proxy.live_at, proxy.disabled_at, proxy.updated_at = now, None, None, now
        if proxy is None:
            proxy = MtprotoProxy(
                node_id=node.id, user_name=name, ordinal=ordinal, created_at=now, updated_at=now
            )
            session.add(proxy)
            have[ordinal] = proxy
    if made:
        out.extras[node.id] = len(made)
        await log_action(
            session,
            actor=ACTOR,
            action="mtproto_extra_users_created",
            target=node.name,
            dry_run=False,
            allowed=True,
            reasons=["links_per_post"],
            context={"node_id": str(node.id), "users": made, "links": len(have) + 1},
            result="extra proxy secrets created on the node (sealed; never logged), with its sponsor tag",
        )
    await session.flush()
    return [have[k] for k in sorted(have)]


async def _sync_extra_tags(
    session: AsyncSession,
    node: Any,
    primary: MtprotoProxy,
    extras: list[MtprotoProxy],
    now: datetime,
    out: PolicyOutcome,
) -> None:
    """Every extra carries its primary's sponsor tag and registration (decision D49)."""
    if not extras:
        return
    head = await _user(session, node.id, primary.user_name)
    if head is None:
        return
    users = (
        (
            await session.execute(
                select(TelemtUser).where(
                    TelemtUser.node_id == node.id, TelemtUser.name.in_([e.user_name for e in extras])
                )
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for user in users:
        if (user.ad_tag, user.registration_id) != (head.ad_tag, head.registration_id):
            user.ad_tag, user.registration_id, user.updated_at = head.ad_tag, head.registration_id, now
            changed += 1
    if changed:
        out.tags_synced[node.id] = changed
        await log_action(
            session,
            actor=ACTOR,
            action="mtproto_extra_tags_synced",
            target=node.name,
            dry_run=False,
            allowed=True,
            reasons=["tag_set" if head.ad_tag is not None else "tag_cleared"],
            context={"node_id": str(node.id), "extras": changed},
            result="extra secrets follow the primary's sponsor tag (tag not logged)",
        )


async def _check_live(
    session: AsyncSession,
    seal_key: bytes,
    node: Any,
    proxies: list[MtprotoProxy],
    now: datetime,
    out: PolicyOutcome,
) -> None:
    pending = [p for p in proxies if p.live_at is None]
    if not pending:
        return
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
        return  # the agent has not applied the current state (with these users) yet
    for proxy in pending:
        proxy.live_at = now
        proxy.updated_at = now
    out.live.append(node.id)
    primary = next((p for p in pending if p.ordinal == 0), None)
    await log_action(
        session,
        actor=ACTOR,
        action="mtproto_proxy_live",
        target=node.name,
        dry_run=False,
        allowed=True,
        reasons=["agent_applied", "telemt_up"],
        context={
            "node_id": str(node.id),
            **({"user": primary.user_name} if primary is not None else {}),
            "extras": [p.user_name for p in pending if p.ordinal > 0],
        },
        result="the node runs its public MTProto proxy"
        if primary
        else "the node runs its extra MTProto links",
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
        proxies = (
            (
                await session.execute(
                    select(MtprotoProxy).where(
                        MtprotoProxy.node_id == node_id, MtprotoProxy.disabled_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        for proxy in proxies:  # the primary and its extras go together
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
    links_per_post: int = 1,
) -> PolicyOutcome:
    """One idempotent pass. Without valid settings or the sealing key nothing is configured or
    created (fail closed), but users of retiring/deleted nodes are still disabled.
    ``links_per_post``: the MTProto links a post carries (posting setting ``max_mtproto``; D49)."""
    out = PolicyOutcome()
    # Disabling comes first: a node that cannot be configured below must never keep the users of a
    # retiring or deleted node enabled.
    await _disable_retired(session, now, out)
    if settings is not None and seal_key is not None:
        nodes = (
            await session.execute(
                select(Node.id, Node.name, Node.state, Node.slot)
                .where(Node.state.in_([s.value for s in CONFIGURE_STATES]))
                .order_by(Node.created_at, Node.id)
                .limit(MAX_NODES_PER_RUN)
            )
        ).all()
        slotted = sum(1 for n in nodes if n.slot is not None)
        unslotted = len(nodes) - slotted
        for node in nodes:
            wanted = links_per_node(
                links_per_post, slotted=slotted, unslotted=unslotted, has_slot=node.slot is not None
            )
            # One SAVEPOINT per node: a node whose secret does not open (SealError) or that hits a
            # desired-state limit is skipped and retried next pass; the other nodes still go on.
            try:
                async with session.begin_nested():
                    await _ensure_config(session, node, settings, out)
                    proxy = await _ensure_user(session, seal_key, node, now, out)
                    extras = await _ensure_extras(session, seal_key, node, proxy, wanted, now, out)
                    await _sync_extra_tags(session, node, proxy, extras, now, out)
                    if node.state == S.READY.value:
                        await _check_live(session, seal_key, node, [proxy, *extras], now, out)
            except (SealError, DesiredStateError) as exc:  # only the type is logged, never values
                out.failed[node.id] = type(exc).__name__
                log.warning("mtproto: node %s skipped this pass (%s)", node.name, type(exc).__name__)
    await session.flush()
    return out
