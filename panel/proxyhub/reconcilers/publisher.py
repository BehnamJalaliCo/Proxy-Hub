"""Control loop ``publisher`` (M3.3/M3.4): new posts on the day's plan, status edits of recent posts.

Every 30 s, in one transaction under the loop's advisory lock: first the status/host edits of
recent posts, then the due new post (proxyhub/posts/publisher.py). Each step runs in its own
SAVEPOINT, so a failure in one is logged and does not undo or block the other; the providers'
links are read at most once per round and only when a step needs them. Nothing here talks to
Telegram: posts and edits are outbox jobs that the ``telegram_outbox`` loop (the bot) or the
``operator_outbox`` loop (the operator account, decision D38) sends with the per-channel pacing. New
posts go through the poster the owner chose (proxyhub/posts/poster.py), read every round.

A failed step is logged with the exception TYPE and the code locations only: a database error's
text can carry the statement's parameters, and a post's text holds proxy links (CLAUDE.md rule 3).
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable, Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.links.contract import Provider
from proxyhub.posts import links, publisher
from proxyhub.posts.poster import POSTERS, Poster, configured
from proxyhub.reconcilers.base import Reconciler
from proxyhub.telegram.store import now_utc

log = logging.getLogger(__name__)


def _log_failure(step: str, exc: BaseException) -> None:
    where = "".join(traceback.format_tb(exc.__traceback__))
    log.error("publisher: %s failed this round (%s)\n%s", step, type(exc).__name__, where)


class PublisherReconciler(Reconciler):
    name = "publisher"
    interval_seconds = 30.0

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = now_utc,
        providers: Sequence[Provider] | None = None,
        poster: Poster | None = None,
    ) -> None:
        self.now = now
        self.providers = providers  # None: proxyhub.links.registry.PROVIDERS at run time
        self.poster = poster  # None: the owner's choice (posts.operator) every round
        self.last: dict[str, str | int] = {}

    async def run_once(self, conn: AsyncConnection) -> None:
        now = self.now()
        cached: list[links.Harvest] = []

        async def harvest() -> links.Harvest:
            if not cached:
                cached.append(await links.harvest(conn, self.providers))
            return cached[0]

        poster = self.poster or await configured(conn)
        posters = {**POSTERS, poster.name: poster}
        try:
            async with conn.begin_nested():
                self.last["edits"] = await publisher.refresh_posts(conn, now, harvest, posters)
        except Exception as exc:
            _log_failure("status edits", exc)
            self.last["edits"] = "error"
        try:  # D56: a breaking-news post first; the day plan's slot waits behind it
            async with conn.begin_nested():
                self.last["breaking"] = await publisher.publish_breaking(conn, now, harvest, poster)
        except Exception as exc:
            _log_failure("breaking post", exc)
            self.last["breaking"] = "error"
        try:
            async with conn.begin_nested():
                self.last["post"] = await publisher.publish_due(conn, now, harvest, poster)
        except Exception as exc:
            _log_failure("new post", exc)
            self.last["post"] = "error"
