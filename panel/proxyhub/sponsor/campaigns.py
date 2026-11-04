"""Paid sponsor campaigns: which channel every proxy should show now (pure, no I/O).

docs/11 section 2 row 4: a campaign shows the customer's PUBLIC channel as Proxy Sponsor between its
start and end; outside every campaign the sponsor is the default channel (@ipoxxo, D23). A change
takes about an hour to reach Telegram's servers (روند @MTProxybot step 7), and switching every proxy
takes several @MTProxybot conversations at a human pace, so the switch TO the customer starts
``lead`` before the start; the switch BACK starts at the end. The customer therefore never gets
less than the window that was sold.

States: ``scheduled`` (waiting), ``active`` (its channel is the wanted sponsor), ``finished``,
``cancelled`` (by the owner). ``draft`` is not used by this module.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

SCHEDULED: Final = "scheduled"
ACTIVE: Final = "active"
FINISHED: Final = "finished"
CANCELLED: Final = "cancelled"
OPEN_STATES: Final = frozenset({SCHEDULED, ACTIVE})
MAX_DURATION: Final = timedelta(days=366)
# Switching one proxy is 4 messages to @MTProxybot at most 30 an hour: 9 proxies take about 75
# minutes, so the switch starts 2 hours before a campaign (owner-editable on the sponsor page).
DEFAULT_LEAD: Final = timedelta(minutes=120)


@dataclass(frozen=True)
class Window:
    id: uuid.UUID
    channel: str
    starts_at: datetime
    ends_at: datetime
    state: str

    def switch_at(self, lead: timedelta) -> datetime:
        return self.starts_at - lead

    def effective(self, now: datetime, lead: timedelta) -> bool:
        return self.state in OPEN_STATES and self.switch_at(lead) <= now < self.ends_at


def effective_campaign(windows: Iterable[Window], now: datetime, lead: timedelta) -> Window | None:
    """The campaign whose channel is wanted now. Back-to-back campaigns (one ends when the next
    starts) are allowed: during the next one's lead the running one keeps its channel."""
    live = sorted(
        (w for w in windows if w.effective(now, lead)),
        key=lambda w: (w.starts_at > now, w.starts_at, str(w.id)),
    )
    return live[0] if live else None


def wanted_channel(windows: Iterable[Window], default: str, now: datetime, lead: timedelta) -> str:
    found = effective_campaign(windows, now, lead)
    return found.channel if found is not None else default


def transitions(windows: Iterable[Window], now: datetime, lead: timedelta) -> list[tuple[Window, str]]:
    """State changes due now: scheduled -> active at ``starts_at - lead``; scheduled or active ->
    finished at ``ends_at``."""
    due: list[tuple[Window, str]] = []
    for w in windows:
        if w.state in OPEN_STATES and now >= w.ends_at:
            due.append((w, FINISHED))
        elif w.state == SCHEDULED and now >= w.switch_at(lead):
            due.append((w, ACTIVE))
    return due


class CampaignError(ValueError):
    """``code`` names the problem (Persian text in proxyhub/sponsor/messages.py)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_window(
    starts_at: datetime,
    ends_at: datetime,
    *,
    now: datetime,
    others: Sequence[Window],
    editing: uuid.UUID | None = None,
    already_started: bool = False,
) -> None:
    """A new or edited campaign's times: timezone-aware, end after start, end in the future, at
    most a year, and no overlap with another open campaign: every proxy has one sponsor at a time
    (back to back is fine)."""
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise CampaignError("campaign_time_format")
    if ends_at <= starts_at:
        raise CampaignError("campaign_end_before_start")
    if ends_at <= now:
        raise CampaignError("campaign_in_past")
    if ends_at - starts_at > MAX_DURATION:
        raise CampaignError("campaign_too_long")
    if not already_started and starts_at < now - timedelta(minutes=5):
        raise CampaignError("campaign_start_in_past")
    for other in others:
        if other.id == editing or other.state not in OPEN_STATES:
            continue
        if starts_at < other.ends_at and other.starts_at < ends_at:
            raise CampaignError("campaign_overlap")
