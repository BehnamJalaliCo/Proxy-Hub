"""Control-loop base: each loop runs under a Postgres advisory lock, so two workers never act twice."""

from __future__ import annotations

import abc
import asyncio
import logging
import zlib

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger(__name__)


def lock_key(name: str) -> int:
    """Stable 63-bit key for pg advisory locks (namespace 'proxyhub:' avoids collisions)."""
    return zlib.crc32(f"proxyhub:{name}".encode()) & 0x7FFFFFFF


class Reconciler(abc.ABC):
    name: str
    interval_seconds: float = 15.0
    max_backoff_seconds: float = 300.0

    @abc.abstractmethod
    async def run_once(self, conn: AsyncConnection) -> None:
        """Observe, decide, do at most one small step, record it. Must be idempotent."""

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        """Run one iteration if this process gets the lock. Returns True if it ran."""
        async with engine.connect() as lock_conn:
            got = (
                await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key(self.name)})
            ).scalar_one()
            if not got:
                return False
            try:
                async with engine.begin() as conn:
                    await self.run_once(conn)
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(self.name)})
                await lock_conn.commit()
        return True

    async def loop(self, engine: AsyncEngine, stop: asyncio.Event) -> None:
        backoff = self.interval_seconds
        while not stop.is_set():
            try:
                await self.try_run_once(engine)
                backoff = self.interval_seconds
            except Exception:
                log.exception("reconciler %s failed; retrying in %.0fs", self.name, backoff)
                backoff = min(backoff * 2, self.max_backoff_seconds)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass


class Heartbeat(Reconciler):
    """Writes the worker heartbeat so /healthz can tell whether the worker is alive."""

    name = "heartbeat"
    interval_seconds = 15.0

    async def run_once(self, conn: AsyncConnection) -> None:
        await conn.execute(
            text(
                "INSERT INTO settings (key, value, updated_at) "
                "VALUES ('worker.heartbeat', jsonb_build_object('at', now()), now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            )
        )
