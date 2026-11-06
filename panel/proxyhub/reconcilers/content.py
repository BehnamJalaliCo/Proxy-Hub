"""Control loop ``content`` (decision D51, M3.1 phase A): the owner's sources, read every 5 minutes.

Each minute it runs one round of proxyhub/content/engine.py under the loop's advisory lock, with no
transaction open while a source or a file answers. It reads public pages, feeds and files, and
(decision D55) the owner's public Telegram channels with the operator account, read-only
(proxyhub/content/telegram_account.py; the account's session comes from the vault and never
leaves proxyhub/mtproxy). It writes the review queue; nothing reaches a channel without an
approval (the content supervisor's, decision D52, proxyhub/reconcilers/content_qc.py, or the
owner's) and the publisher (proxyhub/posts/publisher.py).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.content.engine import Engine, HttpFactory, Round
from proxyhub.content.net import SafeHttp
from proxyhub.content.telegram_account import AccountReader
from proxyhub.reconcilers.base import Reconciler
from proxyhub.reconcilers.telegram import run_locked
from proxyhub.telegram.store import now_utc
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)


class _VaultOnce:
    def __init__(self, factory: Callable[[], Vault]) -> None:
        self._factory = factory
        self._vault: Vault | None = None

    def __call__(self) -> Vault | None:
        if self._vault is None:
            try:
                self._vault = self._factory()
            except VaultError as exc:
                log.warning("content: vault unavailable (%s)", type(exc).__name__)
                return None
        return self._vault


class ContentReconciler(Reconciler):
    name = "content"
    interval_seconds = 60.0

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = now_utc,
        http_factory: HttpFactory = SafeHttp,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        use_account: bool = True,
    ) -> None:
        self.now = now
        self.http_factory = http_factory
        self._vault = _VaultOnce(vault_factory)
        self.use_account = use_account
        self._engine: Engine | None = None
        self.last: Round | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            if self._engine is None or self._engine.engine is not engine:
                account = (
                    AccountReader(engine=engine, vault=self._vault, clock=self.now)
                    if self.use_account
                    else None
                )
                self._engine = Engine(
                    engine=engine, clock=self.now, http_factory=self.http_factory, account=account
                )
            self.last = await self._engine.run_round()
            if self.last.read or self.last.media or self.last.waiting:
                log.info(
                    "content: %d source(s) read (%s), %d waiting (%s), %d file(s) (%s)",
                    len(self.last.read),
                    ", ".join(f"{k}={v}" for k, v in self.last.read.items()),
                    len(self.last.waiting),
                    ", ".join(f"{k}={v}" for k, v in self.last.waiting.items()),
                    len(self.last.media),
                    ", ".join(f"{k}={v}" for k, v in self.last.media.items()),
                )

        return await run_locked(engine, self.name, step)
