"""Reality settings, stored under key ``reality`` in the ``settings`` table (owner-editable).

Transport (2026-11-05, see ``transport`` below): VLESS + WebSocket + TLS with the panel's own
certificate for the node's own DNS name by default; VLESS Reality stays available as a switch.

Defaults (owner decision D41, 2026-11-05): every config runs 48 hours or 1 TB, whichever comes
first, and a config that still works is RENEWED in place an hour before its end (same link, 48 more
hours, 1 TB more), so people who already use it never get stuck; two configs per serving slot (or
slotless node); port 443 (contract item 1, fixed). Every config name starts with the channel's
@username (the ad users see in their app).

XHTTP (owner, 2026-11-05: "XHTTP works well in Iran"): with transport ``reality`` and ``xhttp_enabled``
(default on) every node also serves VLESS + XHTTP + REALITY on :data:`XHTTP_PORT`, with its scope's
REALITY key and target and the same configs, so each config has two links: «کانفیگ N (TCP)» and
«کانفیگ N (XHTTP)», one uuid, one time and data cap (D41) for both. ws_tls has no XHTTP inbound.

The REALITY target (docs/10 X5: foreign, TLS 1.3 + HTTP/2, not a redirect, not behind a third-party
CDN, near the nodes) defaults to ``dl.google.com``: Google's own edge (no third-party CDN), TLS 1.3
with HTTP/2 and X25519, served from Google's front ends in Frankfurt and Hamina right next to
Hetzner's fsn1/nbg1/hel1, and a download host that Iranian networks are expected to leave open (it
serves Android and Chrome updates; not verified from Iran here: the panel-server runbook checks
it). Every node checks the target itself (TLS 1.3 + h2) and reports the result together with the
target it checked; a config is only made on a node that confirmed its scope's current target, and
the owner is told about nodes where it fails. After the target changes, every scope moves to it
(proxyhub/reality/policy.py ``retarget_scopes``): at once if the old target fails there or the scope
has no active config, otherwise once its active configs have ended, so links already posted keep
working until their own end.

``target_mode`` (P15, 2026-11-05): ``auto`` (default) lets the panel pick the target among the
owner's candidate sites (proxyhub/targets): the best one that is open from Iran by a real TLS
handshake on MCI and Rightel and that our nodes' own TLS 1.3 + HTTP/2 check passes (the default
``dl.google.com`` was slow from Iran). Every switch is logged and told to the owner. ``manual``: the
owner's ``target_host`` stays as it is.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.agent_api.validation import valid_domain
from proxyhub.telegram.store import get_setting, put_setting

log = logging.getLogger(__name__)

SETTINGS_KEY = "reality"
STATE_KEY = "reality.state"  # the policy's own bookkeeping (alert de-duplication)
PORT = 443  # contract item 1: Xray VLESS Reality listens on TCP 443 on every node
# VLESS + XHTTP + REALITY on every REALITY node (agent 0.4.0 on): fixed like PORT, because it is in
# every XHTTP link already posted. 2083 is a common HTTPS port; the node firewall lets it in.
XHTTP_PORT = 2083
GB = 1_000_000_000  # caps are decimal gigabytes, like Hetzner's traffic figures
_LINK_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .|-]{0,31}$")

Db = AsyncConnection | AsyncSession
Transport = Literal["ws_tls", "reality"]
TargetMode = Literal["auto", "manual"]


class RealitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # The inbound every node serves on 443 and the shape of every new link (2026-11-05):
    # * ``ws_tls`` (default): VLESS + WebSocket + TLS with the panel's own Let's Encrypt certificate
    #   for the node's own DNS name (slot<N>.<domain>, or t<8 hex>.<domain> for a test node), so
    #   the SNI a censor sees belongs to the IP it goes to. Mirrors the owner's own working config
    #   (VLESS + WS + TLS, real certificate, grey-cloud A record).
    # * ``reality``: VLESS + REALITY on TCP with the target below (SNI dl.google.com on a Hetzner IP;
    #   carried 37 MB/s from Germany but no website opened from Iran in the owner's test).
    # Switching ends the active configs of the other transport (as burned): a node serves one.
    transport: Transport = "ws_tls"
    # With ``reality``: also VLESS + XHTTP + REALITY on XHTTP_PORT for the same configs (a second
    # link per config). Off: the XHTTP inbound goes away and its links show as paused in posts.
    xhttp_enabled: bool = True
    configs_per_node: int = Field(default=2, ge=1, le=20)
    cap_hours: int = Field(default=48, ge=1, le=24 * 30)
    cap_gb: int = Field(default=1000, ge=1, le=10_000)
    # D41: a working config gets another cap_hours and cap_gb before it ends (same link).
    renew: bool = True
    # auto: proxyhub/targets picks target_host among the candidates (P15); manual: the owner's.
    target_mode: TargetMode = "auto"
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
