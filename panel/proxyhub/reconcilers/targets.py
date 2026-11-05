"""Targets loop (P15): the owner's candidate REALITY / fake-TLS sites, measured from Iran by real
TLS handshakes (MCI and Rightel, Globalping), and the ``auto`` target choice (proxyhub/targets).

Every minute:

1. our nodes' own TLS 1.3 + HTTP/2 checks of the candidate sites are recorded (``target_ok``);
2. when a round is due (every ``interval_h`` hours, a quick second look at a current target that
   looked closed, a site the owner just added) and Globalping's hourly allowance covers it plus a
   full node-probe cycle, the sites are measured (network I/O outside any transaction) and stored;
3. in ``auto`` mode, the Reality target and the MTProto fake-TLS domain follow the choice
   (proxyhub/targets/choice.py); every switch is logged and told to the owner in Persian. The
   Reality and MTProto loops then apply them by their own rules (proxyhub/reality/policy.py
   ``retarget_scopes``: active configs end at their own time unless the old target fails).

Nothing here touches Hetzner, money or a node's lifecycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from proxyhub.cloudflare.filtering import tighter_rate
from proxyhub.mtproto.settings import SettingsCorrupt, load_settings
from proxyhub.probes.globalping import GlobalpingClient, RateLimit
from proxyhub.probes.service import (
    globalping_rate,
    last_tls_probes,
    node_probe_reserve,
    ready_node_count,
    record_globalping_rate,
)
from proxyhub.probes.settings import load_probe_settings
from proxyhub.reality.settings import load_reality_settings
from proxyhub.reconcilers.base import Reconciler
from proxyhub.targets.service import (
    apply_auto,
    budget_allows,
    due_round,
    load_candidates,
    measure_round,
    record_node_checks,
    round_cost,
    store_round,
    target_locations,
)
from proxyhub.targets.settings import load_state, load_target_settings, save_state

log = logging.getLogger(__name__)

GpFactory = Callable[[], GlobalpingClient]


class TargetsReconciler(Reconciler):
    name = "targets"
    interval_seconds = 60.0

    def __init__(self, gp_factory: GpFactory = GlobalpingClient) -> None:
        self._gp_factory = gp_factory
        self._rate: RateLimit | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        # ``conn`` is never used, so its transaction never starts: reads and writes use their own
        # short transactions, never across network I/O (the domains loop does the same).
        engine = conn.engine
        now = datetime.now(UTC)
        async with engine.begin() as db:
            settings = await load_target_settings(db)
            if not settings.enabled:
                return
            probes = await load_probe_settings(db)
            reality = await load_reality_settings(db)
            try:
                mt_domain: str | None = (await load_settings(db)).tls_domain
            except SettingsCorrupt:
                mt_domain = None
            current = [h for h in (reality.target_host, mt_domain) if h]
            hosts = list(dict.fromkeys([*settings.candidates, *current]))
            await record_node_checks(db, hosts, now)
            states = await load_candidates(db)
            state = await load_state(db)
            keys = probes.key_networks
            due = due_round(state, settings, hosts, current, states, keys, now)
            reserve = node_probe_reserve(probes, await ready_node_count(db), await last_tls_probes(db))
            rate = tighter_rate(await globalping_rate(db), self._rate, now)
        if due is not None:
            locations = target_locations(probes)
            probes_per = sum(int(loc["limit"]) for loc in locations)
            cost = round_cost(due.hosts, probes_per, probes.tls_control_host)
            if budget_allows(rate, now, cost, reserve):
                async with self._gp_factory() as gp:
                    rnd = await measure_round(gp, due.hosts, probes.tls_control_host, locations)
                self._rate = rnd.rate or rate
                async with engine.begin() as db:
                    updated = await store_round(db, rnd)
                    if rnd.rate is not None:
                        await record_globalping_rate(db, rnd.rate)
                    state = await load_state(db)
                    state["last_attempt_at"] = rnd.started_at.isoformat()
                    if due.full and rnd.control.status == "ok":
                        state["last_full_at"] = rnd.started_at.isoformat()
                    await save_state(db, state)
                log.info(
                    "targets: %s round of %d site(s) from Iran (control %s): %d updated",
                    due.reason,
                    len(due.hosts),
                    rnd.control.status,
                    len(updated),
                )
            else:
                log.debug("targets: round postponed, Globalping's allowance is kept for the node probes")
        async with engine.begin() as db:
            for s in await apply_auto(db, settings, keys, datetime.now(UTC)):
                log.info("targets: %s target %s -> %s (auto, %s)", s.kind, s.old, s.new, s.why)
