"""Request and response bodies of the agent API (strict: unknown fields are refused)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from proxyhub.agent_api.validation import VERSION
from proxyhub.pki.issue import MAX_CSR_PEM_CHARS

Version = Annotated[str, StringConstraints(pattern=f"^{VERSION.pattern}$")]
CsrPem = Annotated[str, StringConstraints(min_length=64, max_length=MAX_CSR_PEM_CHARS)]
# The token's exact shape is checked when it is looked up; here only a sane bound.
Token = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RegisterRequest(_Strict):
    token: Token
    csr_pem: CsrPem
    agent_version: Version
    telemt_version: Version | None


class RenewRequest(_Strict):
    csr_pem: CsrPem


class CertificateResponse(_Strict):
    cert_pem: str
    ca_pem: str
