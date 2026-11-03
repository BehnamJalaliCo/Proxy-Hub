"""Brute-force protection: failure counters in PostgreSQL, per username and global.

Per username: 5 failures (within 24 h) lock that username for 15 minutes; each further lock doubles
the time, up to 24 hours. The level falls back to zero after a full day without a lock. Unknown
usernames are counted the same way, so a lock never reveals whether an account exists.

Global: 50 failures within 15 minutes, across all usernames, lock every login for 15 minutes,
doubling up to 1 hour. The cap is lower than per username on purpose: anyone on the internet can
trip the global counter, and the owner must never be shut out for a whole day by strangers.

Rows are keyed by an HMAC of the username (master-key subkey), so a password typed into the
username box by mistake is never stored in a readable form.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.auth.secretbox import keyed_digest
from proxyhub.db.models_auth import AuthThrottle

GLOBAL_KEY = "global"
PRUNE_AFTER = timedelta(days=2)


@dataclass(frozen=True)
class LockPolicy:
    scope: str
    threshold: int
    window: timedelta
    base_lock: timedelta
    max_lock: timedelta
    level_reset_after: timedelta

    def lock_duration(self, level: int) -> timedelta:
        factor: int = 2 ** max(level - 1, 0)
        return min(self.base_lock * factor, self.max_lock)


USER_POLICY = LockPolicy(
    scope="user",
    threshold=5,
    window=timedelta(hours=24),
    base_lock=timedelta(minutes=15),
    max_lock=timedelta(hours=24),
    level_reset_after=timedelta(hours=24),
)
GLOBAL_POLICY = LockPolicy(
    scope="global",
    threshold=50,
    window=timedelta(minutes=15),
    base_lock=timedelta(minutes=15),
    max_lock=timedelta(hours=1),
    level_reset_after=timedelta(hours=24),
)


@dataclass(frozen=True)
class LockEvent:
    scope: str
    level: int
    duration: timedelta
    until: datetime


def user_key(username: str) -> str:
    return "u:" + keyed_digest("throttle-username", username)


async def ensure_rows(session: AsyncSession, keys: Sequence[str]) -> None:
    stmt = insert(AuthThrottle).values([{"key": k, "failures": 0, "lock_level": 0} for k in keys])
    await session.execute(stmt.on_conflict_do_nothing(index_elements=["key"]))


async def load(session: AsyncSession, key: str, *, for_update: bool) -> AuthThrottle:
    stmt = select(AuthThrottle).where(AuthThrottle.key == key).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one()


def retry_after_s(row: AuthThrottle | None, now: datetime) -> int:
    """Whole seconds until ``row`` unlocks; 0 when it is not locked."""
    if row is None or row.locked_until is None or row.locked_until <= now:
        return 0
    return max(1, math.ceil((row.locked_until - now).total_seconds()))


def register_failure(row: AuthThrottle, policy: LockPolicy, now: datetime) -> LockEvent | None:
    """Count one failure on ``row`` (already locked FOR UPDATE). Returns the lock it triggered."""
    if row.window_started_at is None or now - row.window_started_at > policy.window:
        row.failures = 0
        row.window_started_at = now
    row.failures += 1
    row.last_failure_at = now
    if row.failures < policy.threshold:
        return None
    previous_end = row.locked_until
    level = row.lock_level
    if previous_end is None or now - previous_end > policy.level_reset_after:
        level = 0
    level += 1
    duration = policy.lock_duration(level)
    row.lock_level = level
    row.locked_until = now + duration
    row.last_lock_at = now
    row.failures = 0
    row.window_started_at = None
    return LockEvent(scope=policy.scope, level=level, duration=duration, until=row.locked_until)


def reset(row: AuthThrottle) -> None:
    """A successful login clears this username's failures and lock history."""
    row.failures = 0
    row.window_started_at = None
    row.lock_level = 0
    row.locked_until = None


async def prune(session: AsyncSession, now: datetime) -> None:
    """Forget idle per-username rows so random usernames cannot grow the table forever.

    Call it in a transaction of its own (holding no other lock). Rows that an attempt in flight
    holds are skipped, never waited for, so pruning can neither block a login nor deadlock.
    """
    cutoff = now - PRUNE_AFTER
    idle = (
        select(AuthThrottle.key)
        .where(
            AuthThrottle.key != GLOBAL_KEY,
            or_(AuthThrottle.last_failure_at.is_(None), AuthThrottle.last_failure_at < cutoff),
            or_(AuthThrottle.locked_until.is_(None), AuthThrottle.locked_until < cutoff),
        )
        .with_for_update(skip_locked=True)
    )
    await session.execute(
        delete(AuthThrottle).where(AuthThrottle.key.in_(idle)).execution_options(synchronize_session=False)
    )
