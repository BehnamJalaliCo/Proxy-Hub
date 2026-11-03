"""When a Telegram job may go out: no-post windows (decision D21) and per-chat pacing. Pure functions.

* **Quiet windows** are owner settings in Asia/Tehran local time (``HH:MM``), and a window may cross
  midnight (``23:00``-``01:00``). Inside a window no NEW post goes to the main channel; alerts and
  edits of old posts are never held back (docs/10 section 3.12: an edit creates no new post and does
  not push the paid ad down).
* **post_interval_min** is the least number of minutes between two new posts in the main channel.
* **Pacing**: at least 3 s between two messages to one channel and 1 s to the owner's chat (Telegram
  allows about 20 messages a minute per channel; docs/04), and never before a 429 ``retry_after``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

TEHRAN: Final = ZoneInfo("Asia/Tehran")
CHANNEL_GAP: Final = timedelta(seconds=3)
OWNER_GAP: Final = timedelta(seconds=1)
MAX_WINDOWS: Final = 24
MAX_POST_INTERVAL_MIN: Final = 24 * 60
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_DAY_MINUTES: Final = 24 * 60


class InvalidSchedule(ValueError):
    """A window or interval the owner submitted is not valid."""


@dataclass(frozen=True)
class QuietWindow:
    start: time
    end: time

    @classmethod
    def parse(cls, start: str, end: str) -> QuietWindow:
        window = cls(parse_hhmm(start), parse_hhmm(end))
        if window.start == window.end:
            raise InvalidSchedule("a window must not start and end at the same minute")
        return window

    @property
    def crosses_midnight(self) -> bool:
        return self.end < self.start

    def covers(self, t: time) -> bool:
        if self.crosses_midnight:
            return t >= self.start or t < self.end
        return self.start <= t < self.end

    def as_json(self) -> dict[str, str]:
        return {"start": self.start.strftime("%H:%M"), "end": self.end.strftime("%H:%M")}


def parse_hhmm(value: str) -> time:
    m = _HHMM.fullmatch(value)
    if not m:
        raise InvalidSchedule("time must be HH:MM (00:00 to 23:59)")
    return time(int(m.group(1)), int(m.group(2)))


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def covers_whole_day(windows: Sequence[QuietWindow]) -> bool:
    """True if the windows together leave no minute of the day for a post."""
    covered = [False] * _DAY_MINUTES
    for w in windows:
        m, end = _minutes(w.start), _minutes(w.end)
        while m != end:
            covered[m] = True
            m = (m + 1) % _DAY_MINUTES
    return all(covered)


def validate_windows(windows: Sequence[QuietWindow]) -> list[QuietWindow]:
    if len(windows) > MAX_WINDOWS:
        raise InvalidSchedule(f"at most {MAX_WINDOWS} windows")
    if covers_whole_day(windows):
        raise InvalidSchedule("the windows cover the whole day; no post could ever be sent")
    return list(windows)


def validate_interval(minutes: int) -> int:
    if not 0 <= minutes <= MAX_POST_INTERVAL_MIN:
        raise InvalidSchedule(f"post_interval_min must be 0 to {MAX_POST_INTERVAL_MIN}")
    return minutes


def _window_end_after(local: datetime, window: QuietWindow) -> datetime:
    """The local end of ``window``, which covers ``local``."""
    end = datetime.combine(local.date(), window.end, tzinfo=TEHRAN)
    if end <= local:  # crosses midnight and we are before midnight
        end += timedelta(days=1)
    return end


def quiet_until(now: datetime, windows: Sequence[QuietWindow]) -> datetime | None:
    """If ``now`` is inside a window: the first UTC instant no window covers. Else None.

    Back-to-back or overlapping windows are followed to their real end.
    """
    if now.tzinfo is None:
        raise ValueError("naive datetime")
    local = now.astimezone(TEHRAN)
    moved = False
    for _ in range(len(windows) + 1):
        hit = next((w for w in windows if w.covers(local.time())), None)
        if hit is None:
            return local.astimezone(UTC) if moved else None
        local = _window_end_after(local, hit)
        moved = True
    # Every minute is covered (validate_windows forbids this): try again in a day.
    return now + timedelta(days=1)


def next_post_time(
    now: datetime,
    *,
    windows: Sequence[QuietWindow],
    interval_min: int,
    last_post_at: datetime | None,
) -> datetime | None:
    """When a new main-channel post may go out, or None if it may go now."""
    earliest = now
    if last_post_at is not None and interval_min > 0:
        earliest = max(earliest, last_post_at + timedelta(minutes=interval_min))
    later = quiet_until(earliest, windows)
    if later is not None:
        earliest = later
    return earliest if earliest > now else None


def next_send_time(
    now: datetime, *, last_sent_at: datetime | None, retry_until: datetime | None, gap: timedelta
) -> datetime | None:
    """Pacing for one chat: when the next message may go, or None if now."""
    earliest = now
    if last_sent_at is not None:
        earliest = max(earliest, last_sent_at + gap)
    if retry_until is not None:
        earliest = max(earliest, retry_until)
    return earliest if earliest > now else None


def backoff(attempts: int, *, base_s: float = 10.0, cap_s: float = 1800.0) -> timedelta:
    """Exponential backoff after a failed attempt: 10 s, 20 s, 40 s, ... at most 30 minutes."""
    return timedelta(seconds=min(cap_s, base_s * 2 ** max(0, attempts - 1)))
