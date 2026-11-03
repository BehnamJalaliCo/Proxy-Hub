"""The node agent binary served to nodes during provisioning (``GET /agent/v1/artifacts/proxyhub-agent``).

panel/Dockerfile builds it with node-image/build-agent.sh (pinned Go image, reproducible flags) and
fails the build unless its sha256 equals ``AGENT_SHA256`` in node-image/versions.lock. The API
checks again on every request and serves exactly the bytes it hashed: a binary that differs from
the lock (or a missing lock) is never served. The node checks the same hash before installing.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from proxyhub.agent_api.runtime import get_agent_api_settings
from proxyhub.nodes.image import NodeImageError, read_versions_lock

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


class ArtifactUnavailable(RuntimeError):
    """Missing, too large, or not the pinned binary (the reason is a short code)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Artifact:
    data: bytes
    sha256: str
    version: str


def _load(path: Path) -> Artifact:
    try:
        lock = read_versions_lock()
    except NodeImageError:
        raise ArtifactUnavailable("versions_lock_unavailable") from None
    try:
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ArtifactUnavailable("artifact_too_large")
        data = path.read_bytes()
    except OSError:
        raise ArtifactUnavailable("artifact_missing") from None
    digest = hashlib.sha256(data).hexdigest()
    if digest != lock["AGENT_SHA256"]:
        raise ArtifactUnavailable("artifact_hash_mismatch")
    return Artifact(data=data, sha256=digest, version=lock["AGENT_VERSION"])


async def load_agent_artifact() -> Artifact:
    return await asyncio.to_thread(_load, get_agent_api_settings().agent_artifact_file)
