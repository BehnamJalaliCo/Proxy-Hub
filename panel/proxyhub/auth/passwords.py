"""Usernames and passwords: format rules, strength policy and argon2id hashing.

argon2id parameters follow RFC 9106 section 4, second recommended option (t=3, m=64 MiB, p=4).
A stored hash made with older parameters is replaced on the next successful login.
Hashing is CPU-bound, so the async callers run it in a worker thread.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import unicodedata
from enum import StrEnum
from functools import lru_cache

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from proxyhub.auth.common_passwords import COMMON_PASSWORDS, CONTEXT_WORDS

USERNAME_RE = re.compile(r"[a-z0-9_.-]{3,32}")
PASSWORD_MIN_CHARS = 12
PASSWORD_MAX_CHARS = 256
MIN_DISTINCT_CHARS = 5
MIN_CHARS_WITHOUT_CONTEXT_WORDS = 8

ARGON2_TIME_COST = 3
ARGON2_MEMORY_KIB = 64 * 1024
ARGON2_PARALLELISM = 4

HASHER = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_KIB,
    parallelism=ARGON2_PARALLELISM,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


class PasswordProblem(StrEnum):
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    COMMON = "common"
    TOO_SIMPLE = "too_simple"
    CONTAINS_USERNAME = "contains_username"
    CONTEXT_WORDS = "context_words"


def normalize_username(raw: str) -> str:
    """Usernames are case-insensitive: phone keyboards capitalise the first letter."""
    return raw.strip().lower()


def is_valid_username(username: str) -> bool:
    return USERNAME_RE.fullmatch(username) is not None


def normalize_password(raw: str) -> str:
    """NFKC, as NIST SP 800-63B recommends, so the same password typed on two devices matches."""
    return unicodedata.normalize("NFKC", raw)


def password_problem(password: str, username: str) -> PasswordProblem | None:
    """Return why ``password`` is refused, or ``None`` when it is acceptable."""
    pw = normalize_password(password)
    if len(pw) < PASSWORD_MIN_CHARS:
        return PasswordProblem.TOO_SHORT
    if len(pw) > PASSWORD_MAX_CHARS:
        return PasswordProblem.TOO_LONG
    folded = pw.casefold()
    if folded in COMMON_PASSWORDS:
        return PasswordProblem.COMMON
    if len(set(folded)) < MIN_DISTINCT_CHARS:
        return PasswordProblem.TOO_SIMPLE
    if len(username) >= 4 and username.casefold() in folded:
        return PasswordProblem.CONTAINS_USERNAME
    rest = folded
    for word in CONTEXT_WORDS:
        rest = rest.replace(word, "")
    if len(rest) < MIN_CHARS_WITHOUT_CONTEXT_WORDS:
        return PasswordProblem.CONTEXT_WORDS
    return None


def hash_password(password: str) -> str:
    return HASHER.hash(normalize_password(password))


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time check (argon2 compares internally). Never raises for a wrong password."""
    try:
        return HASHER.verify(stored_hash, normalize_password(password))
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return HASHER.hash(secrets.token_urlsafe(24))


def burn_verify_time(password: str) -> None:
    """Spend the same time as a real verification, for unknown usernames (no user enumeration)."""
    verify_password(_dummy_hash(), password)


async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(stored_hash: str | None, password: str) -> bool:
    if stored_hash is None:
        await asyncio.to_thread(burn_verify_time, password)
        return False
    return await asyncio.to_thread(verify_password, stored_hash, password)
