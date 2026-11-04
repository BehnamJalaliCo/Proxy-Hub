"""The sealing subkey that opens Telemt user secrets (the same one the agent API uses)."""

from __future__ import annotations

from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.config import SecretFileError, get_config


class SealKeyUnavailable(RuntimeError):
    """The panel master key file is missing or unsafe (the message never holds key material)."""


def load_proxy_seal_key() -> bytes:
    try:
        return load_seal_key(get_config().master_key_file)
    except (SecretFileError, OSError):
        raise SealKeyUnavailable("master key file is not available") from None
