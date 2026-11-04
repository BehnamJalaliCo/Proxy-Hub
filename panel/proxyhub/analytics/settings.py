"""Owner thresholds of the analytics part (``settings`` row ``analytics``).

Defaults: a traffic warning at 80% of a server's included traffic and a CPU warning at 80% average
over one hour. A stored value that no longer validates falls back to these defaults (still alerting).
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.telegram.store import get_setting, put_setting

KEY: Final = "analytics"

Db = AsyncConnection | AsyncSession


class AnalyticsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Percent of a server's included traffic (this billing period) that triggers a warning.
    traffic_warn_pct: StrictInt = Field(default=80, ge=1, le=100)
    # Average CPU of the whole server, percent, over ``cpu_window_min`` minutes.
    cpu_warn_pct: StrictInt = Field(default=80, ge=1, le=100)
    cpu_window_min: StrictInt = Field(default=60, ge=15, le=360)
    # Owner alerts in Telegram (at most one per node, limit and Tehran day). Warnings on the page stay.
    alerts_enabled: StrictBool = True


DEFAULTS: Final = AnalyticsSettings()


async def load_settings(db: Db) -> AnalyticsSettings:
    value = await get_setting(db, KEY)
    if value is None:
        return DEFAULTS
    try:
        return AnalyticsSettings.model_validate(value)
    except ValidationError:
        return DEFAULTS


def parse_settings(body: Any) -> AnalyticsSettings:
    """A FULL settings object from the dashboard (every field present); raises ``ValueError``
    (pydantic's ``ValidationError`` is one)."""
    if not isinstance(body, dict) or set(body) != set(AnalyticsSettings.model_fields):
        raise ValueError("expected exactly the analytics settings fields")
    return AnalyticsSettings.model_validate(body)


async def save_settings(db: Db, s: AnalyticsSettings) -> None:
    await put_setting(db, KEY, s.model_dump())
