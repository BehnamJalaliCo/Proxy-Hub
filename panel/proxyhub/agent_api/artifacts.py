"""The node agent binary served to nodes during provisioning (``GET /agent/v1/artifacts/proxyhub-agent``).

panel/Dockerfile builds it with node-image/build-agent.sh (pinned Go image, reproducible flags) and
fails the build unless its sha256 equals ``AGENT_SHA256`` in node-image/versions.lock. The API
checks again before serving and serves exactly the bytes it hashed: a binary that differs from
the lock (or a missing lock) is never served. The node checks the same hash before installing.

The route is public (a node has no certificate yet), so the verified bytes are kept in memory and
hashed again only when the file (inode, size, mtime) or the pinned hash changes: a request costs a
stat and a small file read, not a multi-megabyte hash.
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


# (path, inode, size, mtime_ns, pinned sha256, version) -> the artifact verified for exactly that.
_verified: dict[tuple[str, int, int, int, str, str], Artifact] = {}


def _load(path: Path) -> Artifact:
    try:
        lock = read_versions_lock()
    except NodeImageError:
        raise ArtifactUnavailable("versions_lock_unavailable") from None
    try:
        st = path.stat()
    except OSError:
        raise ArtifactUnavailable("artifact_missing") from None
    if st.st_size > MAX_ARTIFACT_BYTES:
        raise ArtifactUnavailable("artifact_too_large")
    key = (str(path), st.st_ino, st.st_size, st.st_mtime_ns, lock["AGENT_SHA256"], lock["AGENT_VERSION"])
    cached = _verified.get(key)
    if cached is not None:
        return cached
    try:
        data = path.read_bytes()
    except OSError:
        raise ArtifactUnavailable("artifact_missing") from None
    digest = hashlib.sha256(data).hexdigest()
    if digest != lock["AGENT_SHA256"]:
        raise ArtifactUnavailable("artifact_hash_mismatch")
    artifact = Artifact(data=data, sha256=digest, version=lock["AGENT_VERSION"])
    _verified.clear()  # one binary at a time
    _verified[key] = artifact
    return artifact


async def load_agent_artifact() -> Artifact:
    return await asyncio.to_thread(_load, get_agent_api_settings().agent_artifact_file)
