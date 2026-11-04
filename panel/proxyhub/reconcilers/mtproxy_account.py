"""The operator account's session check (decision D37, docs/11 section 2.1).

Every minute it looks (one short read) whether a check is due; a check is due 30 minutes after the
last one, only while a session is stored and the fleet's kill switch is OFF (an unreadable switch
counts as ON; docs/11 lock 6: the kill switch stops this module too). A check connects, calls
get_me and disconnects. If Telegram says the session was ended (AUTH_KEY_UNREGISTERED,
SESSION_REVOKED, USER_DEACTIVATED, ...), the account is marked ``disconnected``, the dead session is
removed from the vault and ONE Persian alert is queued for the owner (proxyhub/mtproxy/service.py).

It runs under the advisory lock ``mtproxy_account`` (the API's login and logout take the same one)
with no transaction open while Telegram answers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.mtproxy import service, store
from proxyhub.mtproxy.userclient import ClientOptions
from proxyhub.reconcilers.base import Reconciler
from proxyhub.reconcilers.telegram import run_locked
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)


class MtproxyAccountReconciler(Reconciler):
    name = store.LOCK_NAME
    interval_seconds = 60.0

    def __init__(
        self,
        *,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        options: ClientOptions | None = None,
    ) -> None:
        self._vault_factory = vault_factory
        self._vault: Vault | None = None
        self.options = options or ClientOptions()
        self.last_outcome: str | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            self.last_outcome = await self.check(engine)

        return await run_locked(engine, self.name, step)

    async def check(self, engine: AsyncEngine) -> str:
        if self._vault is None:
            try:
                self._vault = self._vault_factory()
            except VaultError as exc:
                log.warning("mtproxy check: vault unavailable (%s)", type(exc).__name__)
                return "vault_unavailable"
        deps = service.Deps(engine=engine, vault=self._vault, redis=None, options=self.options)
        return await service.check_session(deps)
