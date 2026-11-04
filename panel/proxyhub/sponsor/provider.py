"""Where the live MTProto proxies come from (cross-team SPONSOR contract).

Team mtproto implements ``proxyhub/mtproto/sponsor_api.py``::

    @dataclass(frozen=True)
    class RegistrableProxy: key: str; host: str; port: int; secret_hex: str
    async def proxies_to_register(db: AsyncConnection) -> list[RegistrableProxy]
    async def apply_ad_tag(db: AsyncConnection, key: str, ad_tag: str | None) -> None

This module only describes that shape (:class:`SponsorProvider`, which the module itself satisfies)
and holds :data:`MTPROTO_PROVIDER`, which the integrator points at that module. While it is None the
sponsor reconciler registers nothing (it still keeps campaign states up to date).

* ``key``: a stable id, ``"mtproto:<node_id>:<user>"``.
* ``host``: the slot's DNS name, or the node's IPv4 when there is no usable domain.
* ``secret_hex``: 32 lower-case hex characters, no ``ee``/``dd`` prefix. A secret: it is sent to
  @MTProxybot once, kept out of every log line, and never stored by this package.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

KEY_RE: Final = re.compile(r"mtproto:(?P<node>[0-9a-fA-F-]{36}):(?P<user>[A-Za-z0-9_]{1,32})")
SECRET_RE: Final = re.compile(r"[0-9a-f]{32}")
_HOST_RE: Final = re.compile(
    r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?)+"
)


class Registrable(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def host(self) -> str: ...
    @property
    def port(self) -> int: ...
    @property
    def secret_hex(self) -> str: ...


class SponsorProvider(Protocol):
    async def proxies_to_register(self, db: AsyncConnection) -> Sequence[Registrable]: ...

    async def apply_ad_tag(self, db: AsyncConnection, key: str, ad_tag: str | None) -> None: ...


# Wired by the integrator: ``from proxyhub.mtproto import sponsor_api as MTPROTO_PROVIDER``.
MTPROTO_PROVIDER: SponsorProvider | None = None


def valid(proxy: Registrable) -> bool:
    """The shape the contract promises; anything else is skipped (and never sent to the bot)."""
    return (
        isinstance(proxy.key, str)
        and 0 < len(proxy.key) <= 160
        and isinstance(proxy.host, str)
        and bool(_HOST_RE.fullmatch(proxy.host))
        and isinstance(proxy.port, int)
        and not isinstance(proxy.port, bool)
        and 1 <= proxy.port <= 65535
        and isinstance(proxy.secret_hex, str)
        and bool(SECRET_RE.fullmatch(proxy.secret_hex))
    )


def node_of(key: str | None) -> uuid.UUID | None:
    """The node id inside a key, or None when the key does not have the contract's shape."""
    match = KEY_RE.fullmatch(key or "")
    if match is None:
        return None
    try:
        return uuid.UUID(match.group("node"))
    except ValueError:
        return None


def user_of(key: str) -> str:
    """The Telemt user name inside a key (``mtproxy_registrations.secret_name``), else the key."""
    match = KEY_RE.fullmatch(key)
    return match.group("user") if match else key[:64]
