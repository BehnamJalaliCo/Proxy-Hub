"""Links contract (cross-team contract item 3): the Reality configs the channel posts publish.

``vless://<uuid>@<host>:443?encryption=none&security=reality&sni=<name>&fp=chrome&pbk=<key>&sid=<id>
&type=tcp&flow=xtls-rprx-vision#<name>``. The host was fixed when the config was made (contract
item 2: ``slot<N>.<active domain>``, else the node's IPv4). The name every user's app shows starts
with the channel's name (decision N15), e.g. ``ipoxxo 🇩🇪 12``.

Listed: every active config that a serving node of its scope has confirmed (its agent reported
Xray up, without an error, after the config was made), and configs that ended in the last
:data:`ENDED_KEPT_FOR`, so posts that carry them can be marked. The UUID in a link is what grants
access: links leave the panel only through this provider and the owner's dashboard.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import quote, urlencode

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.fleet.lifecycle import NodeState
from proxyhub.links.contract import ProxyLink
from proxyhub.reality.settings import PORT, load_reality_settings
from proxyhub.reality.store import (
    SERVE_STATES,
    NodeView,
    StatusView,
    all_statuses,
    live_nodes,
    recent_and_active_configs,
)
from proxyhub.telegram.store import channels
from proxyhub.telegram.templates import fa_digits

Db = AsyncConnection | AsyncSession
LinkStatus = Literal["active", "expired", "full", "burned"]

ENDED_KEPT_FOR: Final = timedelta(days=7)
CONFIRM_AFTER: Final = timedelta(seconds=20)  # an agent report this long after creation confirms
DEFAULT_CHANNEL: Final = "ipoxxo"  # decision D23
FLAGS: Final = {
    "fsn1": "🇩🇪",
    "nbg1": "🇩🇪",
    "hel1": "🇫🇮",
    "ash": "🇺🇸",
    "hil": "🇺🇸",
    "sin": "🇸🇬",
}


def flag(location: str) -> str:
    return FLAGS.get(location, "🌐")


def vless_url(
    *, user_id: uuid.UUID, host: str, server_name: str, public_key: str, short_id: str, name: str
) -> str:
    query = urlencode(
        {
            "encryption": "none",
            "security": "reality",
            "sni": server_name,
            "fp": "chrome",
            "pbk": public_key,
            "sid": short_id,
            "type": "tcp",
            "flow": "xtls-rprx-vision",
        },
        quote_via=quote,
    )
    return f"vless://{user_id}@{host}:{PORT}?{query}#{quote(name, safe='')}"


async def link_prefix(db: Db) -> str:
    """What config names start with: the owner's text, else the main channel's @username."""
    settings = await load_reality_settings(db)
    if settings.link_name:
        return settings.link_name
    main = (await channels(db)).get("main") or {}
    username = str(main.get("username") or "").lstrip("@")
    return "@" + (username or DEFAULT_CHANNEL)  # D41: the ad in every config name, a findable @handle


def _confirmed(row: Any, scope_nodes: list[NodeView], statuses: dict[uuid.UUID, StatusView]) -> bool:
    for node in scope_nodes:
        st = statuses.get(node.id)
        if (
            st is not None
            and NodeState(node.state) in SERVE_STATES
            and st.up
            and st.apply_error is None
            and st.reported_at >= row.created_at + CONFIRM_AFTER
        ):
            return True
    return False


def config_name(prefix: str, location: str, config_id: int) -> str:
    return f"{prefix} {flag(location)} {config_id}"


async def links_for(db: Db, now: datetime) -> list[tuple[ProxyLink, Any]]:
    """(link, row) for every listed config, newest first."""
    rows = await recent_and_active_configs(db, now - ENDED_KEPT_FOR)
    if not rows:
        return []
    prefix = await link_prefix(db)
    nodes = await live_nodes(db)
    statuses = await all_statuses(db)
    out: list[tuple[ProxyLink, Any]] = []
    for r in rows:
        if r.status == "active":
            scope_nodes = [
                n for n in nodes if (n.slot == r.slot if r.slot is not None else n.id == r.node_id)
            ]
            if not _confirmed(r, scope_nodes, statuses):
                continue
        status: LinkStatus = r.status
        out.append(
            (
                ProxyLink(
                    kind="reality",
                    key=f"reality:{r.id}",
                    slot=r.slot,
                    label=f"ریلیتی {fa_digits(r.id)}",
                    url=vless_url(
                        user_id=r.uuid,
                        host=r.host,
                        server_name=r.server_name,
                        public_key=r.public_key,
                        short_id=r.short_id,
                        name=config_name(prefix, r.location, r.id),
                    ),
                    expires_at=r.expires_at,
                    status=status,
                ),
                r,
            )
        )
    return out


async def provide_links(db: AsyncConnection) -> list[ProxyLink]:
    return [link for link, _ in await links_for(db, datetime.now(UTC))]
