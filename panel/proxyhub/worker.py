"""Worker process: runs every control loop, each under its own advisory lock."""

from __future__ import annotations

import asyncio
import logging
import signal

from proxyhub.cloudflare.delegation import DelegationReconciler
from proxyhub.db.session import get_engine
from proxyhub.reconcilers.account_guard import AccountGuardReconciler
from proxyhub.reconcilers.acme import AcmeReconciler
from proxyhub.reconcilers.analytics import AnalyticsReconciler, ChannelStatsReconciler
from proxyhub.reconcilers.base import Heartbeat, Reconciler
from proxyhub.reconcilers.dns import DnsReconciler
from proxyhub.reconcilers.domains import DomainsReconciler
from proxyhub.reconcilers.fleet import FleetReconciler
from proxyhub.reconcilers.mtproto import MtprotoReconciler
from proxyhub.reconcilers.mtproxy_account import MtproxyAccountReconciler
from proxyhub.reconcilers.nodes import NodesReconciler
from proxyhub.reconcilers.pricing import PricingReconciler
from proxyhub.reconcilers.probes import ProbesReconciler
from proxyhub.reconcilers.publisher import PublisherReconciler
from proxyhub.reconcilers.reality import RealityReconciler
from proxyhub.reconcilers.sponsor import SponsorReconciler
from proxyhub.reconcilers.telegram import (
    ApiWatchdogReconciler,
    TelegramOutboxReconciler,
    TelegramUpdatesReconciler,
)

log = logging.getLogger(__name__)


def reconcilers() -> list[Reconciler]:
    # budget, sponsor, node_health, ... are added milestone by milestone (docs/10, docs/11).
    # The fleet loop is live only when the owner turns dry-run off (proxyhub/reconcilers/fleet.py).
    return [
        Heartbeat(),
        AccountGuardReconciler(),
        PricingReconciler(),
        FleetReconciler(),
        NodesReconciler(),  # the test node and the live fleet's slot nodes (proxyhub.nodes.executor)
        TelegramUpdatesReconciler(),
        TelegramOutboxReconciler(),
        ApiWatchdogReconciler(),
        DnsReconciler(),  # net: no-op until a verified Cloudflare token and an active domain exist
        AcmeReconciler(),  # *.<active domain> certificate (Let's Encrypt DNS-01) for the ws_tls nodes
        ProbesReconciler(),  # net: records reachability verdicts from Iran only
        DomainsReconciler(),  # D35: filter checks of the pool domains, rotation, deletion of filtered zones
        MtproxyAccountReconciler(),  # D37: the operator account's session check (kill switch OFF only)
        AnalyticsReconciler(),  # read-only: server traffic/CPU every 15 min, channel size hourly, alerts
        ChannelStatsReconciler(),  # D38: channel statistics via the operator account (kill switch OFF only)
        MtprotoReconciler(),  # Telemt settings + one long-lived proxy secret per node (proxyhub.mtproto)
        SponsorReconciler(),  # D25/D37: @MTProxybot registration and sponsor channel (kill switch OFF only)
        DelegationReconciler(),  # fleet r3: hourly NS lookups; a lost registration leaves the pool
        PublisherReconciler(),  # posts: new posts on the day plan + status edits (test channel by default)
        RealityReconciler(),  # D18: fresh VLESS Reality configs per ready node, ended when used up
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
