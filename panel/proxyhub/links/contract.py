from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

# The key of a REALITY config's XHTTP link is the config's own key + this suffix.
XHTTP_KEY_SUFFIX: Final = ":xhttp"


@dataclass(frozen=True)
class ProxyLink:
    kind: Literal["mtproto", "reality"]
    # stable id: "mtproto:<node_id>:<user>", "reality:<config_id>", or for the same config's XHTTP
    # link "reality:<config_id>:xhttp"
    key: str
    slot: int | None
    # short Persian label for the owner (dashboard, logs), e.g. "پروکسی ۱", "کانفیگ ۱ (XHTTP)"; a
    # channel post never shows it (proxyhub/telegram/templates.py)
    label: str
    url: str  # https://t.me/proxy?server=..&port=..&secret=.. or vless://...
    expires_at: datetime | None
    status: Literal["active", "expired", "full", "burned"]
    # Another way into the same config (the XHTTP link of a REALITY config): the key of the link it
    # belongs to. A post counts the config once (``max_reality``) and carries both links together.
    companion_of: str | None = None
    # An EXTRA link into the same MTProto proxy server (decision D49: another Telemt user, its own
    # secret, on the node's port and host): the key of that node's primary link. A new post takes
    # one link per node first and extras only after that (proxyhub/posts/links.py).
    extra_of: str | None = None


Provider = Callable[[AsyncConnection], Awaitable[list[ProxyLink]]]
