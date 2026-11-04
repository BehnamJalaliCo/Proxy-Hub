from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class ProxyLink:
    kind: Literal["mtproto", "reality"]
    key: str  # stable id: "mtproto:<node_id>:<user>" or "reality:<config_id>"
    slot: int | None
    label: str  # short Persian label for the post, e.g. "پروکسی ۱", "ریلیتی ۱"
    url: str  # https://t.me/proxy?server=..&port=..&secret=.. or vless://...
    expires_at: datetime | None
    status: Literal["active", "expired", "full", "burned"]


Provider = Callable[[AsyncConnection], Awaitable[list[ProxyLink]]]
