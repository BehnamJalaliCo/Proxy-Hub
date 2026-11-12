"""Worker process: runs every control loop, each under its own advisory lock."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.cloudflare.delegation import DelegationReconciler
from proxyhub.db.session import get_engine
from proxyhub.reconcilers.account_guard import AccountGuardReconciler
from proxyhub.reconcilers.acme import AcmeReconciler
from proxyhub.reconcilers.analytics import AnalyticsReconciler, ChannelStatsReconciler
from proxyhub.reconcilers.base import LOOP_KEY_PREFIX, Heartbeat, Reconciler
from proxyhub.reconcilers.content import ContentReconciler
from proxyhub.reconcilers.content_qc import ContentQcReconciler
from proxyhub.reconcilers.dns import DnsReconciler
from proxyhub.reconcilers.domains import DomainsReconciler
from proxyhub.reconcilers.fleet import FleetReconciler
from proxyhub.reconcilers.mtproto import MtprotoReconciler
from proxyhub.reconcilers.mtproxy_account import MtproxyAccountReconciler
from proxyhub.reconcilers.nodes import NodesReconciler
from proxyhub.reconcilers.operator_poster import (
    OperatorChecksReconciler,
    OperatorOutboxReconciler,
    OwnEmojiPackReconciler,
)
from proxyhub.reconcilers.pricing import PricingReconciler
from proxyhub.reconcilers.probes import ProbesReconciler
from proxyhub.reconcilers.publisher import PublisherReconciler
from proxyhub.reconcilers.reality import RealityReconciler
from proxyhub.reconcilers.sponsor import SponsorReconciler
from proxyhub.reconcilers.targets import TargetsReconciler
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
        ProbesReconciler(),  # P15: TLS handshakes from Iran decide (the live fleet burns on "blocked")
        TargetsReconciler(),  # P15: candidate REALITY / fake-TLS sites from Iran; auto target choice
        DomainsReconciler(),  # D35: filter checks of the pool domains, rotation, deletion of filtered zones
        MtproxyAccountReconciler(),  # D37: the operator account's session check (kill switch OFF only)
        AnalyticsReconciler(),  # read-only: server traffic/CPU every 15 min, channel size hourly, alerts
        ChannelStatsReconciler(),  # D38: channel statistics via the operator account (kill switch OFF only)
        MtprotoReconciler(),  # Telemt settings + one long-lived proxy secret per node (proxyhub.mtproto)
        SponsorReconciler(),  # D25/D37: @MTProxybot registration and sponsor channel (kill switch OFF only)
        DelegationReconciler(),  # fleet r3: hourly NS lookups; a lost registration leaves the pool
        ContentReconciler(),  # D51: the owner's sources (Telegram previews, RSS) into the review queue
        ContentQcReconciler(),  # D52: the supervisor: Claude QC, auto-approval, source quality, alerts
        PublisherReconciler(),  # posts: new posts on the day plan + status edits (test channel by default)
        OperatorOutboxReconciler(),  # D38: posts/edits as the operator account (Premium emoji)
        OperatorChecksReconciler(),  # D38: daily emoji pack + the account's rights in our channels
        OwnEmojiPackReconciler(),  # D38: the channel's own animated emoji pack, made by the bot
        RealityReconciler(),  # D18: fresh VLESS Reality configs per ready node, ended when used up
    ]


async def forget_other_loops(engine: AsyncEngine, names: Sequence[str]) -> None:
    """Outcome records of loops this worker no longer runs (renamed or removed) would read as
    stalled forever: they go at start. Best effort."""
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM settings WHERE key LIKE :prefix AND NOT (key = ANY(:keys))"),
                {"prefix": LOOP_KEY_PREFIX + "%", "keys": [LOOP_KEY_PREFIX + n for n in names]},
            )
    except Exception as exc:
        log.warning("worker: old loop records not removed (%s)", type(exc).__name__)


async def run(loops: Sequence[Reconciler] | None = None) -> None:
    """Run every loop until SIGTERM/SIGINT. A loop task that ends by itself (it catches every
    ordinary error, so only something like a cancellation can end it) stops the worker with exit
    code 1, so Docker restarts it instead of the worker running on without that loop."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine = get_engine()
    loops = reconcilers() if loops is None else list(loops)
    await forget_other_loops(engine, [r.name for r in loops])
    tasks = [asyncio.create_task(r.loop(engine, stop), name=r.name) for r in loops]
    log.info("worker started with %d reconcilers", len(tasks))
    stopping = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
    ended = [t for t in done if t is not stopping]
    for task in ended:
        reason = "cancelled" if task.cancelled() else repr(task.exception())
        log.critical("worker: loop %s ended by itself (%s); stopping the worker", task.get_name(), reason)
    stop.set()
    await asyncio.gather(*tasks, stopping, return_exceptions=True)
    await engine.dispose()
    log.info("worker stopped")
    if ended:
        raise SystemExit(1)
