"""Small, self-contained AES-256-GCM helper for the auth package (TOTP secrets at rest).

The master key is a file (``config.master_key_file``, 64 hex characters, created by
deploy/ensure_secrets.sh). Every purpose gets its own subkey via HKDF-SHA256, so the auth package
never uses the raw master key and never shares a key with another subsystem. Each ciphertext is
bound to its row with associated data (for example ``admin-totp:<user id>``), so an encrypted
value copied to another row does not decrypt.

Blob format: ``0x01 | nonce (12 bytes) | ciphertext + tag``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from proxyhub.config import SecretFileError, get_config, read_secret_file

VERSION = 1
NONCE_BYTES = 12
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


class DecryptError(RuntimeError):
    """Ciphertext is corrupt, was made with another key, or belongs to another row."""


def load_master_key(path: Path | None = None) -> bytes:
    """Read the 32-byte master key. Error messages name the file, never its content."""
    key_path = path or get_config().master_key_file
    raw = read_secret_file(key_path)
    if not _HEX64.fullmatch(raw):
        raise SecretFileError(f"master key must be 64 hex characters: {key_path}")
    return bytes.fromhex(raw)


def derive_key(purpose: str, master_key: bytes | None = None) -> bytes:
    """A 32-byte subkey for one purpose (HKDF-SHA256, fixed salt, purpose as info)."""
    master = master_key if master_key is not None else load_master_key()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"proxyhub/auth/v1", info=purpose.encode())
    return hkdf.derive(master)


def seal(plaintext: bytes, context: str, *, key: bytes | None = None) -> bytes:
    """Encrypt ``plaintext`` and bind it to ``context`` (associated data)."""
    subkey = key if key is not None else derive_key("secretbox")
    nonce = os.urandom(NONCE_BYTES)
    return bytes([VERSION]) + nonce + AESGCM(subkey).encrypt(nonce, plaintext, context.encode())


def open_sealed(blob: bytes, context: str, *, key: bytes | None = None) -> bytes:
    """Decrypt a blob from :func:`seal`; raises :class:`DecryptError` on any mismatch."""
    if len(blob) < 1 + NONCE_BYTES + 16 or blob[0] != VERSION:
        raise DecryptError("unknown secretbox format")
    subkey = key if key is not None else derive_key("secretbox")
    nonce, body = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
    try:
        return AESGCM(subkey).decrypt(nonce, body, context.encode())
    except InvalidTag as exc:
        raise DecryptError("secretbox authentication failed") from exc


def keyed_digest(purpose: str, value: str, *, key: bytes | None = None) -> str:
    """HMAC-SHA256 of ``value`` under a purpose subkey (hex). Used where the input may be
    low-entropy and must not be recoverable from a database dump (throttle keys)."""
    subkey = key if key is not None else derive_key(purpose)
    return hmac.new(subkey, value.encode(), hashlib.sha256).hexdigest()
