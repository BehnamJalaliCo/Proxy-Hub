"""Who sends a post: an interface with one implementation today, the bot through the outbox.

A later slice (decision D38) may post through the operator account with custom emoji. It would add
another :class:`Poster` (its own outbox job kinds and sender) and register it in :data:`POSTERS`;
nothing in the publisher changes. A post is always EDITED by the poster that sent it (a bot cannot
edit the operator account's message and the other way round), so ``posts.poster`` records its name.

No poster has a delete operation (CLAUDE.md rule 6).
"""

from __future__ import annotations

from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.telegram import store as tg_store

POST_KIND: Final = "auto"  # channel_posts.kind of the publisher's posts


class Poster(Protocol):
    name: str

    async def queue_post(self, db: AsyncConnection, *, role: str, text_html: str) -> int:
        """Queue a new post for the channel of ``role`` in the caller's transaction; returns the
        outbox job id (``channel_posts.outbox_id`` once it is sent)."""
        ...

    async def queue_edit(self, db: AsyncConnection, *, channel_post_id: int, text_html: str) -> int:
        """Queue an edit of a post this poster sent; returns the outbox job id."""
        ...


class BotPoster:
    """The panel's bot, through the Telegram outbox (pacing, 429, at-most-once posts)."""

    name = "bot"

    async def queue_post(self, db: AsyncConnection, *, role: str, text_html: str) -> int:
        return await tg_store.enqueue(
            db, "send_post", {"role": role, "text": text_html, "post_kind": POST_KIND}
        )

    async def queue_edit(self, db: AsyncConnection, *, channel_post_id: int, text_html: str) -> int:
        return await tg_store.enqueue(db, "edit_post", {"post_id": channel_post_id, "text": text_html})


BOT: Final = BotPoster()
POSTERS: Final[dict[str, Poster]] = {BOT.name: BOT}
