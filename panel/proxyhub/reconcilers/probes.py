"""Probes loop (docs/10 M4.1): reachability of every READY node from Iran, every 10 minutes per
node by default (settings key ``probes``), plus rounds the owner asks for (POST /api/probes/run).

Each tick (one minute) takes up to ``max_nodes_per_tick`` due nodes, measures them together with
one shared control target per source, stores the results and the recomputed verdicts. Verdicts
are only recorded: nothing is replaced or deleted because of them in this round. With no READY
node (today) it does nothing at all, and calls nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.cloudflare.service import active_zone
from proxyhub.probes.checkhost import CheckHostClient
from proxyhub.probes.globalping import GlobalpingClient
from proxyhub.probes.service import (
    ChFactory,
    GpFactory,
    due_targets,
    globalping_rate,
    prune,
    run_cycle,
    store_cycle,
)
from proxyhub.probes.settings import load_probe_settings
from proxyhub.reconcilers.base import Reconciler

log = logging.getLogger(__name__)

PRUNE_EVERY = timedelta(hours=1)


class ProbesReconciler(Reconciler):
    name = "probes"
    interval_seconds = 60.0

    def __init__(
        self, gp_factory: GpFactory = GlobalpingClient, ch_factory: ChFactory = CheckHostClient
    ) -> None:
        self._gp_factory = gp_factory
        self._ch_factory = ch_factory
        self._last_prune: datetime | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        now = datetime.now(UTC)
        settings = await load_probe_settings(conn)
        if self._last_prune is None or now - self._last_prune >= PRUNE_EVERY:
            self._last_prune = now
            removed = await prune(conn, settings, now)
            if removed:
                log.info("probes: removed %d cycles older than %d days", removed, settings.retention_days)
        if not settings.enabled:
            return
        zone = await active_zone(conn)
        targets = await due_targets(conn, settings, now, zone.name if zone else None)
        if not targets:
            return
        result = await run_cycle(
            targets,
            settings,
            gp_factory=self._gp_factory,
            ch_factory=self._ch_factory,
            gp_rate=await globalping_rate(conn),
        )
        verdicts = await store_cycle(conn, result, targets, settings)
        log.info(
            "probes: %d node(s) measured: %s",
            len(verdicts),
            ", ".join(f"{t.name}={verdicts[t.node_id][0]}" for t in targets if t.node_id in verdicts),
        )
