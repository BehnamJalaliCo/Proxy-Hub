"""Source plugins (decision D20: «لایه‌ی محتوا را باز بگذار»; docs/10 section 3.12).

Every kind of source is one plugin with the same small interface (:class:`SourcePlugin`):

1. ``check_ref``: the owner's input (a channel name, a feed URL) checked strictly and normalized;
2. ``link``: the https page the dashboard links to for that source;
3. ``fetch``: read the source over :class:`~proxyhub.content.net.SafeHttp` and normalize what it
   gave to :class:`FetchedItem`: plain text, a media reference, the original post's link, its time
   and its views. Nothing else of the source reaches the rest of the engine.

The engine (proxyhub/content/engine.py) never looks at a source's kind beyond :data:`PLUGINS`, so a
new kind is one new plugin here and one value in the ``content_sources.kind`` check.

Built now: ``telegram`` (a public channel's preview page, proxyhub/content/telegram_preview.py) and
``rss`` (an RSS 2.0 or Atom feed, proxyhub/content/rss.py).

**Instagram (later, not built):** reading other people's pages needs a separate, logged-in
Instagram account (no official API for it; docs/10 section 3.12 and risk R11: the account may be
blocked, the IG-Hub experience). It will be a plugin ``instagram`` with the same three steps; its
login is a secret and goes to the vault like the operator account's session, never into
``content_sources.ref``. Until then many news pages have a public Telegram channel too.
"""

from __future__ import annotations

from typing import Final, Protocol

from proxyhub.content.items import FetchedItem
from proxyhub.content.net import SafeHttp
from proxyhub.content.rss import RssFeed
from proxyhub.content.telegram_preview import TelegramPreview


class SourcePlugin(Protocol):
    kind: str

    def check_ref(self, raw: str) -> str:
        """The normalized reference, or :class:`~proxyhub.content.items.SourceInvalid`."""
        ...

    def link(self, ref: str) -> str:
        """The https page of this source (for the dashboard)."""
        ...

    def default_label(self, ref: str) -> str:
        """A label when the owner gives none."""
        ...

    async def fetch(self, http: SafeHttp, ref: str) -> list[FetchedItem]:
        """The source's recent posts, in any order. Raises FetchError."""
        ...


PLUGINS: Final[dict[str, SourcePlugin]] = {"telegram": TelegramPreview(), "rss": RssFeed()}
