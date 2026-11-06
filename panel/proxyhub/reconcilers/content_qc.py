"""Control loop ``content_qc`` (decision D52): the content supervisor, one round a minute.

Each round runs proxyhub/content/supervisor.py under the loop's advisory lock, with no transaction
open while Claude answers (a review takes up to ``timeout_s``). It reads the review queue, asks
Claude through the host bridge (deploy/claude-bridge) or the owner's API key, approves or rejects
candidates in ``auto`` mode, turns bad sources off and alerts the owner only for what code cannot
fix. It never posts: the publisher (proxyhub/posts/publisher.py) takes what is approved, else the
content bank.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.config import get_config
from proxyhub.content.supervisor import Outcome, Supervisor
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
                log.warning("content qc: vault unavailable (%s)", type(exc).__name__)
                return None
        return self._vault


class ContentQcReconciler(Reconciler):
    name = "content_qc"
    interval_seconds = 60.0

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = now_utc,
        bridge_socket: Path | None = None,
        vault_factory: Callable[[], Vault] = Vault.from_config,
    ) -> None:
        self.now = now
        self.bridge_socket = bridge_socket
        self._vault = _VaultOnce(vault_factory)
        self._supervisor: Supervisor | None = None
        self.last: Outcome | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            if self._supervisor is None or self._supervisor.engine is not engine:
                socket = self.bridge_socket or get_config().claude_bridge_socket
                self._supervisor = Supervisor(
                    engine=engine, clock=self.now, bridge_socket=socket, vault=self._vault
                )
            try:
                self.last = await self._supervisor.run_round()
            except Exception as exc:
                # A database error's text can carry a caption or a source's words: only the type is
                # logged, and the loop's back-off applies (the base loop logs this error).
                raise RuntimeError(f"content qc round failed ({type(exc).__name__})") from None
            last = self.last
            if last.candidate_id is not None or last.rule_rejected or last.disabled_sources or last.alerts:
                log.info(
                    "content qc: %s candidate=%s decision=%s review=%s rules=%d disabled=%s alerts=%s",
                    last.action,
                    last.candidate_id,
                    last.decision,
                    last.review,
                    len(last.rule_rejected),
                    last.disabled_sources,
                    last.alerts,
                )

        return await run_locked(engine, self.name, step)
