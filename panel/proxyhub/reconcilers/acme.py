"""ACME loop (proxyhub/acme), every 5 minutes: the wildcard certificate of the active pool domain.

It does nothing until the owner has stored a verified Cloudflare token, chosen an active domain, and
the Reality transport is ``ws_tls`` (the certificate's only use). Then, in this order:

1. ``_acme-challenge`` records of ours left by an interrupted order are removed (purpose ``acme``,
   managed-record rule: only records with our marker AND our row).
2. Certificates of domains that left Cloudflare or were deleted from the pool are forgotten (vault
   entries and row).
3. If the active domain has no certificate or fewer than 30 days left, and its backoff has passed,
   ONE order runs (proxyhub/acme/service.py). A failure waits 1 h, 2 h, 4 h ... at most a day, or
   longer when the CA says so; the owner hears of it at most once a day.

Hetzner, the budget and the node caps are not involved: this only writes DNS records and the vault.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.acme.protocol import AcmeSession
from proxyhub.acme.service import (
    ACTOR,
    PURPOSE,
    AcmeOptions,
    ChallengeDns,
    CloudflareChallengeDns,
    SessionFactory,
    clear_challenges,
    order_certificate,
)
from proxyhub.acme.store import cert_row, cert_rows, forget, renewal_due
from proxyhub.cloudflare.client import CloudflareClient
from proxyhub.cloudflare.pool import PoolState
from proxyhub.cloudflare.rotation import GONE
from proxyhub.cloudflare.service import TOKEN_SECRET, ClientFactory, active_zone, load_state, zones_with_rows
from proxyhub.db.models_net import CfZone
from proxyhub.fleet.actions import log_action
from proxyhub.reality.settings import load_reality_settings
from proxyhub.reconcilers.base import Reconciler
from proxyhub.vault import Vault, VaultError, secret_exists

log = logging.getLogger(__name__)

DnsFactory = Callable[[AsyncEngine, CloudflareClient, str], ChallengeDns]

# Pool states whose certificate is kept (a standby may become active again; a filtered domain's
# names are gone, but its zone may come back to standby).
KEPT_STATES: Final = frozenset(
    {PoolState.ACTIVE.value, PoolState.STANDBY.value, PoolState.FILTERED.value, PoolState.NONE.value}
)


class AcmeReconciler(Reconciler):
    name = "acme"
    interval_seconds = 300.0
    max_backoff_seconds = 1800.0

    def __init__(
        self,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        client_factory: ClientFactory = CloudflareClient,
        options: Callable[[], AcmeOptions] = AcmeOptions.from_config,
        session_factory: SessionFactory = AcmeSession,
        dns_factory: DnsFactory = CloudflareChallengeDns,
    ) -> None:
        self._vault_factory = vault_factory
        self._client_factory = client_factory
        self._options = options
        self._session_factory = session_factory
        self._dns_factory = dns_factory

    async def run_once(self, conn: AsyncConnection) -> None:
        if not await secret_exists(conn, TOKEN_SECRET):
            return
        state = await load_state(conn)
        if state is None or not state.verified:
            return
        settings = await load_reality_settings(conn)
        try:
            vault = self._vault_factory()
            token = await vault.get(conn, TOKEN_SECRET)
        except VaultError as exc:
            log.warning("acme: vault unavailable (%s)", type(exc).__name__)
            return
        if token is None:
            return
        engine = conn.engine
        now = datetime.now(UTC)
        await self._forget_unused(conn, now)
        zone = await active_zone(conn)
        leftovers = await zones_with_rows(conn, PURPOSE)
        due = (
            zone is not None
            and settings.transport == "ws_tls"
            and renewal_due(row := await cert_row(conn, zone.name), now)
            and (row is None or row.retry_at is None or row.retry_at <= now)
        )
        if not leftovers and not due:
            return
        async with self._client_factory(token) as cf:
            for zone_id in leftovers:  # an order interrupted by a crash or a restart
                await clear_challenges(engine, cf, zone_id)
            if due and zone is not None:
                await order_certificate(
                    engine,
                    self._dns_factory(engine, cf, zone.id),
                    zone_id=zone.id,
                    domain=zone.name,
                    options=self._options(),
                    vault=vault,
                    now=now,
                    session_factory=self._session_factory,
                )

    async def _forget_unused(self, conn: AsyncConnection, now: datetime) -> None:
        """Forget the certificate of a domain that Cloudflare no longer lists or that the pool
        deleted (its names can never be served again)."""
        rows = await cert_rows(conn)
        if not rows:
            return
        zones = {
            r.name: (r.status, r.pool_state)
            for r in (await conn.execute(select(CfZone.name, CfZone.status, CfZone.pool_state))).all()
        }
        for row in rows:
            status, pool_state = zones.get(row.domain, (GONE, PoolState.DELETED.value))
            if status != GONE and pool_state in KEPT_STATES:
                continue
            if await forget(conn, row.domain):
                await log_action(
                    conn,
                    actor=ACTOR,
                    action="acme_certificate_forgotten",
                    target=f"*.{row.domain}",
                    dry_run=False,
                    allowed=True,
                    reasons=["zone_gone" if status == GONE else f"pool_{pool_state}"],
                    result="certificate and key removed from the vault",
                )
