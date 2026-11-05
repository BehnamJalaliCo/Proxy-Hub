"""DNS loop (docs/10 section 3.10, decision D13): slot<N>.<active domain> -> the node's IPv4, and a
slotless test node's own name t<8 hex>.<active domain> -> its IPv4 (from its first ready on, until it
leaves service), so every serving node has a name of its own (VLESS + WS + TLS, Telemt's TLS front).

It does nothing at all until the owner has stored a verified Cloudflare token AND chosen an active
domain in the pool. Then, every minute: desired records come from the database (the node holding
each slot), the zone's real records are read, and only the differences are applied, under the
managed-record rule (proxyhub/cloudflare/records.py): a record is changed or deleted only if it
carries the ``managed-by=proxy-hub`` comment AND our table lists it. Records the panel created on
a zone that is no longer active are removed (their names moved with the active domain). Every
change, conflict and refusal is written to actions_log. Once an hour the token is re-verified and
the zone list refreshed, so a domain the owner adds to the Proxy-Hub Cloudflare account shows up by
itself (D35). A token widened to another account stops all DNS changes until it is narrowed again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.cloudflare.client import CloudflareClient, CloudflareError
from proxyhub.cloudflare.service import (
    TOKEN_SECRET,
    ClientFactory,
    active_zone,
    desired_slot_records,
    load_state,
    reconcile_zone,
    refresh_token_and_zones,
    zones_with_rows,
)
from proxyhub.reconcilers.base import Reconciler
from proxyhub.vault import Vault, VaultError, secret_exists

log = logging.getLogger(__name__)

ACTOR = "dns"
ZONE_REFRESH = timedelta(hours=1)
MAX_API_CALLS_PER_RUN = 10


class DnsReconciler(Reconciler):
    name = "dns"
    interval_seconds = 60.0

    def __init__(
        self,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        client_factory: ClientFactory = CloudflareClient,
        max_api_calls: int = MAX_API_CALLS_PER_RUN,
    ) -> None:
        self._vault_factory = vault_factory
        self._client_factory = client_factory
        self._max_api_calls = max_api_calls
        self._known_conflicts: set[tuple[str, str, str]] = set()

    async def run_once(self, conn: AsyncConnection) -> None:
        if not await secret_exists(conn, TOKEN_SECRET):
            return  # owner has not set a token: nothing to do, nothing to call
        try:
            vault = self._vault_factory()
            token = await vault.get(conn, TOKEN_SECRET)
        except VaultError as exc:
            log.warning("dns: vault unavailable (%s)", type(exc).__name__)
            return
        if token is None:
            return
        engine = conn.engine
        now = datetime.now(UTC)
        async with self._client_factory(token) as cf:
            state = await load_state(conn)
            if (
                state is None
                or state.zones_refreshed_at is None
                or now - state.zones_refreshed_at >= ZONE_REFRESH
            ):
                async with engine.begin() as w:
                    await refresh_token_and_zones(w, cf, now, actor=ACTOR)
                state = await load_state(conn)
            if state is None or not state.verified:
                return
            zone = await active_zone(conn)
            if zone is None:
                return  # no active domain chosen: a safe no-op
            desired = await desired_slot_records(conn, zone.name)
            with_rows = await zones_with_rows(conn, "slot")
            if not desired and not with_rows:
                return  # no slot node and no record of ours anywhere: nothing to read or change
            others = [z for z in with_rows if z != zone.id]
            budget = self._max_api_calls
            seen_conflicts: set[tuple[str, str, str]] = set()
            for zone_id in [zone.id, *others]:
                if budget <= 0:
                    break
                try:
                    report = await reconcile_zone(
                        engine,
                        cf,
                        zone_id,
                        desired if zone_id == zone.id else [],
                        purpose="slot",
                        actor=ACTOR,
                        max_api_calls=budget,
                        known_conflicts=self._known_conflicts,
                    )
                except CloudflareError as exc:
                    log.warning("dns: zone %s skipped: %s", zone_id, exc)
                    continue
                budget -= report.api_calls
                seen_conflicts.update(report.conflicts)
                if report.stopped:
                    break
            # A conflict that has cleared is logged again if it ever comes back.
            self._known_conflicts &= seen_conflicts
