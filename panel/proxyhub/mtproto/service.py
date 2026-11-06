"""What the panel knows about each node's public MTProto proxy (read side).

Host (cross-team contract item 2): the node's DNS name (``slot<N>.<active domain>``, or a test
node's ``t<8 hex>.<active domain>``) when that name really points at this node, i.e. the panel's own
A record for it exists in Cloudflare (``cf_records`` row with a Cloudflare id) with this node's IPv4;
otherwise the node's IPv4. So a link never names a host that does not lead to the node holding its
secret (a slot's name moves to the replacement node, whose secret is different). The fake-TLS
domain of the ``ee`` secret is the node's own ``tls_domain`` (proxyhub/mtproto/policy.py: the node's
name while its Xray serves that name with our certificate, else the owner's setting).

A node has its primary proxy (``ordinal`` 0, the one registered with @MTProxybot) and, while a post
needs more links than nodes run, EXTRA proxies (decision D49): other Telemt users on the same port
and host, each with its own secret and the primary's sponsor tag (``extra_of``: the primary's key).

Status of a proxy:

* ``pending``: not yet confirmed running on its node (never published);
* ``active``: confirmed running and the node is in service (a short unhealthy spell keeps it);
* ``burned``: the node was found filtered from Iran;
* ``expired``: the node is draining, retiring or deleted, or the secret was disabled.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.agent_api.sealing import SealError, open_secret
from proxyhub.cloudflare.service import NodeNames, node_names
from proxyhub.db.models import Node
from proxyhub.db.models_agent import TelemtNodeConfig, TelemtUser
from proxyhub.db.models_mtproto import MtprotoProxy
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.mtproto.settings import Mode, link_mode
from proxyhub.mtproto.urls import label_for, proxy_key, proxy_url, user_name_for

log = logging.getLogger(__name__)

Db = AsyncConnection | AsyncSession
Status = Literal["pending", "active", "expired", "burned"]
HostKind = Literal["domain", "ip"]

# Links of a deleted node stay listed (as expired) this long, so posts can still be marked.
DELETED_KEPT_FOR = timedelta(days=7)
OUT_OF_SERVICE = frozenset({NodeState.DRAINING.value, NodeState.RETIRING.value, NodeState.DELETED.value})


def proxy_status(node_state: str, enabled: bool, live_at: datetime | None) -> Status:
    if not enabled or node_state in OUT_OF_SERVICE:
        return "expired"
    if node_state == NodeState.BURNED.value:
        return "burned"
    if live_at is None:
        return "pending"
    return "active"


@dataclass(frozen=True)
class ProxyView:
    key: str
    node_id: uuid.UUID
    node_name: str
    node_state: str
    is_test: bool
    slot: int | None
    user: str
    host: str | None
    host_kind: HostKind | None
    port: int
    mode: Mode
    tls_domain: str
    enabled: bool
    sponsor_tag_set: bool
    connections: int | None
    total_octets: int | None
    stats_at: datetime | None
    live_at: datetime | None
    status: Status
    # The 32-hex secret: only for the public link and @MTProxybot registration (rule 3).
    secret_hex: str = field(repr=False)
    ordinal: int = 0  # 0: the node's primary proxy; 1 and up: its extras (D49)
    extra_of: str | None = None  # an extra: its node's primary key (never registered itself)

    @property
    def label(self) -> str:
        return label_for(self.slot, self.is_test, self.ordinal)

    @property
    def url(self) -> str | None:
        if self.host is None:
            return None
        return proxy_url(self.host, self.port, self.secret_hex, self.mode, self.tls_domain)


def _ipv4(value: Any) -> str | None:
    if value is None:
        return None
    try:
        ip = ipaddress.ip_interface(str(value)).ip
    except ValueError:
        return None
    return str(ip) if ip.version == 4 else None


def pick_host(
    slot: int | None,
    node_id: uuid.UUID,
    ipv4: str | None,
    names: NodeNames,
) -> tuple[str | None, HostKind | None]:
    """Contract item 2: the node's DNS name if it leads to this node, else the node's IPv4."""
    name = names.own_name(slot, node_id, ipv4)
    if name is not None:
        return name, "domain"
    if ipv4 is not None:
        return ipv4, "ip"
    return None, None


async def list_proxies(db: Db, seal_key: bytes, now: datetime) -> list[ProxyView]:
    """Every node's public proxies, its primary first, then its extras (deleted nodes for
    :data:`DELETED_KEPT_FOR`), slots first."""
    names = await node_names(db)
    rows = (
        await db.execute(
            select(
                MtprotoProxy.node_id,
                MtprotoProxy.user_name,
                MtprotoProxy.ordinal,
                MtprotoProxy.live_at,
                Node.name.label("node_name"),
                Node.state,
                Node.is_test,
                Node.slot,
                Node.ipv4,
                TelemtUser.secret_sealed,
                TelemtUser.enabled,
                TelemtUser.ad_tag.label("user_ad_tag"),
                TelemtUser.last_current_connections,
                TelemtUser.last_total_octets,
                TelemtUser.last_stats_at,
                TelemtNodeConfig.ports,
                TelemtNodeConfig.tls_domain,
                TelemtNodeConfig.mode_tls,
                TelemtNodeConfig.mode_secure,
                TelemtNodeConfig.mode_classic,
                TelemtNodeConfig.ad_tag.label("node_ad_tag"),
            )
            .join(Node, Node.id == MtprotoProxy.node_id)
            .join(
                TelemtUser,
                and_(TelemtUser.node_id == MtprotoProxy.node_id, TelemtUser.name == MtprotoProxy.user_name),
            )
            .join(TelemtNodeConfig, TelemtNodeConfig.node_id == MtprotoProxy.node_id)
            .where(or_(Node.state != NodeState.DELETED.value, Node.deleted_at >= now - DELETED_KEPT_FOR))
            .order_by(
                Node.slot.asc().nulls_last(), Node.created_at.asc(), Node.id.asc(), MtprotoProxy.ordinal.asc()
            )
        )
    ).all()
    primaries = {r.node_id: r.user_name for r in rows if r.ordinal == 0}
    views: list[ProxyView] = []
    for r in rows:
        try:
            secret_hex = open_secret(seal_key, r.node_id, r.user_name, r.secret_sealed).hex()
        except SealError:
            log.warning("mtproto: the sealed secret of node %s does not open; proxy skipped", r.node_id)
            continue
        ipv4 = _ipv4(r.ipv4)
        host, kind = pick_host(r.slot, r.node_id, ipv4, names)
        views.append(
            ProxyView(
                key=proxy_key(r.node_id, r.user_name),
                node_id=r.node_id,
                node_name=r.node_name,
                node_state=r.state,
                is_test=bool(r.is_test),
                slot=r.slot,
                user=r.user_name,
                host=host,
                host_kind=kind,
                port=int(r.ports[0]),
                mode=link_mode(tls=r.mode_tls, secure=r.mode_secure, classic=r.mode_classic),
                tls_domain=r.tls_domain,
                enabled=bool(r.enabled),
                sponsor_tag_set=r.user_ad_tag is not None or r.node_ad_tag is not None,
                connections=r.last_current_connections,
                total_octets=r.last_total_octets,
                stats_at=r.last_stats_at,
                live_at=r.live_at,
                status=proxy_status(r.state, bool(r.enabled), r.live_at),
                secret_hex=secret_hex,
                ordinal=int(r.ordinal),
                extra_of=(
                    proxy_key(r.node_id, primaries.get(r.node_id) or user_name_for(r.node_id))
                    if r.ordinal > 0
                    else None
                ),
            )
        )
    return views
