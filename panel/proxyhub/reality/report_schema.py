"""The ``xray`` section of an agent report (strict, like the rest of proxyhub/agent_api/reports.py).

Per Reality user only its label and cumulative byte total; for Xray only up/version/uptime, the
target check (with the target and server name it was about: both come from the panel's own desired
state, DNS names only) and an apply error code. Nothing can carry an address (rule 7): every string
has a narrow pattern and the whole report is scanned for anything address-shaped before validation.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from proxyhub.agent_api.validation import ERROR_CODE, valid_domain

MAX_XRAY_USERS = 1100
INT64_MAX = 2**63 - 1
# The agent's own label rule (agent/internal/desired/xray.go); the panel's labels are cfg-<id>@proxyhub.
XRAY_EMAIL = r"^[a-z][a-z0-9-]{0,31}@[a-z][a-z0-9]{0,15}$"
XRAY_VERSION = r"^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$"
# Narrow shapes here; the validator below checks the full DNS-name rule (DOMAIN, never an IP).
CHECKED_TARGET = r"^[a-z0-9.-]{4,253}:[0-9]{1,5}$"
CHECKED_SERVER_NAME = r"^[a-z0-9.-]{4,253}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class XrayUserCounters(_Strict):
    email: Annotated[str, StringConstraints(pattern=XRAY_EMAIL)]
    total_octets: Annotated[int, Field(ge=0, le=INT64_MAX)]


class XrayCounters(_Strict):
    enabled: bool
    up: bool
    version: Annotated[str, StringConstraints(pattern=XRAY_VERSION)] | None
    uptime_s: Annotated[float, Field(ge=0, le=20 * 365 * 86400)] | None
    target_ok: bool | None
    # What the last target check was about (null together with target_ok: not checked yet).
    checked_target: Annotated[str, StringConstraints(pattern=CHECKED_TARGET)] | None
    checked_server_name: Annotated[str, StringConstraints(pattern=CHECKED_SERVER_NAME)] | None
    apply_error: Annotated[str, StringConstraints(pattern=f"^{ERROR_CODE.pattern}$")] | None
    users: Annotated[list[XrayUserCounters], Field(max_length=MAX_XRAY_USERS)]

    @model_validator(mode="after")
    def _unique(self) -> XrayCounters:
        emails = [u.email for u in self.users]
        if len(emails) != len(set(emails)):
            raise ValueError("duplicate xray user")
        checked = (self.target_ok, self.checked_target, self.checked_server_name)
        if any(v is None for v in checked) != all(v is None for v in checked):
            raise ValueError("target_ok, checked_target and checked_server_name come together")
        if self.checked_target is not None:
            host, _, port = self.checked_target.rpartition(":")
            if not valid_domain(host) or not 1 <= int(port) <= 65535:
                raise ValueError("checked_target must be dns-name:port")
        if self.checked_server_name is not None and not valid_domain(self.checked_server_name):
            raise ValueError("checked_server_name must be a DNS name")
        return self

    def checked(self) -> tuple[str, str] | None:
        """(target, server name) the reported target check was about, or None (not checked)."""
        if self.checked_target is None or self.checked_server_name is None:
            return None
        return self.checked_target, self.checked_server_name
