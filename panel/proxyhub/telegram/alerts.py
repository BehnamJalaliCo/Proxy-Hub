"""Owner alerts sent DIRECTLY to the owner's private chat (not through the outbox).

Used by ``POST /internal/alerts/alertmanager`` (Alertmanager webhook, payload version 4) and by the
worker's API watchdog. Direct on purpose: a stopped worker must not hold back the alert that says
the worker stopped (WorkerHeartbeatStale).

* :class:`AlertmanagerWebhook` validates the real body Alertmanager v0.34.2 sends (a captured one is
  test data in tests/test_telegram_captured.py).
* :func:`render_alertmanager_message` turns it into one Persian HTML message (firing first, then
  resolved), at most 4096 characters.
* :class:`Deduper` drops an alert already delivered with the same fingerprint AND status in the last
  5 minutes (Alertmanager may repeat a notification, e.g. after our own 5xx).
* :class:`TargetCache` remembers the last bot token + owner chat it read from the database, in this
  process's memory only, so an alert can still go out while PostgreSQL is down (PanelDatabaseDown).
* :func:`send_direct` retries briefly: up to 3 attempts, waiting for a 429 ``retry_after`` of at
  most 10 s. An alert may arrive twice if Telegram's answer is lost after it was sent (at least
  once; a duplicate alert is harmless, a lost one is not).
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from proxyhub.telegram.client import BotClient, NetworkError, RetryAfter, ServerError
from proxyhub.telegram.schedule import TEHRAN
from proxyhub.telegram.templates import MAX_MESSAGE_CHARS, fa_digits, utf16_len, visible_text
from proxyhub.vault import SecretValue

DEDUPE_WINDOW: Final = timedelta(minutes=5)
SEND_ATTEMPTS: Final = 3
MAX_RETRY_WAIT_S: Final = 10
MAX_ALERTS: Final = 1000
MAX_SUMMARY_CHARS: Final = 300
MAX_DESCRIPTION_CHARS: Final = 1000
_SEVERITY_ICON: Final = {"critical": "🔴", "warning": "🟠"}


class AmAlert(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime = Field(alias="startsAt")
    ends_at: datetime | None = Field(default=None, alias="endsAt")
    fingerprint: str = Field(min_length=1, max_length=64)


class AlertmanagerWebhook(BaseModel):
    """Alertmanager webhook payload (prometheus.io/docs/alerting/latest/configuration/#webhook_config)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    version: Literal["4"]
    status: Literal["firing", "resolved"]
    receiver: str = Field(max_length=200)
    alerts: list[AmAlert] = Field(max_length=MAX_ALERTS)
    truncated_alerts: int = Field(default=0, ge=0, alias="truncatedAlerts")


def _clock(at: datetime) -> str:
    return fa_digits(at.astimezone(TEHRAN).strftime("%H:%M"))


def _alert_block(alert: AmAlert) -> str:
    name = alert.labels.get("alertname", "")
    summary = (alert.annotations.get("summary") or name or "هشدار")[:MAX_SUMMARY_CHARS]
    if alert.status == "firing":
        icon = _SEVERITY_ICON.get(alert.labels.get("severity", ""), "🔵")
        lines = [f"{icon} <b>{html.escape(summary, quote=False)}</b>"]
        description = alert.annotations.get("description", "")[:MAX_DESCRIPTION_CHARS]
        if description:
            lines.append(html.escape(description, quote=False))
        lines.append(f"⏱ از ساعت {_clock(alert.starts_at)} (تهران)")
    else:
        when = alert.ends_at if alert.ends_at and alert.ends_at.year > 1 else None
        suffix = f" (ساعت {_clock(when)})" if when else ""
        lines = [f"🟢 <b>{html.escape(summary, quote=False)}</b>{suffix}"]
    return "\n".join(lines)


def _fits(message: str) -> bool:
    return len(message) <= MAX_MESSAGE_CHARS and utf16_len(visible_text(message)) <= MAX_MESSAGE_CHARS


def _footer(omitted: int) -> str:
    return f"\n\n… و {fa_digits(omitted)} هشدار دیگر" if omitted else ""


def render_alertmanager_message(alerts: Sequence[AmAlert], *, truncated: int = 0) -> str:
    """One Persian HTML message for these alerts (firing first, then resolved); <= 4096 chars.

    Alerts that do not fit (and the ones Alertmanager itself truncated) are counted in a last line.
    """
    firing = [a for a in alerts if a.status == "firing"]
    resolved = [a for a in alerts if a.status == "resolved"]
    sections = [
        ("🚨 <b>هشدار Proxy-Hub</b>", firing),
        ("✅ <b>برطرف شد (Proxy-Hub)</b>", resolved),
    ]
    total = len(firing) + len(resolved) + truncated
    shown = 0
    parts: list[str] = []
    full = False
    for header, items in sections:
        for index, alert in enumerate(items):
            candidate = [*parts, *([header] if index == 0 else []), _alert_block(alert)]
            if not _fits("\n\n".join(candidate) + _footer(total - shown - 1)):
                full = True
                break
            parts = candidate
            shown += 1
        if full:
            break
    return "\n\n".join(parts) + _footer(total - shown)


class Deduper:
    """Remembers (fingerprint, status) pairs delivered in the last ``window`` (process memory)."""

    def __init__(self, window: timedelta = DEDUPE_WINDOW, max_entries: int = 10_000) -> None:
        self.window = window
        self.max_entries = max_entries
        self._seen: dict[tuple[str, str], datetime] = {}

    def _prune(self, now: datetime) -> None:
        cutoff = now - self.window
        for key in [k for k, at in self._seen.items() if at <= cutoff]:
            del self._seen[key]
        while len(self._seen) > self.max_entries:
            del self._seen[next(iter(self._seen))]

    def fresh(self, alerts: Sequence[AmAlert], now: datetime) -> list[AmAlert]:
        self._prune(now)
        out: list[AmAlert] = []
        keys: set[tuple[str, str]] = set()
        for alert in alerts:
            key = (alert.fingerprint, alert.status)
            if key in self._seen or key in keys:
                continue
            keys.add(key)
            out.append(alert)
        return out

    def mark(self, alerts: Sequence[AmAlert], now: datetime) -> None:
        for alert in alerts:
            self._seen[(alert.fingerprint, alert.status)] = now
        self._prune(now)

    def clear(self) -> None:
        self._seen.clear()


class NotConfigured(Exception):
    """No bot, or no paired owner chat: there is nobody to send to (``code`` says which)."""

    def __init__(self, code: Literal["bot_not_set", "owner_not_paired"]) -> None:
        super().__init__(code)
        self.code: Literal["bot_not_set", "owner_not_paired"] = code


@dataclass(frozen=True)
class OwnerTarget:
    token: SecretValue
    chat_id: int

    def __repr__(self) -> str:
        return f"OwnerTarget(token=***, chat_id={self.chat_id})"


class TargetCache:
    """Last good :class:`OwnerTarget` read from the database, kept in this process only."""

    def __init__(self) -> None:
        self._target: OwnerTarget | None = None

    async def get(self, load: Callable[[], Awaitable[OwnerTarget]]) -> OwnerTarget:
        try:
            target = await load()
        except NotConfigured:
            self._target = None
            raise
        except Exception:
            if self._target is None:
                raise
            return self._target  # database unreachable: the last known bot and chat
        self._target = target
        return target

    def clear(self) -> None:
        self._target = None


BotFactory = Callable[[SecretValue], BotClient]

# One API process (uvicorn, no workers): these live for the life of that process.
ALERT_DEDUPER = Deduper()
ALERT_TARGETS = TargetCache()


async def send_direct(
    make_bot: BotFactory,
    target: OwnerTarget,
    text: str,
    *,
    attempts: int = SEND_ATTEMPTS,
    max_retry_wait_s: int = MAX_RETRY_WAIT_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Send ``text`` to the owner now; returns the message_id. Retries network/5xx errors and short
    429 waits; raises the last error otherwise (Unauthorized, Forbidden, BadRequest at once)."""
    async with make_bot(target.token) as bot:
        for attempt in range(1, attempts + 1):
            try:
                return (await bot.send_message(target.chat_id, text)).message_id
            except RetryAfter as exc:
                if attempt == attempts or exc.seconds > max_retry_wait_s:
                    raise
                await sleep(exc.seconds)
            except (NetworkError, ServerError):
                if attempt == attempts:
                    raise
                await sleep(float(attempt))
    raise AssertionError("unreachable")
