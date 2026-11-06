"""The owner's candidate sites, stored under key ``targets`` in the ``settings`` table.

Defaults (P15, 2026-11-05: measured from Iran with TLS handshakes on the real panel server):
``www.apple.com``, ``github.com``, ``aws.amazon.com`` and ``www.speedtest.net`` were healthy on MCI and
Rightel (``www.microsoft.com`` was closed on MCI and ``dl.google.com`` slow, so neither is listed);
``www.cloudflare.com``, ``www.mozilla.org``, ``addons.mozilla.org`` and ``www.samsung.com`` are big
TLS 1.3 + HTTP/2 sites added for choice. Every candidate (and each current target) is measured from
MCI and Rightel every ``interval_h`` hours; the owner edits the list in the dashboard's Settings.

Order (P16, 2026-11-05): ``github.com`` first (its own addresses, not a CDN edge, and the owner's MCI
test with it was fast). ``www.apple.com`` is no longer listed: Xray 26.10.31 itself warns that an SNI
with apple, icloud or microsoft in it raises the block risk (infra/conf/transport_security.go), and
3x-ui's and PasarGuard's checks agree. The CDN-hosted sites (Akamai, Cloudflare, CloudFront,
Fastly) come after: an SNI whose real addresses are far from our node's is easier to spot.

P19 (2026-11-06): a node still served ``sni=www.apple.com``. No name Xray warns about
(proxyhub/targets/risky.py: apple, icloud or microsoft in it, or a .ru, .ir or .cn name) is a
candidate any more: the owner's list refuses one, and a list stored earlier loses it when loaded (the
other entries stay; a list left empty takes the defaults).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.agent_api.validation import valid_domain
from proxyhub.targets.risky import risky_sni
from proxyhub.telegram.store import get_setting, put_setting

log = logging.getLogger(__name__)

Db = AsyncConnection | AsyncSession
SETTINGS_KEY = "targets"
STATE_KEY = "targets.state"  # the loop's own bookkeeping (last full round, alert de-duplication)

DEFAULT_CANDIDATES: tuple[str, ...] = (
    "github.com",
    "www.speedtest.net",
    "aws.amazon.com",
    "www.cloudflare.com",
    "www.mozilla.org",
    "addons.mozilla.org",
    "www.samsung.com",
)
MAX_CANDIDATES = 12
RISKY_ERROR = "risky_sni"  # the validation error type of a name Xray warns about (the API says why)
_dropped_logged: set[str] = set()  # stored names already reported as dropped (once per process)


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
            if risky_sni(host):
                raise PydanticCustomError(RISKY_ERROR, "Xray warns about this name as a REALITY target")
            if host not in out:
                out.append(host)
        return out


def without_risky(value: Any) -> Any:
    """A stored value without the candidates Xray warns about (saved before P16 / P19). The other
    candidates and settings stay; a list left empty takes the defaults. Anything else as it is."""
    if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
        return value
    hosts: list[Any] = value["candidates"]
    dropped = [h for h in hosts if isinstance(h, str) and risky_sni(h)]
    if not dropped:
        return value
    new = {h.strip().lower().rstrip(".") for h in dropped} - _dropped_logged
    if new:
        _dropped_logged.update(new)
        log.warning("targets: stored candidate(s) %s left out: Xray warns about them", ", ".join(sorted(new)))
    kept = [h for h in hosts if h not in dropped]
    return {**value, "candidates": kept or list(DEFAULT_CANDIDATES)}


async def load_target_settings(db: Db) -> TargetSettings:
    value = await get_setting(db, SETTINGS_KEY)
    if value is None:
        return TargetSettings()
    try:
        return TargetSettings.model_validate(without_risky(value))
    except ValidationError:
        log.warning("targets: stored settings are invalid; using the defaults")
        return TargetSettings()


async def save_target_settings(db: Db, settings: TargetSettings) -> None:
    await put_setting(db, SETTINGS_KEY, settings.model_dump(mode="json"))


async def load_state(db: Db) -> dict[str, Any]:
    return await get_setting(db, STATE_KEY) or {}


async def save_state(db: Db, state: dict[str, Any]) -> None:
    await put_setting(db, STATE_KEY, state)
