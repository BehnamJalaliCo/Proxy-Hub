"""REALITY key pairs: X25519, made here and kept in the vault (rule 3).

Xray encodes keys as unpadded base64url of the 32 raw bytes (``privateKey`` on the server; the
public key is ``pbk`` in a link). The public key is derived here with ``cryptography`` (RFC 7748,
the same function Xray uses), so it is never read back from Xray's CLI (docs/10 X4).

Why the panel makes the key and not the node (docs/10 section 3.5 first planned the latter): a
slot's key must survive the slot moving to a new server, so that ``slot<N>.<domain>`` links already
posted keep working (M2.6). The private key is sealed in the vault and only ever sent inside the
desired state of that scope's own nodes, over mTLS.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from proxyhub.vault import SecretValue


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class KeyPair:
    private: SecretValue
    public: str

    def __repr__(self) -> str:  # never print the private key
        return f"KeyPair(public={self.public!r}, private=***)"


def new_key_pair() -> KeyPair:
    key = X25519PrivateKey.generate()
    return KeyPair(
        private=SecretValue(b64url(key.private_bytes_raw())),
        public=b64url(key.public_key().public_bytes_raw()),
    )


def public_from_private(private_b64: str) -> str:
    raw = base64.urlsafe_b64decode(private_b64 + "=" * (-len(private_b64) % 4))
    return b64url(X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw())


def new_short_id() -> str:
    """16 hex characters (8 random bytes), the longest short id REALITY accepts."""
    return secrets.token_hex(8)


def new_ws_path() -> str:
    """A scope's WebSocket path for the ws_tls transport: "/" + 16 random hex characters. Not a
    secret (it is in every link of the scope), only harder to stumble on than "/"."""
    return "/" + secrets.token_hex(8)
