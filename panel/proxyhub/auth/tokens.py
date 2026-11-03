"""Random bearer tokens (setup link, session cookie) and recovery codes.

Only the SHA-256 of a token or code is stored. All of them carry at least 80 bits of entropy from
``secrets``, so a stored hash cannot be reversed by guessing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import secrets

TOKEN_BYTES = 32
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_CHARS = 16  # 16 symbols x 5 bits = 80 bits
# 32 symbols without look-alikes (no 0/O, 1/I).
RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_RECOVERY_RE = re.compile(f"[{RECOVERY_ALPHABET}]{{{RECOVERY_CODE_CHARS}}}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def new_token() -> str:
    """32 random bytes, URL-safe base64 without padding (43 characters)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> bytes | None:
    """SHA-256 of the raw token bytes, or ``None`` if ``token`` is not one of ours."""
    if _TOKEN_RE.fullmatch(token) is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (binascii.Error, ValueError):
        return None
    # Canonical form only: exactly one string maps to each token.
    if len(raw) != TOKEN_BYTES or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != token:
        return None
    return hashlib.sha256(raw).digest()


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes: set[str] = set()
    while len(codes) < count:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_CHARS))
        codes.add("-".join(raw[i : i + 4] for i in range(0, RECOVERY_CODE_CHARS, 4)))
    return sorted(codes)


def normalize_recovery_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    code = re.sub(r"[\s-]", "", raw).upper()
    return code if _RECOVERY_RE.fullmatch(code) else None


def recovery_code_hash(code: str) -> bytes | None:
    normalized = normalize_recovery_code(code)
    if normalized is None:
        return None
    return hashlib.sha256(b"proxyhub-recovery-v1:" + normalized.encode()).digest()
