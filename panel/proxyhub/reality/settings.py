"""Reality settings, stored under key ``reality`` in the ``settings`` table (owner-editable).

Defaults (cross-team contract item 5, docs/10 O4): every config lasts 24 hours or 50 GB, whichever
comes first; two fresh configs per serving slot (or slotless node), started half a lifetime apart
so a new one appears about every 12 hours; port 443 (contract item 1, fixed).

The REALITY target (docs/10 X5: foreign, TLS 1.3 + HTTP/2, not a redirect, not behind a third-party
CDN, near the nodes) defaults to ``dl.google.com``: Google's own edge (no third-party CDN), TLS 1.3
with HTTP/2 and X25519, served from Google's front ends in Frankfurt and Hamina right next to
Hetzner's fsn1/nbg1/hel1, and a download host that Iranian networks are expected to leave open (it
serves Android and Chrome updates; not verified from Iran here: the panel-server runbook checks
it). Every node checks the target itself (TLS 1.3 + h2) and reports the result; a node whose check
fails gets no new configs and the owner is told. The target applies to key scopes created after a
change: an existing scope keeps its target so the links already posted stay valid.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.agent_api.validation import valid_domain
from proxyhub.telegram.store import get_setting, put_setting

log = logging.getLogger(__name__)

SETTINGS_KEY = "reality"
STATE_KEY = "reality.state"  # the policy's own bookkeeping (alert de-duplication)
PORT = 443  # contract item 1: Xray VLESS Reality listens on TCP 443 on every node
GB = 1_000_000_000  # caps are decimal gigabytes, like Hetzner's traffic figures
_LINK_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .|-]{0,31}$")

Db = AsyncConnection | AsyncSession


class RealitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    configs_per_node: int = Field(default=2, ge=1, le=20)
    cap_hours: int = Field(default=24, ge=1, le=24 * 30)
    cap_gb: int = Field(default=50, ge=1, le=10_000)
    target_host: str = "dl.google.com"
    target_port: int = Field(default=443, ge=1, le=65535)
    # The name every config gets in the user's app starts with this (decision N15). None: the main
    # channel's username, else "ipoxxo" (decision D23).
    link_name: str | None = None

    @field_validator("target_host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = v.strip().lower()
        if not valid_domain(v):
            raise ValueError("target_host must be a DNS name")
        return v

    @field_validator("link_name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lstrip("@")
        if not _LINK_NAME.fullmatch(v):
            raise ValueError("link_name: 1-32 letters, digits, space, _ . | -")
        return v

    @property
    def target(self) -> str:
        return f"{self.target_host}:{self.target_port}"

    @property
    def cap_bytes(self) -> int:
        return self.cap_gb * GB


async def load_reality_settings(db: Db) -> RealitySettings:
    value = await get_setting(db, SETTINGS_KEY)
    if value is None:
        return RealitySettings()
    try:
        return RealitySettings.model_validate(value)
    except ValidationError:
        log.warning("reality: stored settings are invalid; using the defaults")
        return RealitySettings()


async def save_reality_settings(db: Db, settings: RealitySettings) -> None:
    await put_setting(db, SETTINGS_KEY, settings.model_dump(mode="json"))


async def load_state(db: Db) -> dict[str, Any]:
    return await get_setting(db, STATE_KEY) or {}


async def save_state(db: Db, state: dict[str, Any]) -> None:
    await put_setting(db, STATE_KEY, state)
