"""Links contract (cross-team contract item 3): the MTProto links the channel posts publish.

One link per node, the node's long-lived primary proxy, plus its EXTRA links (decision D49:
``extra_of`` names the primary), each as ``https://t.me/proxy?server=..&port=..&secret=..``
(:mod:`proxyhub.mtproto.urls`), host per contract item 2. Only proxies that were confirmed running
are listed; a proxy whose node left service stays listed as ``expired`` (``burned`` when its node
was found filtered) for :data:`proxyhub.mtproto.service.DELETED_KEPT_FOR` after deletion, so a post
that carries it can be marked. MTProto secrets do not expire (``expires_at`` is always None).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.links.contract import ProxyLink
from proxyhub.mtproto.keys import load_proxy_seal_key
from proxyhub.mtproto.service import ProxyView, list_proxies

LinkStatus = Literal["active", "expired", "full", "burned"]


def _link_status(view: ProxyView) -> LinkStatus | None:
    if view.status == "pending":
        return None
    return view.status


def to_link(view: ProxyView) -> ProxyLink | None:
    url = view.url
    status = _link_status(view)
    if view.live_at is None or url is None or status is None:
        return None
    return ProxyLink(
        kind="mtproto",
        key=view.key,
        slot=view.slot,
        label=view.label,
        url=url,
        expires_at=None,
        status=status,
        extra_of=view.extra_of,
    )


async def provide_links(db: AsyncConnection) -> list[ProxyLink]:
    """Raises :class:`proxyhub.mtproto.keys.SealKeyUnavailable` when the master key is missing (a
    caller must then change nothing, not treat every link as gone)."""
    views = await list_proxies(db, load_proxy_seal_key(), datetime.now(UTC))
    return [link for link in (to_link(v) for v in views) if link is not None]
