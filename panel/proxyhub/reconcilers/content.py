"""Control loop ``content`` (decision D51, M3.1 phase A): the owner's sources, read every 5 minutes.

Each minute it runs one round of proxyhub/content/engine.py under the loop's advisory lock, with no
transaction open while a source or a file answers. It only reads public pages, feeds and files
(never Telegram's API, never a secret) and writes the review queue; nothing reaches a channel
without the owner's approval (decision D20) and the publisher (proxyhub/posts/publisher.py).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.content.engine import Engine, HttpFactory, Round
from proxyhub.content.net import SafeHttp
from proxyhub.reconcilers.base import Reconciler
from proxyhub.reconcilers.telegram import run_locked
from proxyhub.telegram.store import now_utc

log = logging.getLogger(__name__)


class ContentReconciler(Reconciler):
    name = "content"
    interval_seconds = 60.0

    def __init__(
        self, *, now: Callable[[], datetime] = now_utc, http_factory: HttpFactory = SafeHttp
    ) -> None:
        self.now = now
        self.http_factory = http_factory
        self._engine: Engine | None = None
        self.last: Round | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            if self._engine is None or self._engine.engine is not engine:
                self._engine = Engine(engine=engine, clock=self.now, http_factory=self.http_factory)
            self.last = await self._engine.run_round()
            if self.last.read or self.last.media:
                log.info(
                    "content: %d source(s) read (%s), %d file(s) (%s)",
                    len(self.last.read),
                    ", ".join(f"{k}={v}" for k, v in self.last.read.items()),
                    len(self.last.media),
                    ", ".join(f"{k}={v}" for k, v in self.last.media.items()),
                )

        return await run_locked(engine, self.name, step)
