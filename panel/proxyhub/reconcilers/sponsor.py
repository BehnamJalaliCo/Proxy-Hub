"""The ``sponsor`` loop (docs/11 section 2 row 7, decisions D25 and D37): every live MTProto proxy
registered in @MTProxybot with the wanted Proxy Sponsor (@ipoxxo, or a paying customer's channel
during a campaign, and back at its end). One pass is :func:`proxyhub.sponsor.service.run_once`: at
most ONE conversation with the bot, at a human pace, under the operator account's lock, and nothing
at all while the kill switch is ON.

It runs under its own advisory lock ``sponsor`` (two workers never both act) with no transaction
open while Telegram answers. The live proxies come from team mtproto's provider
(:data:`proxyhub.sponsor.provider.MTPROTO_PROVIDER`); with no provider (tests only), the loop only
keeps the campaign states up to date.

A pass with nothing to do for a live proxy (``idle``, or ``paused``) runs one step of the owner's
job on @MTProxybot's own list instead (:func:`proxyhub.sponsor.inventory.step`): the inventory, the
promotion check, or the cleanup of proxies whose node was deleted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.mtproxy.userclient import ClientOptions
from proxyhub.reconcilers.base import Reconciler
from proxyhub.reconcilers.telegram import run_locked
from proxyhub.sponsor import inventory, service
from proxyhub.sponsor import provider as provider_module
from proxyhub.sponsor.provider import SponsorProvider
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)


class SponsorReconciler(Reconciler):
    name = "sponsor"
    interval_seconds = 60.0

    def __init__(
        self,
        *,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        provider: Callable[[], SponsorProvider | None] = lambda: provider_module.MTPROTO_PROVIDER,
        options: ClientOptions | None = None,
    ) -> None:
        self._vault_factory = vault_factory
        self._vault: Vault | None = None
        self._provider = provider
        self.options = options or ClientOptions()
        self.last_outcome: str | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            self.last_outcome = await self.step(engine)

        return await run_locked(engine, self.name, step)

    async def step(self, engine: AsyncEngine) -> str:
        if self._vault is None:
            try:
                self._vault = self._vault_factory()
            except VaultError as exc:
                log.warning("sponsor: vault unavailable (%s)", type(exc).__name__)
                return "vault_unavailable"
        deps = service.Deps(engine=engine, vault=self._vault, provider=self._provider(), options=self.options)
        outcome = await service.run_once(deps)
        if outcome not in ("idle", "paused"):
            return outcome
        # Nothing for a live proxy this pass: one step of the owner's job on @MTProxybot's own list
        # (inventory, promotion check, cleanup), if any. A removal waits while conversations pause.
        listed = await inventory.step(deps, paused=outcome == "paused")
        return outcome if listed == "idle" else listed
