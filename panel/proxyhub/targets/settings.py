"""The owner's candidate sites, stored under key ``targets`` in the ``settings`` table.

Defaults (P15, 2026-11-05: measured from Iran with TLS handshakes on the real panel server):
``www.apple.com``, ``github.com``, ``aws.amazon.com`` and ``www.speedtest.net`` were healthy on MCI and
Rightel (``www.microsoft.com`` was closed on MCI and ``dl.google.com`` slow, so neither is listed);
``www.cloudflare.com``, ``www.mozilla.org``, ``addons.mozilla.org`` and ``www.samsung.com`` are big
TLS 1.3 + HTTP/2 sites added for choice. Every candidate (and each current target) is measured from
MCI and Rightel every ``interval_h`` hours; the owner edits the list in the dashboard's Settings.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.agent_api.validation import valid_domain
from proxyhub.telegram.store import get_setting, put_setting

log = logging.getLogger(__name__)

Db = AsyncConnection | AsyncSession
SETTINGS_KEY = "targets"
STATE_KEY = "targets.state"  # the loop's own bookkeeping (last full round, alert de-duplication)

DEFAULT_CANDIDATES: tuple[str, ...] = (
    "www.apple.com",
    "github.com",
    "aws.amazon.com",
    "www.speedtest.net",
    "www.cloudflare.com",
    "www.mozilla.org",
    "addons.mozilla.org",
    "www.samsung.com",
)
MAX_CANDIDATES = 12


class TargetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    candidates: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CANDIDATES), min_length=1, max_length=MAX_CANDIDATES
    )
    interval_h: int = Field(default=6, ge=1, le=48)

    @field_validator("candidates")
    @classmethod
    def _hosts(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            host = raw.strip().lower().rstrip(".")
            if not valid_domain(host):
                raise ValueError("every candidate must be a DNS name (not an IP address)")
            if host not in out:
                out.append(host)
        return out


async def load_target_settings(db: Db) -> TargetSettings:
    value = await get_setting(db, SETTINGS_KEY)
    if value is None:
        return TargetSettings()
    try:
        return TargetSettings.model_validate(value)
    except ValidationError:
        log.warning("targets: stored settings are invalid; using the defaults")
        return TargetSettings()


async def save_target_settings(db: Db, settings: TargetSettings) -> None:
    await put_setting(db, SETTINGS_KEY, settings.model_dump(mode="json"))


async def load_state(db: Db) -> dict[str, Any]:
    return await get_setting(db, STATE_KEY) or {}


async def save_state(db: Db, state: dict[str, Any]) -> None:
    await put_setting(db, STATE_KEY, state)
