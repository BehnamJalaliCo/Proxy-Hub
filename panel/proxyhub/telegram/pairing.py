"""Owner pairing codes: the owner sends ``/start <code>`` to the bot from a private chat.

* 8 characters from a 32-letter alphabet without look-alikes (40 bits), so it also fits a
  ``https://t.me/<bot>?start=<code>`` deep link (A-Z, 0-9 allowed).
* Valid for 10 minutes and only once; creating a new code cancels the old one.
* Only a SHA-256 hash of the code is stored in the database. The plain code is also kept in
  Redis (memory only, no persistence on the panel) until it expires, only so the dashboard can
  show it again while the owner is pairing (``GET /api/telegram/status``).
* After 20 wrong ``/start`` codes the pending code is cancelled (someone is guessing). Nothing
  about the sender of a wrong code is stored: only the counter.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from typing import Final

ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH: Final = 8
TTL: Final = timedelta(minutes=10)
MAX_WRONG_CODES: Final = 20
_START_RE = re.compile(r"^/start(?:@[A-Za-z0-9_]{3,64})?\s+(\S{1,64})\s*$")


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalize(code: str) -> str | None:
    """Upper-case and check the alphabet; None if it cannot be a pairing code."""
    code = code.strip().upper()
    if len(code) != CODE_LENGTH or any(c not in ALPHABET for c in code):
        return None
    return code


def code_hash(code: str) -> str:
    return hashlib.sha256(f"proxyhub-telegram-pairing|{code}".encode()).hexdigest()


def start_argument(text: str) -> str | None:
    """``/start CODE`` (also ``/start@BotName CODE``) -> ``CODE``; anything else -> None."""
    m = _START_RE.fullmatch(text.strip())
    return m.group(1) if m else None
