"""The ``xray`` section of an agent report (strict, like the rest of proxyhub/agent_api/reports.py).

Per Reality user only its label and cumulative byte total; for Xray only up/version/uptime, the
target check and an apply error code. Nothing can carry an address (rule 7): every string has a
narrow pattern and the whole report is scanned for anything address-shaped before validation.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from proxyhub.agent_api.validation import ERROR_CODE

MAX_XRAY_USERS = 1100
INT64_MAX = 2**63 - 1
# The agent's own label rule (agent/internal/desired/xray.go); the panel's labels are cfg-<id>@proxyhub.
XRAY_EMAIL = r"^[a-z][a-z0-9-]{0,31}@[a-z][a-z0-9]{0,15}$"
XRAY_VERSION = r"^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$"


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
    apply_error: Annotated[str, StringConstraints(pattern=f"^{ERROR_CODE.pattern}$")] | None
    users: Annotated[list[XrayUserCounters], Field(max_length=MAX_XRAY_USERS)]

    @model_validator(mode="after")
    def _unique(self) -> XrayCounters:
        emails = [u.email for u in self.users]
        if len(emails) != len(set(emails)):
            raise ValueError("duplicate xray user")
        return self
