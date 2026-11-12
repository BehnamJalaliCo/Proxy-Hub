"""Control loop "mtproto": applies :mod:`proxyhub.mtproto.policy` (Telemt settings and one
long-lived proxy secret per node, its extra links while a post needs more links than nodes run
(decision D49: the posting setting ``max_mtproto``), live detection, disabling the secrets of nodes
leaving service).

It only writes the panel's own desired state; the node agents pull it. No external API, no cost, so
it does not wait for the kill switch, and it never creates or deletes a server (rules 5 and 8).
Once an hour (every tick while a backlog remains) it also removes agent report rows older than
REPORT_RETENTION (proxyhub/agent_api/reports.py), in batches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.agent_api.reports import PRUNE_BATCH, prune_reports
from proxyhub.mtproto.keys import SealKeyUnavailable, load_proxy_seal_key
from proxyhub.mtproto.policy import PolicyOutcome, apply_policy
from proxyhub.mtproto.settings import MtprotoSettings, SettingsCorrupt, load_settings
from proxyhub.posts import settings as posting
from proxyhub.reconcilers.base import Reconciler

log = logging.getLogger(__name__)

PRUNE_EVERY = timedelta(hours=1)


class MtprotoReconciler(Reconciler):
    name = "mtproto"
    interval_seconds = 15.0

    def __init__(
        self,
        seal_key_loader: Callable[[], bytes] = load_proxy_seal_key,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.seal_key_loader = seal_key_loader
        self.clock = clock
        self.last_outcome: PolicyOutcome | None = None
        self._last_prune: datetime | None = None
        self._prune_backlog = False

    async def _prune(self, conn: AsyncConnection, now: datetime) -> None:
        if not self._prune_backlog and self._last_prune is not None and now - self._last_prune < PRUNE_EVERY:
            return
        self._last_prune = now
        removed = await prune_reports(conn, now)
        self._prune_backlog = removed >= PRUNE_BATCH
        if removed:
            log.info("mtproto: removed %d agent report rows past their retention", removed)

    async def run_once(self, conn: AsyncConnection) -> None:
        await self._prune(conn, datetime.now(UTC))  # retention is wall-clock time
        settings: MtprotoSettings | None
        try:
            settings = await load_settings(conn)
        except SettingsCorrupt:
            log.warning("mtproto: stored settings are invalid; nothing is configured until they are fixed")
            settings = None
        seal_key: bytes | None
        try:
            seal_key = self.seal_key_loader()
        except SealKeyUnavailable:
            log.warning("mtproto: the panel master key is not available; no proxy secret can be made")
            seal_key = None
        # The session joins the loop's transaction on this connection (it never commits it itself:
        # Reconciler.try_run_once commits, or rolls everything back on an error).
        # How many MTProto links a post carries (a stored value that does not validate gives the
        # defaults, never an error).
        links_per_post = (await posting.load(conn)).max_mtproto
        session = AsyncSession(bind=conn)
        try:
            self.last_outcome = await apply_policy(
                session, seal_key=seal_key, settings=settings, now=self.clock(), links_per_post=links_per_post
            )
        finally:
            await session.close()
