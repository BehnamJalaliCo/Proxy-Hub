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

**A photo or video post** (decision D51): the job's payload carries ``media`` (:class:`MediaPayload`:
the kind and the ``content_media`` row whose bytes the outbox uploads at send time) and the text is
the media's caption, within 1024 characters for both posters
(proxyhub/telegram/templates.py :data:`CAPTION_LIMIT`), so the bot can take the very same job. Its
status edits carry ``"caption": true`` and change the caption, never the text.

No poster has a delete operation (CLAUDE.md rule 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.posts import operator
from proxyhub.telegram import store as tg_store

POST_KIND: Final = "auto"  # channel_posts.kind of the publisher's posts


@dataclass(frozen=True)
class MediaPayload:
    """The photo or video of a post, by its stored file (``content_media.id``)."""

    kind: Literal["photo", "video"]
    media_id: int
    width: int | None = None
    height: int | None = None
    duration: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "media_id": self.media_id,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
        }


def post_payload(
    role: str, text_html: str, media: MediaPayload | None, breaking: bool = False
) -> dict[str, Any]:
    """``breaking``: a breaking-news post (D56): the outbox sends it in no-post hours too and does
    not hold it for the gap between two posts (the publisher keeps its own budget)."""
    payload: dict[str, Any] = {"role": role, "text": text_html, "post_kind": POST_KIND}
    if media is not None:
        payload["media"] = media.as_json()
    if breaking:
        payload["breaking"] = True
    return payload


def edit_payload(channel_post_id: int, text_html: str, caption: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"post_id": channel_post_id, "text": text_html}
    if caption:
        payload["caption"] = True
    return payload


class Poster(Protocol):
    name: str

    async def unavailable(self, db: AsyncConnection, *, role: str, now: datetime) -> str | None:
        """Why this poster cannot take a NEW post for the channel of ``role`` now (a short code),
        or None. The publisher then gives the post to the bot."""
        ...

    async def queue_post(
        self,
        db: AsyncConnection,
        *,
        role: str,
        text_html: str,
        media: MediaPayload | None = None,
        breaking: bool = False,
    ) -> int:
        """Queue a new post for the channel of ``role`` in the caller's transaction; returns the
        outbox job id (``channel_posts.outbox_id`` once it is sent). With ``media`` the post is
        that photo or video and ``text_html`` its caption."""
        ...

    async def queue_edit(
        self, db: AsyncConnection, *, channel_post_id: int, text_html: str, caption: bool = False
    ) -> int:
        """Queue an edit of a post this poster sent (of its caption, for a photo or video post);
        returns the outbox job id."""
        ...


class BotPoster:
    """The panel's bot, through the Telegram outbox (pacing, 429, at-most-once posts)."""

    name = "bot"

    async def unavailable(self, db: AsyncConnection, *, role: str, now: datetime) -> str | None:
        return None  # the bot's own conditions (a bot, a channel) are the publisher's checks

    async def queue_post(
        self,
        db: AsyncConnection,
        *,
        role: str,
        text_html: str,
        media: MediaPayload | None = None,
        breaking: bool = False,
    ) -> int:
        return await tg_store.enqueue(db, "send_post", post_payload(role, text_html, media, breaking))

    async def queue_edit(
        self, db: AsyncConnection, *, channel_post_id: int, text_html: str, caption: bool = False
    ) -> int:
        return await tg_store.enqueue(db, "edit_post", edit_payload(channel_post_id, text_html, caption))


class OperatorPoster:
    """The operator account (Premium), through its own outbox jobs (proxyhub/posts/operator_outbox.py).
    The payloads are the bot's, so a post the operator cannot send goes to the bot unchanged."""

    name = "operator"

    async def unavailable(self, db: AsyncConnection, *, role: str, now: datetime) -> str | None:
        return await operator.unavailable(db, role=role, now=now)

    async def queue_post(
        self,
        db: AsyncConnection,
        *,
        role: str,
        text_html: str,
        media: MediaPayload | None = None,
        breaking: bool = False,
    ) -> int:
        return await tg_store.enqueue(db, operator.SEND, post_payload(role, text_html, media, breaking))

    async def queue_edit(
        self, db: AsyncConnection, *, channel_post_id: int, text_html: str, caption: bool = False
    ) -> int:
        return await tg_store.enqueue(db, operator.EDIT, edit_payload(channel_post_id, text_html, caption))


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
