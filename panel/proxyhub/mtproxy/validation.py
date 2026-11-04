"""Input formats of the operator-account login and the masked phone (pure; no I/O, no logging).

Every parser returns the normalised value or raises :class:`InputError` with a contract code. The
error never carries the input: a phone, an api_hash, a code or a password must not come back in a
response or a log line.
"""

from __future__ import annotations

import re
from typing import Final

API_ID_MAX: Final = 2**31 - 1
CODE_MIN_DIGITS: Final = 5
CODE_MAX_DIGITS: Final = 8
PASSWORD_MAX_CHARS: Final = 256
MASK: Final = "•••••"

# Persian (U+06F0..) and Arabic-Indic (U+0660..) digits typed on a phone keyboard.
_DIGITS: Final = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_API_ID_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_API_HASH_RE = re.compile(r"^[0-9a-f]{32}$")
_E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")
_PHONE_SEPARATORS_RE = re.compile(r"[\s\-().‌‎‏‪-‮]")


class InputError(ValueError):
    """``code`` is one of: api_id_format, api_hash_format, phone_format, code_format, password_format."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def ascii_digits(value: str) -> str:
    return value.translate(_DIGITS)


def parse_api_id(value: int | str) -> int:
    """my.telegram.org's ``App api_id``: a positive number (digits only, Persian digits allowed)."""
    if isinstance(value, bool):
        raise InputError("api_id_format")
    text = str(value) if isinstance(value, int) else ascii_digits(value.strip())
    if not _API_ID_RE.fullmatch(text) or int(text) > API_ID_MAX:
        raise InputError("api_id_format")
    return int(text)


def parse_api_hash(value: str) -> str:
    """my.telegram.org's ``App api_hash``: exactly 32 hexadecimal characters (stored lower-case)."""
    text = value.strip().lower()
    if not _API_HASH_RE.fullmatch(text):
        raise InputError("api_hash_format")
    return text


def parse_phone(value: str) -> str:
    """E.164 (``+`` country code and number, 7 to 15 digits). Spaces, dashes, brackets, dots and
    direction marks are dropped; ``00`` in front becomes ``+``; Persian digits become ASCII."""
    text = _PHONE_SEPARATORS_RE.sub("", ascii_digits(value))
    if text.startswith("00"):
        text = "+" + text[2:]
    if not _E164_RE.fullmatch(text):
        raise InputError("phone_format")
    return text


def parse_code(value: str) -> str:
    """The login code Telegram sent: 5 to 8 digits (spaces and dashes dropped)."""
    text = re.sub(r"[\s\-]", "", ascii_digits(value))
    if not text.isascii() or not text.isdigit() or not CODE_MIN_DIGITS <= len(text) <= CODE_MAX_DIGITS:
        raise InputError("code_format")
    return text


def parse_password(value: str) -> str:
    """The two-step password, exactly as typed (no trimming: spaces may be part of it)."""
    if not value or len(value) > PASSWORD_MAX_CHARS:
        raise InputError("password_format")
    return value


def mask_phone(phone: str) -> str:
    """``+447700900123`` -> ``+44•••••23``: the first two and the last two digits, always five dots
    (the length is not shown either). Never call it with anything but a parsed E.164 number."""
    digits = phone.lstrip("+")
    if len(digits) < 7 or not digits.isdigit():
        return "+" + MASK
    return f"+{digits[:2]}{MASK}{digits[-2:]}"
