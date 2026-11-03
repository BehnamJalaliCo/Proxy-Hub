"""Worker process: runs every control loop, each under its own advisory lock."""

from __future__ import annotations

import asyncio
import logging
import signal

from proxyhub.db.session import get_engine
from proxyhub.reconcilers.account_guard import AccountGuardReconciler
from proxyhub.reconcilers.base import Heartbeat, Reconciler
from proxyhub.reconcilers.dns import DnsReconciler
from proxyhub.reconcilers.fleet import FleetReconciler
from proxyhub.reconcilers.pricing import PricingReconciler
from proxyhub.reconcilers.probes import ProbesReconciler

log = logging.getLogger(__name__)


def reconcilers() -> list[Reconciler]:
    # budget, sponsor, node_health, ... are added milestone by milestone (docs/10, docs/11).
    # The fleet loop is dry-run only (proxyhub.reconcilers.fleet.FLEET_LIVE_ENABLED).
    return [
        Heartbeat(),
        AccountGuardReconciler(),
        PricingReconciler(),
        FleetReconciler(),
        DnsReconciler(),  # net: no-op until a verified Cloudflare token and an active domain exist
        ProbesReconciler(),  # net: records reachability verdicts from Iran only
    ]


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine = get_engine()
    tasks = [asyncio.create_task(r.loop(engine, stop), name=r.name) for r in reconcilers()]
    log.info("worker started with %d reconcilers", len(tasks))
    await stop.wait()
    await asyncio.gather(*tasks, return_exceptions=True)
    await engine.dispose()
    log.info("worker stopped")
