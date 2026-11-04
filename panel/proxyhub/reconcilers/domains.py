"""Domains loop (decision D35, docs/10 section 3.10): the filter check of every pool domain from
Iran, rotation on a ``filtered`` verdict, the "no standby left" reminder, and the deletion of
filtered zones from Cloudflare after the grace period.

Every minute:

1. **Checks.** Zones that are due (the active domain every 30 minutes, standby domains every 6
   hours, sooner to confirm a hijacked answer; proxyhub/cloudflare/filtering.py) get one Globalping
   DNS measurement each (at most :data:`MAX_CHECKS_PER_TICK`, 4 probes in Iran per measurement).
   The Globalping allowance is shared with the node probes: a check runs only if the remaining
   tests cover it plus a full node-probe cycle (otherwise it is skipped, and that is logged once).
   Each result is stored and decided in one short transaction (proxyhub/cloudflare/rotation.py).
2. **Reminder.** With no standby domain left after a filter event, the owner is reminded once a day.
3. **Deletion.** At most one filtered zone per tick is deleted from Cloudflare, only when every
   condition holds (proxyhub/cloudflare/deletion.py).

Network I/O never runs inside an open database transaction: reads use a short-lived connection,
each write its own short transaction. With no zone in the pool it reads a few rows and calls
nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.cloudflare import TOKEN_SECRET
from proxyhub.cloudflare.client import CloudflareClient
from proxyhub.cloudflare.deletion import deletable_zones, delete_filtered_zone
from proxyhub.cloudflare.filtering import (
    DomainRound,
    budget_allows,
    check_due,
    domain_dns_body,
    failed_round,
    parse_domain_round,
    suspicious,
    tighter_rate,
)
from proxyhub.cloudflare.pool import PoolState
from proxyhub.cloudflare.rotation import (
    ACTOR,
    GONE,
    PoolZone,
    last_check_at,
    load_zones,
    record_round,
    remind_no_standby,
    rounds_since,
)
from proxyhub.cloudflare.service import ClientFactory
from proxyhub.fleet.actions import iso, log_action
from proxyhub.probes.globalping import GlobalpingClient, RateLimit
from proxyhub.probes.service import (
    GpFactory,
    globalping_rate,
    node_probe_reserve,
    ready_node_count,
    record_globalping_rate,
)
from proxyhub.probes.settings import load_probe_settings
from proxyhub.reconcilers.base import Reconciler
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

MAX_CHECKS_PER_TICK = 2
MAX_DELETES_PER_TICK = 1
CHECKED_STATES = (PoolState.ACTIVE.value, PoolState.STANDBY.value)


@dataclass(frozen=True)
class Due:
    zone: PoolZone
    last_at: datetime | None


async def measure(gp: GlobalpingClient, name: str) -> DomainRound:
    """One Globalping DNS measurement of ``name`` from Iran (network I/O only)."""
    at = datetime.now(UTC)
    created = await gp.create(domain_dns_body(name))
    if created.status != "ok" or created.id is None:
        return failed_round(at, created.status)
    status, raw = await gp.wait(created.id)
    if raw is None:
        return failed_round(at, "error" if status == "ok" else status, ref=created.id)
    # A partial result after a timeout still counts for the probes that answered.
    return parse_domain_round(raw, at, ref=created.id, status="ok" if status == "ok" else "timeout")


async def due_checks(conn: AsyncConnection, now: datetime) -> list[Due]:
    """Pool zones due for a check: the active one first, then the longest unchecked."""
    due: list[Due] = []
    for z in await load_zones(conn):
        if z.pool_state not in CHECKED_STATES or z.status == GONE:
            continue
        last = await last_check_at(conn, z.id)
        if check_due(z.pool_state, last, suspicious(await rounds_since(conn, z.id, z.pool_changed_at)), now):
            due.append(Due(z, last))
    due.sort(
        key=lambda d: (d.zone.pool_state != PoolState.ACTIVE.value, d.last_at is not None, d.last_at or now)
    )
    return due


class DomainsReconciler(Reconciler):
    name = "domains"
    interval_seconds = 60.0

    def __init__(
        self,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        client_factory: ClientFactory = CloudflareClient,
        gp_factory: GpFactory = GlobalpingClient,
        max_checks: int = MAX_CHECKS_PER_TICK,
    ) -> None:
        self._vault_factory = vault_factory
        self._client_factory = client_factory
        self._gp_factory = gp_factory
        self._max_checks = max_checks
        self._rate: RateLimit | None = None
        self._skip_logged = False

    async def run_once(self, conn: AsyncConnection) -> None:
        # ``conn`` is never used, so its transaction never starts: every read below uses its own
        # short connection and every write its own short transaction, never across network I/O.
        engine = conn.engine
        await self.check_domains(engine)
        async with engine.begin() as db:
            await remind_no_standby(db, datetime.now(UTC))
        await self.delete_filtered(engine)

    # --- 1. filter checks --------------------------------------------------------------------

    async def check_domains(self, engine: AsyncEngine) -> int:
        now = datetime.now(UTC)
        async with engine.connect() as db:
            due = await due_checks(db, now)
            if not due:
                return 0
            settings = await load_probe_settings(db)
            reserve = node_probe_reserve(settings, await ready_node_count(db))
            rate = tighter_rate(await globalping_rate(db), self._rate, now)
        done = 0
        async with self._gp_factory() as gp:
            for item in due[: self._max_checks]:
                if not budget_allows(rate, datetime.now(UTC), reserve):
                    await self._skipped(engine, item.zone, rate, reserve)
                    break
                self._skip_logged = False
                rnd = await measure(gp, item.zone.name)
                rate = gp.last_rate or rate
                self._rate = rate
                async with engine.begin() as db:
                    outcome = await record_round(db, item.zone.id, rnd, datetime.now(UTC))
                    if gp.last_rate is not None:
                        await record_globalping_rate(db, gp.last_rate)
                done += 1
                if outcome is not None:
                    log.info(
                        "domains: %s (%s) checked from Iran: %d/%d hijacked -> %s",
                        item.zone.name,
                        item.zone.pool_state,
                        rnd.hijacked,
                        rnd.answered,
                        outcome.verdict,
                    )
        return done

    async def _skipped(
        self, engine: AsyncEngine, zone: PoolZone, rate: RateLimit | None, reserve: int
    ) -> None:
        """Too few Globalping tests left this hour: wait for the window to reset. Logged once per
        streak, not every minute."""
        if self._skip_logged:
            return
        self._skip_logged = True
        async with engine.begin() as db:
            await log_action(
                db,
                actor=ACTOR,
                action="domain_check_skipped",
                target=zone.name,
                dry_run=False,
                allowed=False,
                reasons=["globalping_budget_low"],
                context={
                    "remaining": rate.remaining if rate else None,
                    "reset_at": iso(rate.reset_at) if rate else None,
                    "reserved_for_nodes": reserve,
                },
                result="filter check postponed: Globalping's hourly allowance is kept for the node probes",
            )

    # --- 3. deleting filtered zones ------------------------------------------------------------

    async def delete_filtered(self, engine: AsyncEngine) -> list[str]:
        now = datetime.now(UTC)
        async with engine.connect() as db:
            zone_ids = await deletable_zones(db, now)
            if not zone_ids:
                return []
            try:
                token = await self._vault_factory().get(db, TOKEN_SECRET)
            except VaultError as exc:
                log.warning("domains: vault unavailable (%s)", type(exc).__name__)
                return []
        if token is None:
            return []
        outcomes: list[str] = []
        async with self._client_factory(token) as cf:
            for zone_id in zone_ids[:MAX_DELETES_PER_TICK]:
                outcomes.append(await delete_filtered_zone(engine, cf, zone_id, datetime.now(UTC)))
        return outcomes
