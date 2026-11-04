"""When the day's posts go out. Pure functions, Tehran time.

The posting hours of one day (``day_start`` to ``day_end``; an end before the start runs past
midnight, and ``00:00`` as end means midnight) minus the owner's no-post windows (D21) are the
*free minutes* of that day. ``posts_per_day`` slots are spread evenly over the free minutes, each
moved by a small jitter (at most a quarter of the gap between slots and at most 12 minutes) so the
channel does not look like a clock (docs/04). The jitter comes from a hash of the day and the slot
number, so the plan of a day is the same on every call and after a restart.

A slot is *due* from its time until :data:`GRACE` later; a slot missed by more than that (the
worker was down) is skipped rather than posted at an odd hour.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

from proxyhub.telegram.schedule import TEHRAN, QuietWindow

GRACE: Final = timedelta(minutes=45)
MAX_JITTER_MIN: Final = 12.0


@dataclass(frozen=True)
class Slot:
    day: date  # the Tehran date the posting hours start on
    index: int
    at: datetime  # UTC


def posting_window(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    """The UTC start and end of ``day``'s posting hours."""
    begin = datetime.combine(day, start, tzinfo=TEHRAN)
    end_day = day if end > start else day + timedelta(days=1)
    finish = datetime.combine(end_day, end, tzinfo=TEHRAN)
    return begin.astimezone(UTC), finish.astimezone(UTC)


def _jitter(day: date, index: int) -> float:
    """A stable number in [-1, 1) for this day and slot."""
    digest = hashlib.sha256(f"proxyhub:posts:{day.isoformat()}:{index}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**63 - 1.0


def free_minutes(begin: datetime, finish: datetime, quiet: Sequence[QuietWindow]) -> list[int]:
    """Minute offsets from ``begin`` that no no-post window covers."""
    total = int((finish - begin).total_seconds() // 60)
    out: list[int] = []
    for m in range(total):
        local = (begin + timedelta(minutes=m)).astimezone(TEHRAN).time()
        if not any(w.covers(local) for w in quiet):
            out.append(m)
    return out


def day_plan(day: date, *, count: int, start: time, end: time, quiet: Sequence[QuietWindow]) -> list[Slot]:
    """``count`` slots of ``day`` at distinct minutes, in time order (fewer only when the day has
    fewer free minutes than ``count``)."""
    if count <= 0 or start == end:
        return []
    begin, finish = posting_window(day, start, end)
    free = free_minutes(begin, finish, quiet)
    if not free:
        return []
    n = min(count, len(free))
    gap = len(free) / n
    reach = min(gap / 4, MAX_JITTER_MIN)
    picked: list[int] = []  # indexes into ``free``, strictly increasing
    for i in range(n):
        pos = int((i + 0.5) * gap + _jitter(day, i) * reach)
        low = picked[-1] + 1 if picked else 0
        high = len(free) - (n - i)  # leave one free minute for each slot still to come
        picked.append(min(max(pos, low), high))
    return [Slot(day=day, index=i, at=begin + timedelta(minutes=free[p])) for i, p in enumerate(picked)]


def _days_around(now: datetime) -> list[date]:
    today = now.astimezone(TEHRAN).date()
    return [today - timedelta(days=1), today, today + timedelta(days=1)]


def due_slot(
    now: datetime, *, count: int, start: time, end: time, quiet: Sequence[QuietWindow]
) -> Slot | None:
    """The latest slot at or before ``now`` (of yesterday's or today's plan) if it is less than
    :data:`GRACE` old, else None."""
    latest: Slot | None = None
    for day in _days_around(now)[:2]:
        for slot in day_plan(day, count=count, start=start, end=end, quiet=quiet):
            if slot.at <= now and (latest is None or slot.at > latest.at):
                latest = slot
    if latest is None or now - latest.at > GRACE:
        return None
    return latest


def upcoming(
    now: datetime, *, count: int, start: time, end: time, quiet: Sequence[QuietWindow], limit: int = 48
) -> list[Slot]:
    """Slots after ``now`` (today's and tomorrow's), in time order."""
    out = [
        slot
        for day in _days_around(now)
        for slot in day_plan(day, count=count, start=start, end=end, quiet=quiet)
        if slot.at > now
    ]
    return sorted(out, key=lambda s: s.at)[:limit]
