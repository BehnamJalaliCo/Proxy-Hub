"""Telemt user secrets at rest: AES-256-GCM under a subkey of the panel master key (rule 3).

The master key is ``config.master_key_file`` (64 hex characters, deploy/ensure_secrets.sh). The
agent API derives its own purpose key with HKDF-SHA256, so it never shares a key with another
subsystem, and binds every ciphertext to its row (node id + user name) as associated data, so a
sealed secret copied to another row does not open.

Blob: ``0x01 | nonce (12) | ciphertext (16) | tag (16)`` = 45 bytes for a 16-byte Telemt secret.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from proxyhub.config import SecretFileError, read_secret_file

VERSION = 1
NONCE_BYTES = 12
SECRET_BYTES = 16  # Telemt: exactly 32 hex characters
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")


class SealError(RuntimeError):
    """A sealed value is corrupt, from another key, or from another row. Carries no secret."""


def load_seal_key(master_key_file: Path) -> bytes:
    raw = read_secret_file(master_key_file)
    if not _HEX64.fullmatch(raw):
        raise SecretFileError(f"master key must be 64 hex characters: {master_key_file}")
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"proxyhub/agent/v1", info=b"telemt-user-secret")
    return hkdf.derive(bytes.fromhex(raw))


def _context(node_id: uuid.UUID, name: str) -> bytes:
    return f"telemt-user:{node_id}:{name}".encode()


def seal_secret(key: bytes, node_id: uuid.UUID, name: str, secret: bytes) -> bytes:
    if len(secret) != SECRET_BYTES:
        raise SealError("telemt secret must be 16 bytes")
    nonce = os.urandom(NONCE_BYTES)
    return bytes([VERSION]) + nonce + AESGCM(key).encrypt(nonce, secret, _context(node_id, name))


def open_secret(key: bytes, node_id: uuid.UUID, name: str, blob: bytes) -> bytes:
    if len(blob) != 1 + NONCE_BYTES + SECRET_BYTES + 16 or blob[0] != VERSION:
        raise SealError("unknown sealed secret format")
    nonce, body = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
    try:
        return AESGCM(key).decrypt(nonce, body, _context(node_id, name))
    except InvalidTag:
        raise SealError("sealed secret failed authentication") from None
