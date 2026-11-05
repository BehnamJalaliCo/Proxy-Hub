"""The operator poster's loops (decision D38; proxyhub/posts/operator_outbox.py, operator_checks.py).

* ``operator_outbox``: one ``operator_send_post`` / ``operator_edit_post`` job per step, every second
  while there is work (0.2 s right after a job), under the operator account's lock and the bot's
  per-channel pacing. With no job due it only reads the outbox; the account's lock is not taken.
* ``operator_checks``: every minute it looks (one short read) whether the daily read of the owner's
  emoji pack and of the account's rights in our channels is due, and does it then.

Both stop while the fleet's kill switch is ON (posts then go with the bot, edits wait). Each runs
under its own advisory lock, with no transaction open while Telegram answers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.mtproxy.userclient import ClientOptions
from proxyhub.posts import operator_checks, operator_outbox
from proxyhub.reconcilers.base import Reconciler
from proxyhub.reconcilers.telegram import run_locked
from proxyhub.telegram.store import now_utc
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)


class _VaultOnce:
    def __init__(self, factory: Callable[[], Vault]) -> None:
        self._factory = factory
        self._vault: Vault | None = None

    def get(self) -> Vault | None:
        if self._vault is None:
            try:
                self._vault = self._factory()
            except VaultError as exc:
                log.warning("operator poster: vault unavailable (%s)", type(exc).__name__)
                return None
        return self._vault


class OperatorOutboxReconciler(Reconciler):
    name = "operator_outbox"
    interval_seconds = 1.0

    def __init__(
        self,
        *,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        options: ClientOptions | None = None,
        lock_wait_s: float = operator_outbox.DEFAULT_LOCK_WAIT_S,
    ) -> None:
        self._vault = _VaultOnce(vault_factory)
        self.options = options
        self.lock_wait_s = lock_wait_s
        self._deps: operator_outbox.Deps | None = None
        self.last_outcome: str | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            self.last_outcome = await self.step(engine)
            busy = self.last_outcome in ("sent", "retry", "failed", "handed_to_bot", "deferred")
            self.interval_seconds = 0.2 if busy else 1.0

        return await run_locked(engine, self.name, step)

    async def step(self, engine: AsyncEngine) -> str:
        if self._deps is None or self._deps.engine is not engine:
            vault = self._vault.get()
            if vault is None:
                return "vault_unavailable"
            self._deps = operator_outbox.Deps(
                engine=engine, vault=vault, options=self.options, lock_wait_s=self.lock_wait_s
            )
        return await operator_outbox.process_one(self._deps)


class OperatorChecksReconciler(Reconciler):
    name = "operator_checks"
    interval_seconds = 60.0

    def __init__(
        self,
        *,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        options: ClientOptions | None = None,
    ) -> None:
        self._vault = _VaultOnce(vault_factory)
        self.options = options
        self.last_outcome: str | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            self.last_outcome = await self.step(engine)

        return await run_locked(engine, self.name, step)

    async def step(self, engine: AsyncEngine) -> str:
        vault = self._vault.get()
        if vault is None:
            return "vault_unavailable"
        return await operator_checks.run_checks(engine, vault, now=now_utc(), options=self.options)
