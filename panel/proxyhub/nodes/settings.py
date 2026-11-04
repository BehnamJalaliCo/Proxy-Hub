"""Settings of the live node pipeline (environment, never secrets).

``PROXYHUB_AGENT_DOMAIN`` is the node agents' site (``agent.<domain>``, Cloudflare grey). The
deployer passes ``AGENT_DOMAIN`` from /etc/proxy-hub/deploy.env to the api and worker containers.
Without it no node can be created: its user data would have nowhere to register.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from proxyhub.agent_api.validation import valid_domain


class LiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROXYHUB_", extra="ignore")

    agent_domain: str | None = None
    # node-image/ (versions.lock, provision.sh, files/, cloud-init/). Default: /app/node-image in
    # the panel image, else the repository checkout.
    node_image_dir: Path | None = None
    # Hetzner's stock image; provision.sh accepts Ubuntu 24.04 x86_64 only.
    node_os_image: str = "ubuntu-24.04"

    def checked_agent_domain(self) -> str | None:
        """The agent domain if it is a plain DNS name (never an IP or localhost), else None."""
        value = (self.agent_domain or "").strip()
        return value if value and valid_domain(value) else None


@lru_cache(maxsize=1)
def get_live_settings() -> LiveSettings:
    return LiveSettings()
