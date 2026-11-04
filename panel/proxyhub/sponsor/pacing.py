"""Human pace for the operator account (docs/11 section 2.1 lock 4).

Every message to @MTProxybot (a text or an inline-button press) first takes a slot here: at least
:data:`MIN_GAP` after the previous one and at most :data:`HOURLY_CAP` in any hour. When Telegram
asks the account to wait (FLOOD_WAIT), no message at all goes out until that time has passed
(``hold_until``), whichever proxy is next. The state is the ``settings`` row :data:`KEY` (the last
hour's send times, never more than the cap, and the hold), locked ``FOR UPDATE`` while a slot is
taken, so the limits hold across restarts and workers. The kill switch is read in the same
transaction: once it is ON no further message goes out (docs/11 lock 6). Reading the chat is not a
message and is paced by the driver's poll interval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.db.models import Setting
from proxyhub.fleet.actions import iso
from proxyhub.mtproxy.store import kill_switch_on

KEY: Final = "sponsor.pacing"
MIN_GAP: Final = timedelta(seconds=5)
HOURLY_CAP: Final = 30
WINDOW: Final = timedelta(hours=1)
MAX_HOLD: Final = timedelta(days=2)


class PaceStop(Exception):
    """No message may go out now: ``reason`` is ``hourly_cap``, ``hold`` (Telegram asked us to
    wait), ``kill_switch`` or ``pace_timeout``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _time(item: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(item))
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo is not None else None


@dataclass(frozen=True)
class Pace:
    sent: tuple[datetime, ...] = field(default_factory=tuple)
    hold_until: datetime | None = None

    @classmethod
    def from_json(cls, value: Any) -> Pace:
        """A stored row, read defensively: unreadable entries are dropped."""
        if not isinstance(value, dict):
            return cls()
        raw = value.get("sent")
        sent = sorted(t for t in (_time(i) for i in (raw if isinstance(raw, list) else [])) if t is not None)
        hold = _time(value.get("hold_until")) if value.get("hold_until") is not None else None
        return cls(sent=tuple(sent), hold_until=hold)

    def to_json(self) -> dict[str, Any]:
        return {"sent": [iso(t) for t in self.sent], "hold_until": iso(self.hold_until)}

    def recent(self, now: datetime) -> list[datetime]:
        """The messages of the last hour (a message stamped in the future counts too)."""
        return [t for t in self.sent if t > now - WINDOW]

    def held(self, now: datetime) -> bool:
        return self.hold_until is not None and now < self.hold_until


@dataclass(frozen=True)
class Decision:
    """``wait_s`` > 0: try again after that many seconds; ``stop``: no message now (``reason``)."""

    ok: bool
    wait_s: float = 0.0
    stop: str | None = None


def decide(pace: Pace, now: datetime) -> Decision:
    if pace.held(now):
        return Decision(ok=False, stop="hold")
    last_hour = pace.recent(now)
    if len(last_hour) >= HOURLY_CAP:
        return Decision(ok=False, stop="hourly_cap")
    if last_hour:
        wait = (max(last_hour) + MIN_GAP - now).total_seconds()
        if wait > 0:
            return Decision(ok=False, wait_s=wait)
    return Decision(ok=True)


def remaining(pace: Pace, now: datetime) -> int:
    """How many messages may still go out this hour (none while Telegram asked us to wait)."""
    if pace.held(now):
        return 0
    return max(0, HOURLY_CAP - len(pace.recent(now)))


async def load(db: AsyncConnection, *, for_update: bool = False) -> Pace:
    stmt = select(Setting.value).where(Setting.key == KEY)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).first()
    return Pace() if row is None else Pace.from_json(row[0])


async def save(db: AsyncConnection, pace: Pace) -> None:
    stmt = insert(Setting).values(key=KEY, value=pace.to_json())
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value, "updated_at": text("now()")}
        )
    )


async def hold(db: AsyncConnection, until: datetime, now: datetime) -> None:
    """Telegram asked the account to wait: no message to anyone before ``until`` (at most
    :data:`MAX_HOLD` ahead; a longer hold already in place is kept)."""
    await db.execute(
        insert(Setting).values(key=KEY, value=Pace().to_json()).on_conflict_do_nothing(index_elements=["key"])
    )
    pace = await load(db, for_update=True)
    until = min(until, now + MAX_HOLD)
    if pace.hold_until is None or pace.hold_until < until:
        await save(db, Pace(sent=tuple(pace.recent(now)), hold_until=until))


async def take_slot(
    engine: AsyncEngine,
    *,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_wait_s: float = 120.0,
) -> datetime:
    """Wait for, and record, the right to send one message now. Raises :class:`PaceStop`."""
    waited = 0.0
    while True:
        async with engine.begin() as db:
            await db.execute(
                insert(Setting)
                .values(key=KEY, value=Pace().to_json())
                .on_conflict_do_nothing(index_elements=["key"])
            )
            pace = await load(db, for_update=True)
            if await kill_switch_on(db):
                raise PaceStop("kill_switch")
            now = clock()
            decision = decide(pace, now)
            if decision.stop is not None:
                raise PaceStop(decision.stop)
            if decision.ok:
                keep = pace.hold_until if pace.held(now) else None
                await save(db, Pace(sent=(*pace.recent(now), now), hold_until=keep))
                return now
        if waited >= max_wait_s:
            raise PaceStop("pace_timeout")
        pause = min(decision.wait_s, max_wait_s)
        waited += pause
        await sleep(pause)
