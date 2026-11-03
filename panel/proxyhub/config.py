"""Runtime configuration.

Secrets never come from environment variables or the repo (CLAUDE.md rule 3).
They are files mounted at ``/run/secrets/<name>`` from ``/etc/proxy-hub/secrets``
on the host (a root-only 0700 directory). Only the *path* is configured here.
"""

from __future__ import annotations

import os
import stat
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict


class SecretFileError(RuntimeError):
    """A secret file is missing, empty, or has unsafe permissions."""


def read_secret_file(path: Path) -> str:
    """Read a secret file and refuse unsafe ones (world-writable, empty)."""
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise SecretFileError(f"secret file missing: {path}") from exc
    if st.st_mode & stat.S_IWOTH:
        raise SecretFileError(f"secret file is world-writable: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SecretFileError(f"secret file is empty: {path}")
    return value


class RuntimeConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROXYHUB_", extra="ignore")

    # Full URL override: only for tests and local development against a throwaway database.
    database_url: str | None = None
    db_host: str = "postgres"
    db_port: int = 5432
    db_name: str = "proxyhub"
    db_user: str = "proxyhub"
    db_password_file: Path = Path("/run/secrets/db_password")

    redis_url: str | None = None
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password_file: Path = Path("/run/secrets/redis_password")

    # 32-byte hex key that encrypts secrets stored in the database (Hetzner/Telegram/Cloudflare tokens).
    master_key_file: Path = Path("/run/secrets/master_key")
    # EC P-256 private key (PEM) of the panel's own CA that signs node agent certificates (mTLS).
    agent_ca_key_file: Path = Path("/run/secrets/agent_ca_key")

    log_level: str = "INFO"
    git_commit: str = os.environ.get("PROXYHUB_GIT_COMMIT", "unknown")

    def async_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        password = quote(read_secret_file(self.db_password_file), safe="")
        return f"postgresql+asyncpg://{self.db_user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"

    def redis_dsn(self) -> str:
        if self.redis_url:
            return self.redis_url
        password = quote(read_secret_file(self.redis_password_file), safe="")
        return f"redis://:{password}@{self.redis_host}:{self.redis_port}/0"


@lru_cache(maxsize=1)
def get_config() -> RuntimeConfig:
    return RuntimeConfig()
