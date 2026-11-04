"""Analytics loops (worker).

* ``analytics``: every 15 minutes, for each of OUR live nodes (a ``nodes`` row with a Hetzner id in an
  account we still read, AND the server carries ``managed-by=proxy-hub``), one READ-ONLY look at
  Hetzner: the server's traffic counters and the last 15 minutes of CPU and bandwidth
  (proxyhub/analytics/hetzner_usage.py). Then the threshold alerts (once per node, limit and Tehran
  day). Every hour, the member count of the main and test channels (getChatMemberCount).
* ``analytics_channel_stats``: every 3 hours, Telegram's own channel statistics through the operator
  account (decision D38; proxyhub/mtproxy/channel_stats.py). Only while the fleet's kill switch is
  OFF and the account is connected, under the operator account's lock (one Telegram operation on
  that account at a time).

Both hold their advisory lock with no transaction open while Hetzner or Telegram answers. Nothing is
created, changed or deleted at Hetzner or Telegram. Failures are recorded as short codes in the
``settings`` row ``analytics.status`` (never a token, never an exception text).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.accounts.hetzner_io import HetznerFailure, call_hetzner
from proxyhub.analytics import sampling
from proxyhub.analytics.hetzner_usage import LoadWindow, ServerUsage, read_load, read_managed_usage
from proxyhub.fleet.actions import iso
from proxyhub.hetzner.client import HetznerReader
from proxyhub.mtproxy import channel_stats
from proxyhub.mtproxy import store as mtp_store
from proxyhub.mtproxy.userclient import ClientOptions
from proxyhub.reconcilers.base import Reconciler
from proxyhub.reconcilers.telegram import run_locked
from proxyhub.telegram import store
from proxyhub.telegram.alerts import BotFactory
from proxyhub.telegram.client import BotClient, TelegramError, Unauthorized
from proxyhub.vault import Vault, VaultError, hcloud_secret_name

log = logging.getLogger(__name__)

STATUS_KEY: Final = "analytics.status"
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def record_status(engine: AsyncEngine, part: str, value: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        current = await store.get_setting(conn, STATUS_KEY) or {}
        current[part] = value
        await store.put_setting(conn, STATUS_KEY, current)


class AnalyticsReconciler(Reconciler):
    name = "analytics"
    interval_seconds = 60.0

    def __init__(
        self,
        *,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        reader_factory: Callable[[str], HetznerReader] = HetznerReader,
        bot_factory: BotFactory = BotClient,
        clock: Clock = _utcnow,
    ) -> None:
        self._vault_factory = vault_factory
        self._vault: Vault | None = None
        self._reader_factory = reader_factory
        self._bot_factory = bot_factory
        self._clock = clock
        self.next_nodes_at: datetime | None = None
        self.next_channels_at: datetime | None = None

    def vault(self) -> Vault:
        if self._vault is None:
            self._vault = self._vault_factory()
        return self._vault

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            await self.step(engine)

        return await run_locked(engine, self.name, step)

    async def step(self, engine: AsyncEngine) -> None:
        now = self._clock()
        if self.next_nodes_at is None or self.next_channels_at is None:
            async with engine.connect() as conn:
                last_node = await sampling.last_node_sample_at(conn)
                last_channel = await sampling.last_channel_sample_at(conn)
            self.next_nodes_at = last_node + sampling.NODE_INTERVAL if last_node else now
            self.next_channels_at = last_channel + sampling.CHANNEL_INTERVAL if last_channel else now
        if now >= self.next_nodes_at:
            self.next_nodes_at = now + sampling.NODE_INTERVAL
            await self.sample_nodes(engine, now)
        if now >= self.next_channels_at:
            self.next_channels_at = now + sampling.CHANNEL_INTERVAL
            await self.sample_channels(engine, now)

    # --- servers ----------------------------------------------------------------------------------

    async def sample_nodes(self, engine: AsyncEngine, now: datetime) -> int:
        """One sample per sampleable node; then the threshold alerts. Returns how many were stored."""
        at = sampling.minute(now)
        async with engine.connect() as conn:
            nodes = await sampling.sampleable_nodes(conn)
        errors: dict[str, str] = {}
        stored = 0
        if nodes:
            try:
                vault = self.vault()
            except VaultError as exc:
                await record_status(engine, "nodes", {"at": iso(now), "error": "vault_unavailable"})
                log.warning("analytics: vault unavailable (%s)", type(exc).__name__)
                return 0
            groups: dict[uuid.UUID, list[sampling.SampledNode]] = defaultdict(list)
            for n in nodes:
                groups[n.account_id].append(n)
            for account_id, group in groups.items():
                try:
                    stored += await self._sample_account(engine, vault, account_id, group, at)
                except HetznerFailure as failure:
                    errors[group[0].account_name] = failure.kind
                    log.warning("analytics: account %s: %s", group[0].account_name, failure)
                except Exception as exc:  # one account must never stop the others
                    errors[group[0].account_name] = "internal_error"
                    log.warning(
                        "analytics: account %s: internal error %s", group[0].account_name, type(exc).__name__
                    )
        async with engine.begin() as conn:
            await sampling.raise_threshold_alerts(conn, now)
        await record_status(
            engine,
            "nodes",
            {"at": iso(now), "nodes": len(nodes), "sampled": stored, "errors": errors or None},
        )
        return stored

    async def _sample_account(
        self,
        engine: AsyncEngine,
        vault: Vault,
        account_id: uuid.UUID,
        group: list[sampling.SampledNode],
        at: datetime,
    ) -> int:
        async with engine.connect() as conn:
            token = await vault.get(conn, hcloud_secret_name(account_id))
        if token is None:
            raise HetznerFailure("token_invalid", "no_token_in_vault")
        reader = self._reader_factory(token.reveal())
        usage: list[ServerUsage] = await call_hetzner(partial(read_managed_usage, reader))
        by_id = {u.hetzner_id: u for u in usage}
        loads: dict[uuid.UUID, LoadWindow | None] = {}
        for node in group:
            u = by_id.get(node.hetzner_id)
            if u is None:  # not labelled ours at Hetzner (or gone): never sampled
                continue
            try:
                loads[node.node_id] = await call_hetzner(
                    partial(read_load, reader, node.hetzner_id, at - sampling.NODE_INTERVAL, at, u.cores)
                )
            except HetznerFailure as failure:
                log.warning("analytics: metrics of %s: %s", node.name, failure)
                loads[node.node_id] = None
        stored = 0
        async with engine.begin() as conn:
            previous = await sampling.last_counters(conn, [n.node_id for n in group])
            for node in group:
                u = by_id.get(node.hetzner_id)
                if u is None:
                    continue
                if await sampling.store_node_sample(
                    conn, node, u, loads.get(node.node_id), at, previous.get(node.node_id)
                ):
                    stored += 1
        return stored

    # --- channels ---------------------------------------------------------------------------------

    async def sample_channels(self, engine: AsyncEngine, now: datetime) -> int:
        at = sampling.minute(now)
        try:
            vault = self.vault()
            async with engine.connect() as conn:
                creds = await store.load_bot(conn, vault)
                channels = await store.channels(conn)
        except VaultError:
            await record_status(engine, "channels", {"at": iso(now), "error": "vault_unavailable"})
            return 0
        targets = {r: c["chat_id"] for r, c in channels.items() if isinstance(c.get("chat_id"), int)}
        if creds is None or not targets:
            await record_status(
                engine,
                "channels",
                {"at": iso(now), "error": "bot_not_set" if creds is None else "no_channel"},
            )
            return 0
        counts: dict[str, int] = {}
        errors: dict[str, str] = {}
        try:
            async with self._bot_factory(creds.token) as bot:
                for role, chat_id in targets.items():
                    try:
                        counts[role] = await bot.get_chat_member_count(chat_id)
                    except Unauthorized:
                        raise
                    except TelegramError as exc:
                        errors[role] = type(exc).__name__
        except Unauthorized:
            errors = dict.fromkeys(targets, "bot_unauthorized")
        stored = 0
        async with engine.begin() as conn:
            for role, count in counts.items():
                if await sampling.store_channel_sample(conn, role, targets[role], at, count):
                    stored += 1
        await record_status(engine, "channels", {"at": iso(now), "sampled": stored, "errors": errors or None})
        return stored


# --- channel statistics through the operator account (D38) ----------------------------------------------

CHANNEL_STATS_INTERVAL: Final = timedelta(hours=3)


class ChannelStatsReconciler(Reconciler):
    name = "analytics_channel_stats"
    interval_seconds = 60.0

    def __init__(
        self,
        *,
        vault_factory: Callable[[], Vault] = Vault.from_config,
        options: ClientOptions | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        self._vault_factory = vault_factory
        self._vault: Vault | None = None
        self.options = options or ClientOptions()
        self._clock = clock
        self.next_at: datetime | None = None
        self.last_outcome: str | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        now = self._clock()
        if self.next_at is not None and now < self.next_at:
            return False

        async def step() -> None:
            if self._vault is None:
                try:
                    self._vault = self._vault_factory()
                except VaultError:
                    self.last_outcome = "vault_unavailable"
                    return
            self.last_outcome = await channel_stats.collect(
                engine, self._vault, options=self.options, now=now
            )
            if self.last_outcome not in ("kill_switch", "not_connected", "no_channel"):
                self.next_at = now + CHANNEL_STATS_INTERVAL
            await record_status(engine, "channel_stats", {"at": iso(now), "outcome": self.last_outcome})

        # The operator account's own lock: never at the same time as a login, logout or check.
        return await run_locked(engine, mtp_store.LOCK_NAME, step)
