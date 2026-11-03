"""FleetSettings (proxyhub/settings_schema.py) stored under key ``fleet`` in the ``settings`` table.

No row means the schema defaults: kill switch ON and no budget/node cap, so nothing can ever be
created until the owner sets both caps (decision O3, CLAUDE.md rule 8).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from pydantic import ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.db.models import Setting
from proxyhub.settings_schema import FleetSettings

FLEET_KEY = "fleet"
MAX_BUDGET_EUR = Decimal("1000000")
_SERVER_TYPE_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_LOCATION_RE = re.compile(r"^[a-z]{3}\d?$")  # fsn1, nbg1, hel1, ash, hil, sin

Db = AsyncConnection | AsyncSession


class SettingsCorrupt(RuntimeError):
    """The stored value no longer validates; callers must treat it as 'create nothing'."""


def _unique_list(values: list[str], pattern: re.Pattern[str], what: str) -> list[str]:
    out: list[str] = []
    for v in values:
        v = v.strip().lower()
        if not pattern.fullmatch(v):
            raise ValueError(f"invalid {what}: {v!r}")
        if v not in out:
            out.append(v)
    if not out:
        raise ValueError(f"at least one {what} is required")
    return out


class FleetSettingsInput(FleetSettings):
    """What the owner may submit: FleetSettings plus stricter checks on money and names."""

    @field_validator("budget_cap_eur")
    @classmethod
    def _money(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        exponent = v.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise ValueError("at most 2 decimal places")
        if v > MAX_BUDGET_EUR:
            raise ValueError(f"must be at most {MAX_BUDGET_EUR}")
        return v.quantize(Decimal("0.01"))

    @field_validator("allowed_server_types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        return _unique_list(v, _SERVER_TYPE_RE, "server type")

    @field_validator("locations")
    @classmethod
    def _locations(cls, v: list[str]) -> list[str]:
        return _unique_list(v, _LOCATION_RE, "location")

    def to_settings(self) -> FleetSettings:
        return FleetSettings.model_validate(self.model_dump())


def settings_json(s: FleetSettings) -> dict[str, Any]:
    """JSON for API and storage; decimals as strings."""
    data = s.model_dump(mode="json")
    if s.budget_cap_eur is not None:
        data["budget_cap_eur"] = str(s.budget_cap_eur.quantize(Decimal("0.01")))
    return data


def _parse(value: Any) -> FleetSettings:
    try:
        return FleetSettings.model_validate(value)
    except ValidationError as exc:
        raise SettingsCorrupt(f"stored fleet settings are invalid ({exc.error_count()} errors)") from None


async def load_fleet_settings(db: Db) -> FleetSettings:
    row = (await db.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": FLEET_KEY})).first()
    return FleetSettings() if row is None else _parse(row[0])


async def load_fleet_settings_for_update(db: Db) -> FleetSettings:
    """Lock the settings row (creating it with defaults if missing) for a read-modify-write."""
    await db.execute(
        insert(Setting)
        .values(key=FLEET_KEY, value=settings_json(FleetSettings()))
        .on_conflict_do_nothing(index_elements=[Setting.key])
    )
    row = (
        await db.execute(text("SELECT value FROM settings WHERE key = :k FOR UPDATE"), {"k": FLEET_KEY})
    ).one()
    return _parse(row[0])


async def save_fleet_settings(db: Db, s: FleetSettings) -> None:
    stmt = insert(Setting).values(key=FLEET_KEY, value=settings_json(s))
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value, "updated_at": text("now()")}
        )
    )


def settings_diff(old: FleetSettings, new: FleetSettings) -> dict[str, list[Any]]:
    a, b = settings_json(old), settings_json(new)
    return {k: [a[k], b[k]] for k in b if a.get(k) != b[k]}
