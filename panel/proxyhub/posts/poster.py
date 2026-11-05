"""Who sends a post: the bot through the Telegram outbox, or the operator account (decision D38).

* :class:`BotPoster` (``bot``): the panel's bot; outbox jobs ``send_post`` / ``edit_post``, sent by
  proxyhub/telegram/outbox.py. Plain emoji.
* :class:`OperatorPoster` (``operator``): the second Telegram account with Premium, so a post carries
  the owner's animated custom emoji; outbox jobs ``operator_send_post`` / ``operator_edit_post``,
  sent by proxyhub/posts/operator_outbox.py under the account's lock and the same per-channel
  pacing. It takes a NEW post only when :meth:`OperatorPoster.unavailable` says nothing against it;
  otherwise :func:`for_new_post` hands the post to the bot and the publisher logs why.

A post is always EDITED by the poster that sent it (a bot cannot edit the operator account's
message and the other way round), so ``posts.poster`` records its name. The owner's choice is
:func:`configured` (``posts.operator``; the bot until the owner switches).

No poster has a delete operation (CLAUDE.md rule 6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.posts import operator
from proxyhub.telegram import store as tg_store

POST_KIND: Final = "auto"  # channel_posts.kind of the publisher's posts


class Poster(Protocol):
    name: str

    async def unavailable(self, db: AsyncConnection, *, role: str, now: datetime) -> str | None:
        """Why this poster cannot take a NEW post for the channel of ``role`` now (a short code),
        or None. The publisher then gives the post to the bot."""
        ...

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

    async def unavailable(self, db: AsyncConnection, *, role: str, now: datetime) -> str | None:
        return None  # the bot's own conditions (a bot, a channel) are the publisher's checks

    async def queue_post(self, db: AsyncConnection, *, role: str, text_html: str) -> int:
        return await tg_store.enqueue(
            db, "send_post", {"role": role, "text": text_html, "post_kind": POST_KIND}
        )

    async def queue_edit(self, db: AsyncConnection, *, channel_post_id: int, text_html: str) -> int:
        return await tg_store.enqueue(db, "edit_post", {"post_id": channel_post_id, "text": text_html})


class OperatorPoster:
    """The operator account (Premium), through its own outbox jobs (proxyhub/posts/operator_outbox.py).
    The payloads are the bot's, so a post the operator cannot send goes to the bot unchanged."""

    name = "operator"

    async def unavailable(self, db: AsyncConnection, *, role: str, now: datetime) -> str | None:
        return await operator.unavailable(db, role=role, now=now)

    async def queue_post(self, db: AsyncConnection, *, role: str, text_html: str) -> int:
        return await tg_store.enqueue(
            db, operator.SEND, {"role": role, "text": text_html, "post_kind": POST_KIND}
        )

    async def queue_edit(self, db: AsyncConnection, *, channel_post_id: int, text_html: str) -> int:
        return await tg_store.enqueue(db, operator.EDIT, {"post_id": channel_post_id, "text": text_html})


BOT: Final = BotPoster()
OPERATOR: Final = OperatorPoster()
POSTERS: Final[dict[str, Poster]] = {BOT.name: BOT, OPERATOR.name: OPERATOR}


async def configured(db: AsyncConnection) -> Poster:
    """The poster the owner chose for new posts (the bot until the owner switches)."""
    return POSTERS[(await operator.load_config(db)).poster]


async def for_new_post(
    db: AsyncConnection, poster: Poster, *, role: str, now: datetime
) -> tuple[Poster, str | None]:
    """The poster that takes this NEW post and, when that is the bot instead of ``poster``, why."""
    reason = await poster.unavailable(db, role=role, now=now)
    return (BOT, reason) if reason is not None else (poster, None)
