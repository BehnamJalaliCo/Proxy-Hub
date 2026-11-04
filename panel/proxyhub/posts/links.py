"""The live links from every provider (proxyhub/links/registry.py), checked before they reach a post.

* Each provider runs inside its own SAVEPOINT: one that raises (even a database error) costs only
  its own links for this round, and :attr:`Harvest.complete` turns False, so no post is edited from
  a partial picture (a missing link would otherwise look like a dead one).
* A link must have a known kind and status, a key and a label. An ACTIVE link must have a URL that
  passes :func:`~proxyhub.telegram.templates.mtproto_url` / :func:`~proxyhub.telegram.templates.vless_url`
  (the MTProto link is rebuilt in canonical form). A link that fails is left out and its key is
  remembered in :attr:`Harvest.doubtful`, so posts that carry it are not edited this round.
* Nothing here logs or returns a URL: a log line names the provider and the link key only.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.links import registry
from proxyhub.links.contract import Provider, ProxyLink
from proxyhub.telegram.templates import clean_label, mtproto_url, vless_url

log = logging.getLogger(__name__)

KINDS: Final = ("mtproto", "reality")
STATUSES: Final = ("active", "expired", "full", "burned")
MAX_KEY_CHARS: Final = 200
# A config that ends sooner than this is not put into a NEW post.
MIN_REMAINING: Final = timedelta(hours=1)


@dataclass(frozen=True)
class Harvest:
    links: dict[str, ProxyLink] = field(default_factory=dict)  # by key; URL checked when active
    complete: bool = True  # every provider answered
    doubtful: frozenset[str] = frozenset()  # keys a provider returned in a shape we refused
    providers: int = 0

    def active(self, kind: str) -> list[ProxyLink]:
        return [x for x in self.links.values() if x.kind == kind and x.status == "active"]


def _provider_name(provider: Provider) -> str:
    module = getattr(provider, "__module__", "?")
    return f"{module}.{getattr(provider, '__qualname__', type(provider).__name__)}"


def checked(link: object) -> ProxyLink | None:
    """The link with a cleaned label and (when active) a checked URL, or None if refused."""
    if not isinstance(link, ProxyLink):
        return None
    if link.kind not in KINDS or link.status not in STATUSES:
        return None
    if not isinstance(link.key, str) or not link.key or len(link.key) > MAX_KEY_CHARS:
        return None
    if link.slot is not None and (not isinstance(link.slot, int) or isinstance(link.slot, bool)):
        return None
    if link.expires_at is not None and (
        not isinstance(link.expires_at, datetime) or link.expires_at.tzinfo is None
    ):
        return None
    try:
        label = clean_label(link.label) if isinstance(link.label, str) else ""
        url = link.url
        if link.status == "active":
            if not isinstance(url, str):
                return None
            url = mtproto_url(url) if link.kind == "mtproto" else vless_url(url)
    except ValueError:
        return None
    if not label:
        return None
    return replace(link, label=label, url=url if isinstance(url, str) else "")


async def harvest(db: AsyncConnection, providers: Sequence[Provider] | None = None) -> Harvest:
    providers = registry.PROVIDERS if providers is None else providers
    links: dict[str, ProxyLink] = {}
    doubtful: set[str] = set()
    complete = True
    for provider in providers:
        name = _provider_name(provider)
        try:
            async with db.begin_nested():
                got = await provider(db)
        except Exception as exc:  # a provider's failure must not stop the others or the post
            log.warning("links provider %s failed (%s)", name, type(exc).__name__)
            complete = False
            continue
        if not isinstance(got, list):
            log.warning("links provider %s returned %s instead of a list", name, type(got).__name__)
            complete = False
            continue
        for raw in got:
            link = checked(raw)
            if link is None:
                key = getattr(raw, "key", None)
                if isinstance(key, str) and key:
                    doubtful.add(key[:MAX_KEY_CHARS])
                    log.warning("links provider %s: link %s refused (bad shape or URL)", name, key[:80])
                else:
                    complete = False
                    log.warning("links provider %s: a link without a key was refused", name)
                continue
            links.setdefault(link.key, link)
    return Harvest(links=links, complete=complete, doubtful=frozenset(doubtful), providers=len(providers))


def _order(link: ProxyLink) -> tuple[int, str, str]:
    return (link.slot if link.slot is not None else 1_000_000, link.label, link.key)


def for_new_post(h: Harvest, now: datetime, *, max_mtproto: int, max_reality: int) -> list[ProxyLink]:
    """Active links for a new post: MTProto by slot, then Reality configs that still have at least
    :data:`MIN_REMAINING` left."""
    mtproto = sorted(h.active("mtproto"), key=_order)[:max_mtproto]
    reality = sorted(
        (x for x in h.active("reality") if x.expires_at is None or x.expires_at - now >= MIN_REMAINING),
        key=_order,
    )[:max_reality]
    return [*mtproto, *reality]
