"""What the panel knows about each node's public MTProto proxy (read side).

Host (cross-team contract item 2): the slot's DNS name ``slot<N>.<active domain>`` when that name
really points at this node, i.e. the panel's own A record for it exists in Cloudflare
(``cf_records`` row with a Cloudflare id) with this node's IPv4; otherwise the node's IPv4. So a
link never names a host that does not lead to the node holding its secret (a slot's name moves to
the replacement node, whose secret is different).

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
from proxyhub.cloudflare.pool import PoolState
from proxyhub.cloudflare.rotation import GONE
from proxyhub.db.models import Node
from proxyhub.db.models_agent import TelemtNodeConfig, TelemtUser
from proxyhub.db.models_mtproto import MtprotoProxy
from proxyhub.db.models_net import CfRecord, CfZone
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.mtproto.settings import Mode, link_mode
from proxyhub.mtproto.urls import label_for, proxy_key, proxy_url

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

    @property
    def label(self) -> str:
        return label_for(self.slot, self.is_test)

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


async def _slot_records(db: Db) -> tuple[str | None, dict[uuid.UUID, set[tuple[str, str]]]]:
    """The active domain and, per node, the (name, IPv4) of the A records Cloudflare holds for it."""
    zone = (
        await db.execute(
            select(CfZone.id, CfZone.name).where(
                CfZone.pool_state == PoolState.ACTIVE.value, CfZone.status != GONE
            )
        )
    ).first()
    if zone is None:
        return None, {}
    rows = (
        await db.execute(
            select(CfRecord.node_id, CfRecord.name, CfRecord.content).where(
                CfRecord.zone_id == zone.id,
                CfRecord.type == "A",
                CfRecord.purpose == "slot",
                CfRecord.cf_record_id.is_not(None),
                CfRecord.node_id.is_not(None),
            )
        )
    ).all()
    out: dict[uuid.UUID, set[tuple[str, str]]] = {}
    for r in rows:
        out.setdefault(r.node_id, set()).add((str(r.name).lower(), str(r.content)))
    return str(zone.name).lower(), out


def pick_host(
    slot: int | None,
    ipv4: str | None,
    zone: str | None,
    records: set[tuple[str, str]],
) -> tuple[str | None, HostKind | None]:
    """Contract item 2: the slot's DNS name if it leads to this node, else the node's IPv4."""
    if slot is not None and zone is not None and ipv4 is not None:
        name = f"slot{slot}.{zone}"
        if (name, ipv4) in records:
            return name, "domain"
    if ipv4 is not None:
        return ipv4, "ip"
    return None, None


async def list_proxies(db: Db, seal_key: bytes, now: datetime) -> list[ProxyView]:
    """Every node's public proxy (deleted nodes for :data:`DELETED_KEPT_FOR`), slots first."""
    zone, records = await _slot_records(db)
    rows = (
        await db.execute(
            select(
                MtprotoProxy.node_id,
                MtprotoProxy.user_name,
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
            .order_by(Node.slot.asc().nulls_last(), Node.created_at.asc(), Node.id.asc())
        )
    ).all()
    views: list[ProxyView] = []
    for r in rows:
        try:
            secret_hex = open_secret(seal_key, r.node_id, r.user_name, r.secret_sealed).hex()
        except SealError:
            log.warning("mtproto: the sealed secret of node %s does not open; proxy skipped", r.node_id)
            continue
        ipv4 = _ipv4(r.ipv4)
        host, kind = pick_host(r.slot, ipv4, zone, records.get(r.node_id, set()))
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
            )
        )
    return views
