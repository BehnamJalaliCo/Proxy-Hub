"""Pricing loop: live server-type prices and availability per account, hourly (finding H1).

Hetzner raised prices twice in 2026, so nothing is hard-coded: the planner and the budget only
ever use the offers stored here. A changed or vanished price is written to actions_log.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.accounts.hetzner_io import HetznerFailure, LiveOffer, call_hetzner, read_offers
from proxyhub.accounts.service import PriceChange, store_offers
from proxyhub.accounts.state import OBSERVED_STATES
from proxyhub.db.models import HetznerAccount
from proxyhub.db.models_fleet import HetznerOffer
from proxyhub.fleet.actions import log_action
from proxyhub.hetzner.client import HetznerReader
from proxyhub.reconcilers.base import Reconciler
from proxyhub.vault import SecretValue, Vault, hcloud_secret_name

log = logging.getLogger(__name__)

PRICING_INTERVAL = timedelta(hours=1)
ACTOR = "pricing"


def _fetch_offers(reader_factory: Callable[[str], HetznerReader], token: SecretValue) -> list[LiveOffer]:
    return read_offers(reader_factory(token.reveal()))


async def accounts_with_fetch_time(conn: AsyncConnection) -> list[tuple[uuid.UUID, str, datetime | None]]:
    last_fetch = (
        select(HetznerOffer.account_id, func.max(HetznerOffer.fetched_at).label("fetched_at"))
        .group_by(HetznerOffer.account_id)
        .subquery()
    )
    rows = (
        await conn.execute(
            select(HetznerAccount.id, HetznerAccount.name, last_fetch.c.fetched_at)
            .outerjoin(last_fetch, last_fetch.c.account_id == HetznerAccount.id)
            .where(HetznerAccount.state.in_([s.value for s in OBSERVED_STATES]))
        )
    ).all()
    return [(r.id, r.name, r.fetched_at) for r in rows]


async def log_price_changes(
    conn: AsyncConnection, account_id: uuid.UUID, account_name: str, changes: list[PriceChange]
) -> int:
    """Changed or vanished prices only (a first fetch is not a change)."""
    logged = 0
    for c in changes:
        if c.old_monthly is None:
            continue
        await log_action(
            conn,
            actor=ACTOR,
            action="pricing_change",
            target=f"{c.server_type}@{c.location}",
            dry_run=False,
            allowed=True,
            reasons=["price_changed" if c.new_monthly is not None else "offer_gone"],
            context={
                "account_id": str(account_id),
                "account_name": account_name,
                "old_monthly_eur": str(c.old_monthly),
                "new_monthly_eur": str(c.new_monthly) if c.new_monthly is not None else None,
            },
            result=f"{c.server_type} in {c.location}: {c.old_monthly} -> {c.new_monthly}",
        )
        logged += 1
    return logged


class PricingReconciler(Reconciler):
    name = "pricing"
    interval_seconds = 300.0

    def __init__(
        self,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        reader_factory: Callable[[str], HetznerReader] = HetznerReader,
    ) -> None:
        self._vault_factory = vault_factory
        self._reader_factory = reader_factory
        self._fetched_since_start: set[uuid.UUID] = set()

    def is_due(self, account_id: uuid.UUID, fetched_at: datetime | None, now: datetime) -> bool:
        if account_id not in self._fetched_since_start:
            return True
        return fetched_at is None or now - fetched_at >= PRICING_INTERVAL

    async def run_once(self, conn: AsyncConnection) -> None:
        now = datetime.now(UTC)
        due = [(i, n) for i, n, f in await accounts_with_fetch_time(conn) if self.is_due(i, f, now)]
        if not due:
            return
        vault = self._vault_factory()
        reader_factory = self._reader_factory
        for account_id, account_name in due:
            self._fetched_since_start.add(account_id)
            # One account per savepoint, as in the account guard: one bad account never undoes
            # the prices stored for the others.
            try:
                async with conn.begin_nested():
                    await self._fetch_one(conn, vault, reader_factory, account_id, account_name, now)
            except Exception as exc:
                log.warning("pricing for %s: internal error %s", account_name, type(exc).__name__)

    async def _fetch_one(
        self,
        conn: AsyncConnection,
        vault: Vault,
        reader_factory: Callable[[str], HetznerReader],
        account_id: uuid.UUID,
        account_name: str,
        now: datetime,
    ) -> None:
        token = await vault.get(conn, hcloud_secret_name(account_id))
        if token is None:
            log.warning("pricing: account %s has no token in the vault", account_name)
            return
        try:
            offers = await call_hetzner(partial(_fetch_offers, reader_factory, token))
        except HetznerFailure as failure:
            log.warning("pricing for %s failed: %s", account_name, failure)
            return
        changes = await store_offers(conn, account_id, offers, now)
        await log_price_changes(conn, account_id, account_name, changes)
