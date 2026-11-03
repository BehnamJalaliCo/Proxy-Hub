"""Secrets the agent API needs at request time, loaded lazily from files (never env, never repo).

* the agent CA key (``config.agent_ca_key_file``) to sign node certificates;
* the master key (``config.master_key_file``) for the Telemt-secret sealing subkey;
* the Caddy-to-API shared secret (``agent_proxy_secret_file``, default
  ``/run/secrets/agent_proxy_secret``, created by deploy/ensure_secrets.sh).

They are read on first use, not at import, so the panel starts even before a secret is mounted:
until then every ``/agent/v1`` request fails closed with 503.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from pydantic_settings import BaseSettings, SettingsConfigDict

from proxyhub.agent_api.sealing import load_seal_key
from proxyhub.config import SecretFileError, get_config, read_secret_file
from proxyhub.pki.ca import CaKeyError, load_ca_key

# At least 32 characters of URL-safe text (ensure_secrets.sh writes 64 hex characters).
_PROXY_SECRET = re.compile(r"[A-Za-z0-9_\-]{32,256}")


class AgentApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROXYHUB_", extra="ignore")

    agent_proxy_secret_file: Path = Path("/run/secrets/agent_proxy_secret")
    # The node agent binary built reproducibly in panel/Dockerfile (served to nodes at
    # GET /agent/v1/artifacts/proxyhub-agent only while its sha256 equals AGENT_SHA256).
    agent_artifact_file: Path = Path("/app/artifacts/proxyhub-agent")


@lru_cache(maxsize=1)
def get_agent_api_settings() -> AgentApiSettings:
    return AgentApiSettings()


@dataclass(frozen=True)
class AgentRuntime:
    ca_key: ec.EllipticCurvePrivateKey
    proxy_secret: bytes
    seal_key: bytes

    def __repr__(self) -> str:  # never print key material
        return "AgentRuntime(<secrets hidden>)"


def load_runtime() -> AgentRuntime:
    config = get_config()
    proxy_secret_file = get_agent_api_settings().agent_proxy_secret_file
    proxy_secret = read_secret_file(proxy_secret_file)
    if not _PROXY_SECRET.fullmatch(proxy_secret):
        raise SecretFileError(f"agent proxy secret must be 32+ URL-safe characters: {proxy_secret_file}")
    return AgentRuntime(
        ca_key=load_ca_key(config.agent_ca_key_file),
        proxy_secret=proxy_secret.encode("ascii"),
        seal_key=load_seal_key(config.master_key_file),
    )


_cache: AgentRuntime | None = None


def reset_runtime_cache() -> None:
    """Forget loaded secrets (tests, and after a secret file is rotated)."""
    global _cache
    _cache = None


async def get_agent_runtime() -> AgentRuntime:
    """FastAPI dependency. Missing or unsafe secret files mean 503, never a weaker check.

    Loading twice in a race is harmless (same files, same result), so no lock is needed.
    """
    global _cache
    if _cache is None:
        try:
            _cache = await asyncio.to_thread(load_runtime)
        except (SecretFileError, CaKeyError, OSError):
            raise HTTPException(status_code=503, detail="agent api not configured") from None
    return _cache
