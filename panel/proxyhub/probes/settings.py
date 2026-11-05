"""Probe settings, stored (optionally) under key ``probes`` in the ``settings`` table.

What decides is a TLS handshake from Iran (proxyhub/probes/verdict.py): Globalping ``http``
measurements from MCI (AS197207, up to 3 probes), Rightel (AS57218, up to 2) and up to 3 more probes
anywhere in Iran, to the node's own name on 443 and on the MTProto port, paired with the same
probes' handshake to a TLS control host. A node is ``blocked`` when the handshake failed from MCI
AND Rightel (the ``verdict_asns``) in 2 rounds while the control worked from both.

Budget: the free Globalping limit is 250 tests per hour per IP, and every probe that runs a
measurement is one test. A round costs (1 + 2 x nodes) x probes (about 5 probes in Iran today), so
nodes are measured every 30 minutes and a cycle only takes as many nodes as the remaining allowance
covers (proxyhub/probes/service.py ``tls_plan``); the TCP/ICMP/DNS information runs only on what is
left. Rounds may be up to 90 minutes old for a verdict (two rounds plus one delayed by the budget).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

log = logging.getLogger(__name__)

SETTINGS_KEY = "probes"
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# MCI (Hamrah-e Aval), Rightel, Irancell, TCI: docs/10 I4 and section 3.11.
DEFAULT_ASNS = [197207, 57218, 44244, 58224]
MCI_ASN = 197207
RIGHTEL_ASN = 57218
# Where the TLS handshakes run (P15, 2026-11-05: Globalping has probes on MCI and Rightel, none on
# Irancell or TCI; the country entry adds other Iranian networks as information).
DEFAULT_TLS_LOCATIONS: list[dict[str, Any]] = [
    {"asn": MCI_ASN, "limit": 3},
    {"asn": RIGHTEL_ASN, "limit": 2},
    {"country": "IR", "limit": 3},
]
# Both must fail (every port, 2 rounds) for "blocked".
DEFAULT_VERDICT_ASNS = [MCI_ASN, RIGHTEL_ASN]


class ProbeLocation(BaseModel):
    """One Globalping location object: an ASN or a country, with its own probe limit."""

    model_config = ConfigDict(extra="forbid")

    asn: int | None = Field(default=None, ge=1, le=4294967295)
    country: str | None = None
    limit: int = Field(default=1, ge=1, le=10)

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", v):
            raise ValueError("country must be a two-letter code")
        return v

    @model_validator(mode="after")
    def _one_key(self) -> ProbeLocation:
        if (self.asn is None) == (self.country is None):
            raise ValueError("a location names exactly one of asn or country")
        return self

    def body(self) -> dict[str, Any]:
        key: dict[str, Any] = {"asn": self.asn} if self.asn is not None else {"country": self.country}
        return {**key, "limit": self.limit}


class ProbeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    interval_s: int = Field(default=1800, ge=120, le=86400)
    min_rounds: int = Field(default=2, ge=2, le=10)
    max_round_age_s: int = Field(default=5400, ge=600, le=7 * 86400)
    tls_locations: list[ProbeLocation] = Field(
        default_factory=lambda: [ProbeLocation.model_validate(x) for x in DEFAULT_TLS_LOCATIONS],
        min_length=1,
        max_length=6,
    )
    verdict_asns: list[int] = Field(
        default_factory=lambda: list(DEFAULT_VERDICT_ASNS), min_length=1, max_length=4
    )
    # A big site that is reachable from MCI and Rightel (measured with a TLS handshake on 2026-11-05)
    # and not on Hetzner (a Hetzner-wide block would hide the probes' own health).
    tls_control_host: str = "github.com"
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

    @field_validator("control_host", "tls_control_host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip().lower()
        if not _HOST_RE.fullmatch(v):
            raise ValueError("control hosts must be DNS names")
        return v

    @field_validator("verdict_asns")
    @classmethod
    def _verdict_asns(cls, v: list[int]) -> list[int]:
        if any(a <= 0 or a > 4294967295 for a in v) or len(set(v)) != len(v):
            raise ValueError("ASNs must be distinct positive numbers")
        return v

    @property
    def tls_location_bodies(self) -> list[dict[str, Any]]:
        return [loc.body() for loc in self.tls_locations]

    @property
    def tls_probe_limit(self) -> int:
        """The most probes one TLS measurement can use (each one is a Globalping test)."""
        return sum(loc.limit for loc in self.tls_locations)

    @property
    def key_networks(self) -> list[str]:
        return [f"asn:{a}" for a in self.verdict_asns]

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
