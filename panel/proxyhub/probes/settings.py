"""Probe settings, stored (optionally) under key ``probes`` in the ``settings`` table.

Defaults follow the brief and docs/10: every 10 minutes per READY node, the four Iranian networks
the owner named, a verdict from at least two rounds. The free Globalping limit is 250 tests per
hour per IP; each measurement costs one test per probe found (today only MCI and Rightel have
probes), and the reconciler stays inside the limit using the rate-limit headers.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

log = logging.getLogger(__name__)

SETTINGS_KEY = "probes"
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# MCI (Hamrah-e Aval), Rightel, Irancell, TCI: docs/10 I4 and section 3.11.
DEFAULT_ASNS = [197207, 57218, 44244, 58224]


class ProbeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_s: int = Field(default=600, ge=120, le=86400)
    min_rounds: int = Field(default=2, ge=2, le=10)
    max_round_age_s: int = Field(default=3600, ge=600, le=7 * 86400)
    globalping_asns: list[int] = Field(
        default_factory=lambda: list(DEFAULT_ASNS), min_length=1, max_length=10
    )
    # A stable public host on Hetzner's network: reachable from Iran when the probes work, and on the
    # same provider as our nodes (measured from MCI and Rightel on 2026-11-03).
    control_host: str = "www.hetzner.com"
    control_port: int = Field(default=443, ge=1, le=65535)
    default_port: int = Field(default=443, ge=1, le=65535)  # when the node has no Telemt config yet
    icmp: bool = True
    dns: bool = True
    checkhost: bool = True
    checkhost_max_nodes: int = Field(default=6, ge=1, le=20)
    # All due nodes in one tick: one control measurement per source serves them all (fewer
    # Globalping tests, and check-host throttles repeated checks of the same control host).
    max_nodes_per_tick: int = Field(default=10, ge=1, le=20)
    retention_days: int = Field(default=30, ge=1, le=365)

    @field_validator("control_host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip().lower()
        if not _HOST_RE.fullmatch(v):
            raise ValueError("control_host must be a DNS name")
        return v

    @field_validator("globalping_asns")
    @classmethod
    def _asns(cls, v: list[int]) -> list[int]:
        if any(a <= 0 or a > 4294967295 for a in v) or len(set(v)) != len(v):
            raise ValueError("ASNs must be distinct positive numbers")
        return v


async def load_probe_settings(db: AsyncConnection | AsyncSession) -> ProbeSettings:
    row = (await db.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": SETTINGS_KEY})).first()
    if row is None:
        return ProbeSettings()
    value: Any = row[0]
    try:
        return ProbeSettings.model_validate(value)
    except ValidationError:
        log.warning("probes: stored settings are invalid; using the defaults")
        return ProbeSettings()
