"""Telegram control loops (worker):

* ``telegram_updates``: long-polls getUpdates (only while a bot is set) with a persisted offset.
  ``/start <code>`` from a PRIVATE chat with the open pairing code binds the owner's chat. Every
  other message is ignored and nothing about it is stored (not even in ``tg_updates``).
* ``telegram_outbox``: one outbox job per step (proxyhub/telegram/outbox.py).
* ``telegram_api_watchdog``: GET http://api:8000/livez every 60 s; after 3 failures in a row a
  Persian alert goes DIRECTLY to the owner (bot token from the vault), and a recovery message when
  the API answers again.

The first two hold their advisory lock without an open transaction while they talk to Telegram
(a 25 s long poll must not keep a transaction idle).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.fleet.actions import iso, log_action
from proxyhub.reconcilers.base import Reconciler, lock_key
from proxyhub.telegram import outbox, pairing, store
from proxyhub.telegram.alerts import BotFactory, OwnerTarget, send_direct
from proxyhub.telegram.client import BotClient, Conflict, TelegramError, Unauthorized, Update
from proxyhub.telegram.messages import API_BACK_FA, API_DOWN_FA, PAIRED_FA
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

DEFAULT_LIVEZ_URL: Final = "http://api:8000/livez"
ALLOWED_UPDATES: Final = ("message",)

VaultFactory = Callable[[], Vault]


async def run_locked(engine: AsyncEngine, name: str, step: Callable[[], Awaitable[None]]) -> bool:
    """Run ``step`` under the loop's session advisory lock, with no transaction held meanwhile."""
    async with engine.connect() as lock_conn:
        got = (
            await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key(name)})
        ).scalar_one()
        await lock_conn.commit()
        if not got:
            return False
        try:
            await step()
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(name)})
            await lock_conn.commit()
    return True


# --- updates / owner pairing ---------------------------------------------------------------------


async def handle_update(conn: AsyncConnection, update: Update, now_: datetime) -> int | None:
    """Apply one update. Returns the owner chat id if it just paired, else None."""
    message = update.private_text()
    if message is None:
        return None
    chat_id, body = message
    argument = pairing.start_argument(body)
    if argument is None:
        return None
    code = pairing.normalize(argument)
    if code is not None and await store.consume_pairing(conn, code, now_):
        await store.set_owner_chat(conn, chat_id, now_)
        await log_action(
            conn,
            actor="telegram",
            action="telegram.owner_paired",
            target="owner_chat",
            dry_run=False,
            allowed=True,
            reasons=["pairing_code"],
        )
        return chat_id
    if await store.count_wrong_code(conn, now_):
        await log_action(
            conn,
            actor="telegram",
            action="telegram.pairing_cancelled",
            target="owner_chat",
            dry_run=False,
            allowed=False,
            reasons=["too_many_wrong_codes"],
        )
    return None


class TelegramUpdatesReconciler(Reconciler):
    name = "telegram_updates"
    interval_seconds = 1.0
    IDLE_SECONDS: Final = 15.0
    ERROR_SECONDS: Final = 30.0

    def __init__(
        self,
        *,
        vault_factory: VaultFactory = Vault.from_config,
        bot_factory: BotFactory = BotClient,
        long_poll_s: int = 25,
    ) -> None:
        self.vault_factory = vault_factory
        self.bot_factory = bot_factory
        self.long_poll_s = long_poll_s

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            await self.poll(engine)

        return await run_locked(engine, self.name, step)

    async def _record(self, engine: AsyncEngine, bot_id: int, offset: int | None, error: str | None) -> None:
        async with engine.begin() as conn:
            await store.put_setting(
                conn,
                store.KEY_UPDATES,
                {"bot_id": bot_id, "offset": offset, "last_poll_at": iso(store.now_utc()), "error": error},
            )
            if error == "bot_unauthorized":
                await store.set_health(conn, error, store.now_utc())

    async def poll(self, engine: AsyncEngine) -> int:
        """One getUpdates call; returns how many updates were handled."""
        vault = self.vault_factory()
        async with engine.begin() as conn:
            creds = await store.load_bot(conn, vault)
            state = await store.get_setting(conn, store.KEY_UPDATES) or {}
        if creds is None:
            self.interval_seconds = self.IDLE_SECONDS
            return 0
        saved = state.get("offset")
        offset = saved if state.get("bot_id") == creds.info.id and isinstance(saved, int) else None
        try:
            async with self.bot_factory(creds.token) as bot:
                updates = await bot.get_updates(
                    offset=offset, long_poll_s=self.long_poll_s, allowed_updates=ALLOWED_UPDATES
                )
        except Unauthorized:
            await self._record(engine, creds.info.id, offset, "bot_unauthorized")
            self.interval_seconds = 60.0
            return 0
        except Conflict:  # another poller or a webhook holds this bot's updates
            await self._record(engine, creds.info.id, offset, "updates_conflict")
            self.interval_seconds = self.ERROR_SECONDS
            return 0
        except TelegramError as exc:
            log.warning("telegram updates: %s", exc)
            await self._record(engine, creds.info.id, offset, "telegram_unavailable")
            self.interval_seconds = self.ERROR_SECONDS
            return 0
        self.interval_seconds = 0.5
        paired: list[int] = []
        async with engine.begin() as conn:
            now = store.now_utc()
            for update in updates:
                chat_id = await handle_update(conn, update, now)
                if chat_id is not None:
                    paired.append(chat_id)
            if updates:
                offset = max(u.update_id for u in updates) + 1
            await store.put_setting(
                conn,
                store.KEY_UPDATES,
                {"bot_id": creds.info.id, "offset": offset, "last_poll_at": iso(now), "error": None},
            )
        for chat_id in paired[-1:]:
            try:
                await send_direct(
                    self.bot_factory, OwnerTarget(token=creds.token, chat_id=chat_id), PAIRED_FA
                )
            except TelegramError as exc:
                log.warning("telegram updates: pairing confirmation not delivered (%s)", exc)
        return len(updates)


# --- outbox --------------------------------------------------------------------------------------


class TelegramOutboxReconciler(Reconciler):
    name = "telegram_outbox"
    interval_seconds = 1.0

    def __init__(
        self, *, vault_factory: VaultFactory = Vault.from_config, bot_factory: BotFactory = BotClient
    ) -> None:
        self.vault_factory = vault_factory
        self.bot_factory = bot_factory
        self.last_outcome: str | None = None

    async def run_once(self, conn: AsyncConnection) -> None:
        raise NotImplementedError("this loop manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async def step() -> None:
            self.last_outcome = await outbox.process_one(engine, self.vault_factory(), self.bot_factory)
            # Busy: next job at once (pacing is enforced per chat); nothing to do: wait a little.
            self.interval_seconds = 0.2 if self.last_outcome in ("sent", "retry", "failed") else 1.0

        return await run_locked(engine, self.name, step)


# --- API watchdog --------------------------------------------------------------------------------


class ApiWatchdogReconciler(Reconciler):
    name = "telegram_api_watchdog"
    interval_seconds = 60.0

    def __init__(
        self,
        *,
        url: str = DEFAULT_LIVEZ_URL,
        failures_to_alert: int = 3,
        probe_timeout_s: float = 5.0,
        vault_factory: VaultFactory = Vault.from_config,
        bot_factory: BotFactory = BotClient,
    ) -> None:
        self.url = url
        self.failures_to_alert = failures_to_alert
        self.probe_timeout_s = probe_timeout_s
        self.vault_factory = vault_factory
        self.bot_factory = bot_factory
        self.failures = 0
        self.alerted = False

    async def probe(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.probe_timeout_s, trust_env=False) as client:
                response = await client.get(self.url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def notify(self, conn: AsyncConnection, message: str) -> bool:
        try:
            creds = await store.load_bot(conn, self.vault_factory())
        except VaultError as exc:
            log.warning("api watchdog: vault unavailable (%s)", type(exc).__name__)
            return False
        chat_id = await store.owner_chat_id(conn)
        if creds is None or chat_id is None:
            log.warning("api watchdog: no bot or no paired owner chat; nobody to tell")
            return False
        try:
            await send_direct(self.bot_factory, OwnerTarget(token=creds.token, chat_id=chat_id), message)
        except TelegramError as exc:
            log.warning("api watchdog: message not delivered (%s)", exc)
            return False
        await store.mark_alert_delivered(conn, store.now_utc())
        return True

    async def run_once(self, conn: AsyncConnection) -> None:
        if await self.probe():
            self.failures = 0
            if self.alerted and await self.notify(conn, API_BACK_FA):
                self.alerted = False
            return
        self.failures += 1
        log.warning("api watchdog: %s did not answer (%d in a row)", self.url, self.failures)
        if self.failures >= self.failures_to_alert and not self.alerted:
            self.alerted = await self.notify(conn, API_DOWN_FA)
