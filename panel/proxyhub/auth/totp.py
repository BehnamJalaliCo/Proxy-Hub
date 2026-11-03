"""TOTP (RFC 6238): 30-second steps, 6 digits, SHA-1 (what every authenticator app supports).

A code is accepted for the current step and one step either side (clock drift), and only when its
step is newer than the last step this account used, so a code can never be replayed, not even
within its own 30 seconds.
"""

from __future__ import annotations

import hmac
import re
from datetime import UTC, datetime

import pyotp

ISSUER = "IPOXXO Panel"
DIGITS = 6
INTERVAL_S = 30
VALID_WINDOW = 1
SECRET_BASE32_CHARS = 32  # 160 bits, the RFC 4226 recommended secret length

_CODE_RE = re.compile(r"[0-9]{6}")


def new_secret() -> str:
    return pyotp.random_base32(length=SECRET_BASE32_CHARS)


def provisioning_uri(secret: str, account_name: str = "admin") -> str:
    """``otpauth://`` URI for the setup QR code."""
    return pyotp.TOTP(secret, digits=DIGITS, interval=INTERVAL_S).provisioning_uri(
        name=account_name, issuer_name=ISSUER
    )


def current_step(now: datetime | None = None) -> int:
    moment = now or datetime.now(UTC)
    return int(moment.timestamp()) // INTERVAL_S


def normalize_code(raw: str | None) -> str | None:
    """Digits only (apps show '123 456'); ``None`` when it cannot be a valid code."""
    if raw is None:
        return None
    code = re.sub(r"[\s-]", "", raw)
    return code if _CODE_RE.fullmatch(code) else None


def match_step(
    secret: str, code: str | None, last_used_step: int | None, now: datetime | None = None
) -> int | None:
    """Return the time step ``code`` belongs to, or ``None`` if it is wrong or already used.

    Every candidate step is compared (constant-time compare, no early exit) so the response time
    does not depend on which step matched.
    """
    normalized = normalize_code(code)
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=INTERVAL_S)
    step_now = current_step(now)
    matched: int | None = None
    candidate = normalized if normalized is not None else "x" * DIGITS
    for step in range(step_now - VALID_WINDOW, step_now + VALID_WINDOW + 1):
        if hmac.compare_digest(totp.generate_otp(step).encode(), candidate.encode()) and matched is None:
            matched = step
    if normalized is None or matched is None:
        return None
    if last_used_step is not None and matched <= last_used_step:
        return None
    return matched
