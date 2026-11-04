"""Auth building blocks with real crypto (AES-GCM, argon2id, HMAC, RFC 6238), no database."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyotp
import pytest
from argon2 import PasswordHasher

from proxyhub.auth import passwords, secretbox, throttle, tokens, totp
from proxyhub.auth.service import shorten_user_agent
from proxyhub.auth.setup_link import build_link, normalize_base_url
from proxyhub.config import SecretFileError
from proxyhub.db.models_auth import AuthThrottle

# --- secretbox -----------------------------------------------------------------------------


def _key_file(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "master_key"
    path.write_text(content)
    path.chmod(0o600)
    return path


def test_master_key_file_is_validated_without_leaking_content(tmp_path: Path) -> None:
    good = secrets.token_hex(32)
    assert secretbox.load_master_key(_key_file(tmp_path, good + "\n")) == bytes.fromhex(good)
    bad = "zz" + secrets.token_hex(31)
    with pytest.raises(SecretFileError) as err:
        secretbox.load_master_key(_key_file(tmp_path, bad))
    assert bad not in str(err.value)
    with pytest.raises(SecretFileError):
        secretbox.load_master_key(_key_file(tmp_path, secrets.token_hex(16)))
    with pytest.raises(SecretFileError):
        secretbox.load_master_key(tmp_path / "missing")


def test_seal_round_trip_binds_context_and_key() -> None:
    key = secretbox.derive_key("secretbox", os.urandom(32))
    blob = secretbox.seal(b"JBSWY3DPEHPK3PXP", "admin-totp:1", key=key)
    assert b"JBSWY3DPEHPK3PXP" not in blob
    assert secretbox.open_sealed(blob, "admin-totp:1", key=key) == b"JBSWY3DPEHPK3PXP"
    # Same plaintext twice gives different ciphertexts (random nonce).
    assert secretbox.seal(b"JBSWY3DPEHPK3PXP", "admin-totp:1", key=key) != blob
    with pytest.raises(secretbox.DecryptError):
        secretbox.open_sealed(blob, "admin-totp:2", key=key)  # copied to another row
    with pytest.raises(secretbox.DecryptError):
        secretbox.open_sealed(blob, "admin-totp:1", key=secretbox.derive_key("secretbox", os.urandom(32)))
    tampered = bytearray(blob)
    tampered[-1] ^= 1
    with pytest.raises(secretbox.DecryptError):
        secretbox.open_sealed(bytes(tampered), "admin-totp:1", key=key)
    with pytest.raises(secretbox.DecryptError):
        secretbox.open_sealed(b"\x02" + blob[1:], "admin-totp:1", key=key)


def test_subkeys_are_separated_by_purpose() -> None:
    master = os.urandom(32)
    assert secretbox.derive_key("secretbox", master) != secretbox.derive_key("throttle-username", master)
    assert secretbox.derive_key("secretbox", master) != master
    a = secretbox.keyed_digest("throttle-username", "owner", key=secretbox.derive_key("t", master))
    b = secretbox.keyed_digest("throttle-username", "owner", key=secretbox.derive_key("t", os.urandom(32)))
    assert a != b and len(a) == 64


# --- passwords -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "ok"),
    [
        ("owner", True),
        ("a.b-c_9", True),
        ("abc", True),
        ("x" * 32, True),
        ("ab", False),
        ("x" * 33, False),
        ("Owner", False),
        ("own er", False),
        ("ownér", False),
        ("own@r", False),
    ],
)
def test_username_format(username: str, ok: bool) -> None:
    assert passwords.is_valid_username(username) is ok


def test_username_is_normalized_for_phone_keyboards() -> None:
    assert passwords.normalize_username("  Owner ") == "owner"


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short-pw-1", passwords.PasswordProblem.TOO_SHORT),
        ("x" * 257, passwords.PasswordProblem.TOO_LONG),
        ("Password1234", passwords.PasswordProblem.COMMON),
        ("QWERTYUIOPASDF", passwords.PasswordProblem.COMMON),
        ("abababababababab", passwords.PasswordProblem.TOO_SIMPLE),
        ("my-owner-account-9", passwords.PasswordProblem.CONTAINS_USERNAME),
        ("ipoxxo-panel-77", passwords.PasswordProblem.CONTEXT_WORDS),
        ("Kite!river-7-Moss_lamp", None),
        ("رمز طولانی و خوب برای پنل", None),
    ],
)
def test_password_policy(password: str, problem: passwords.PasswordProblem | None) -> None:
    assert passwords.password_problem(password, "owner") == problem


def test_argon2id_parameters_and_verify() -> None:
    stored = passwords.hash_password("Kite!river-7-Moss_lamp")
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert passwords.verify_password(stored, "Kite!river-7-Moss_lamp")
    assert not passwords.verify_password(stored, "Kite!river-7-Moss_lamP")
    assert not passwords.verify_password("not-a-hash", "whatever")
    assert not passwords.needs_rehash(stored)
    weaker = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("Kite!river-7-Moss_lamp")
    assert passwords.needs_rehash(weaker)
    assert passwords.verify_password(weaker, "Kite!river-7-Moss_lamp")


def test_password_unicode_normalization() -> None:
    # Same password, composed vs decomposed form (different devices / keyboards).
    composed = "café-garden-river"
    decomposed = "café-garden-river"
    assert composed != decomposed
    assert passwords.verify_password(passwords.hash_password(composed), decomposed)


async def test_unknown_user_costs_a_real_verification() -> None:
    assert await passwords.verify_password_async(None, "anything-at-all") is False


# --- TOTP ----------------------------------------------------------------------------------


def test_totp_window_and_replay() -> None:
    secret = totp.new_secret()
    assert len(secret) == 32
    ref = pyotp.TOTP(secret)
    now = datetime(2026, 11, 3, 12, 0, 10, tzinfo=UTC)
    step = totp.current_step(now)
    assert totp.match_step(secret, ref.at(now), None, now) == step
    assert totp.match_step(secret, ref.at(now - timedelta(seconds=30)), None, now) == step - 1
    assert totp.match_step(secret, ref.at(now + timedelta(seconds=30)), None, now) == step + 1
    assert totp.match_step(secret, ref.at(now - timedelta(seconds=60)), None, now) is None
    assert totp.match_step(secret, ref.at(now + timedelta(seconds=60)), None, now) is None
    # Replay: a step equal to or older than the last used one is refused.
    assert totp.match_step(secret, ref.at(now), step, now) is None
    assert totp.match_step(secret, ref.at(now - timedelta(seconds=30)), step, now) is None
    assert totp.match_step(secret, ref.at(now + timedelta(seconds=30)), step, now) == step + 1
    # Formatting from apps ("123 456") is accepted; junk is not.
    code = ref.at(now)
    assert totp.match_step(secret, f"{code[:3]} {code[3:]}", None, now) == step
    for junk in ("", "12345", "1234567", "abcdef", None):
        assert totp.match_step(secret, junk, None, now) is None


def test_provisioning_uri_has_issuer() -> None:
    secret = totp.new_secret()
    uri = totp.provisioning_uri(secret)
    parsed = pyotp.parse_uri(uri)
    assert uri.startswith("otpauth://totp/IPOXXO%20Panel:admin?")
    assert isinstance(parsed, pyotp.TOTP)
    assert parsed.issuer == "IPOXXO Panel" and parsed.digits == 6 and parsed.interval == 30
    assert parsed.secret == secret


# --- tokens and recovery codes ------------------------------------------------------------


def test_token_hash_is_sha256_of_raw_bytes_and_canonical() -> None:
    token = tokens.new_token()
    assert len(token) == 43
    raw = base64.urlsafe_b64decode(token + "=")
    assert tokens.token_hash(token) == hashlib.sha256(raw).digest()
    assert tokens.token_hash(token[:-1]) is None
    assert tokens.token_hash(token + "A") is None
    assert tokens.token_hash("!" * 43) is None
    # The last character carries 2 unused bits: only the canonical spelling is accepted.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    idx = alphabet.index(token[-1])
    sibling = token[:-1] + alphabet[idx ^ 1]
    assert tokens.token_hash(sibling) is None


def test_recovery_codes() -> None:
    codes = tokens.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    for code in codes:
        assert len(code) == 19 and code.count("-") == 3
        assert tokens.recovery_code_hash(code) == tokens.recovery_code_hash(code.lower().replace("-", " "))
    assert tokens.recovery_code_hash("not a code") is None
    assert tokens.recovery_code_hash(codes[0]) != tokens.recovery_code_hash(codes[1])


# --- throttle policy -----------------------------------------------------------------------


def _row() -> AuthThrottle:
    return AuthThrottle(key="u:test", failures=0, lock_level=0)


def test_user_lock_after_five_failures_doubling_to_24h() -> None:
    row = _row()
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    expected = [15, 30, 60, 120, 240, 480, 960, 1440, 1440]
    for minutes in expected:
        for i in range(throttle.USER_POLICY.threshold):
            event = throttle.register_failure(row, throttle.USER_POLICY, now)
            assert (event is None) is (i < throttle.USER_POLICY.threshold - 1)
        assert event is not None and event.duration == timedelta(minutes=minutes)
        assert throttle.retry_after_s(row, now) == minutes * 60
        now = event.until + timedelta(seconds=1)  # the next burst comes right after the lock ends
        assert throttle.retry_after_s(row, now) == 0


def test_lock_level_decays_after_a_quiet_day_and_reset_clears() -> None:
    row = _row()
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    for _ in range(5):
        event = throttle.register_failure(row, throttle.USER_POLICY, now)
    assert event is not None and event.level == 1
    now = event.until + timedelta(hours=25)
    for _ in range(5):
        event = throttle.register_failure(row, throttle.USER_POLICY, now)
    assert event is not None and event.level == 1 and event.duration == timedelta(minutes=15)
    throttle.reset(row)
    assert row.failures == 0 and row.lock_level == 0 and row.locked_until is None


def test_failures_outside_the_window_are_forgotten() -> None:
    row = _row()
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    for _ in range(4):
        assert throttle.register_failure(row, throttle.USER_POLICY, now) is None
    now += timedelta(hours=24, seconds=1)
    assert throttle.register_failure(row, throttle.USER_POLICY, now) is None
    assert row.failures == 1


def test_global_lock_is_capped_at_one_hour() -> None:
    row = AuthThrottle(key=throttle.GLOBAL_KEY, failures=0, lock_level=0)
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    durations = []
    for _ in range(4):
        event = None
        for _ in range(throttle.GLOBAL_POLICY.threshold):
            event = throttle.register_failure(row, throttle.GLOBAL_POLICY, now)
        assert event is not None
        durations.append(event.duration)
        now = event.until + timedelta(seconds=1)
    assert durations == [timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1), timedelta(hours=1)]


# --- small helpers -------------------------------------------------------------------------


def test_user_agent_is_shortened_and_cleaned() -> None:
    assert shorten_user_agent(None) is None
    assert shorten_user_agent("  ") is None
    ua = "Mozilla/5.0 (iPhone)\x00\n  Safari/605.1" + "x" * 500
    short = shorten_user_agent(ua)
    assert short is not None and len(short) == 160 and "\x00" not in short and "\n" not in short
    assert short.startswith("Mozilla/5.0 (iPhone) Safari/605.1")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://panel.beyra-ai.com", "https://panel.beyra-ai.com"),
        ("https://panel.beyra-ai.com/", "https://panel.beyra-ai.com"),
        ("https://panel.beyra-ai.com:8443/x/", "https://panel.beyra-ai.com:8443/x"),
    ],
)
def test_setup_base_url(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected
    assert build_link(raw, "TOKEN") == f"{expected}/setup#TOKEN"


@pytest.mark.parametrize(
    "raw",
    ["http://panel.beyra-ai.com", "panel.beyra-ai.com", "https://", "https://a.b/?q=1", "https://u:p@a.b"],
)
def test_setup_base_url_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_base_url(raw)
