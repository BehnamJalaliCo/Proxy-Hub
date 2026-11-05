from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class ProxyLink:
    kind: Literal["mtproto", "reality"]
    # stable id: "mtproto:<node_id>:<user>", "reality:<config_id>", or for the same config's XHTTP
    # link "reality:<config_id>:xhttp"
    key: str
    slot: int | None
    label: str  # short Persian label for the post, e.g. "پروکسی ۱", "کانفیگ ۱ (XHTTP)"
    url: str  # https://t.me/proxy?server=..&port=..&secret=.. or vless://...
    expires_at: datetime | None
    status: Literal["active", "expired", "full", "burned"]
    # Another way into the same config (the XHTTP link of a REALITY config): the key of the link it
    # belongs to. A post counts the config once (``max_reality``) and carries both links together.
    companion_of: str | None = None


Provider = Callable[[AsyncConnection], Awaitable[list[ProxyLink]]]
