"""Control-loop base: each loop runs under a Postgres advisory lock, so two workers never act twice.

Each loop also records how its passes go in ``settings['worker.loop.<name>']`` (``ok_at`` of the
last pass that finished, ``failures`` in a row since, ``error_at``, its interval), in its own short
transaction: a pass rolled back never counts as a success. The heartbeat only says the worker
process lives; this says each loop does its work. /internal/metrics exports the age of ``ok_at``
and an alert fires when a loop stops finishing passes (one that raises every time, or hangs).
"""

from __future__ import annotations

import abc
import asyncio
import logging
import math
import time
import zlib
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger(__name__)

LOOP_KEY_PREFIX: Final = "worker.loop."
RECORD_EVERY_S: Final = 60.0  # a success is written at most this often (always after a failure)

_RECORD_OK = (
    "INSERT INTO settings (key, value, updated_at) VALUES (:k, jsonb_build_object('ok_at', now(), "
    "'failures', 0, 'interval_s', CAST(:i AS double precision), 'max_backoff_s', "
    "CAST(:m AS double precision)), now()) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
)
_RECORD_FAILED = (
    "INSERT INTO settings (key, value, updated_at) VALUES (:k, jsonb_build_object('failures', 1, "
    "'error_at', now(), 'interval_s', CAST(:i AS double precision), 'max_backoff_s', "
    "CAST(:m AS double precision)), now()) "
    "ON CONFLICT (key) DO UPDATE SET value = settings.value || jsonb_build_object('failures', "
    "COALESCE((settings.value->>'failures')::int, 0) + 1, 'error_at', now(), "
    "'interval_s', CAST(:i AS double precision), 'max_backoff_s', CAST(:m AS double precision)), "
    "updated_at = now()"
)


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
        recorded_at, failing = -math.inf, False
        while not stop.is_set():
            try:
                ran = await self.try_run_once(engine)
                backoff = self.interval_seconds
            except Exception:
                backoff = min(backoff * 2, self.max_backoff_seconds)
                log.exception("reconciler %s failed; retrying in %.0fs", self.name, backoff)
                await self.record_outcome(engine, ok=False)
                failing = True
            else:
                # Nothing is written when another worker holds the lock (this one did no work).
                if ran and (failing or time.monotonic() - recorded_at >= RECORD_EVERY_S):
                    await self.record_outcome(engine, ok=True)
                    recorded_at, failing = time.monotonic(), False
            await self.pause(stop, backoff)

    async def pause(self, stop: asyncio.Event, seconds: float) -> None:
        """The wait between passes (ends early when the worker stops)."""
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def record_outcome(self, engine: AsyncEngine, *, ok: bool) -> None:
        """Best effort, in its own transaction: a database outage must not stop the loop itself."""
        params = {"k": LOOP_KEY_PREFIX + self.name, "i": self.interval_seconds, "m": self.max_backoff_seconds}
        try:
            async with engine.begin() as conn:
                await conn.execute(text(_RECORD_OK if ok else _RECORD_FAILED), params)
        except Exception as exc:
            log.warning("reconciler %s: outcome not recorded (%s)", self.name, type(exc).__name__)


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
