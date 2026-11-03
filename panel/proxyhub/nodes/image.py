"""The reviewed node-image files the panel ships to nodes (``node-image/`` in the repository,
``/app/node-image`` in the panel image): versions.lock, provision.sh, host files, cloud-init
template. Nothing here is secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from proxyhub.nodes.settings import get_live_settings

IMAGE_NODE_IMAGE = Path("/app/node-image")
REPO_NODE_IMAGE = Path(__file__).resolve().parents[3] / "node-image"
USER_DATA_TEMPLATE = "cloud-init/node-user-data.yaml.tmpl"
# Same line rule as provision.sh and build-agent.sh (grep -E "^KEY=[A-Za-z0-9._:/+-]+$").
_LOCK_LINE = re.compile(r"([A-Z0-9_]+)=([A-Za-z0-9._:/+-]+)")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_FILE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
REQUIRED_LOCK_KEYS = (
    "TELEMT_VERSION",
    "TELEMT_URL",
    "TELEMT_TARBALL_SHA256",
    "TELEMT_BINARY_SHA256",
    "AGENT_VERSION",
    "AGENT_SHA256",
)


class NodeImageError(RuntimeError):
    """node-image/ is missing or inconsistent (names a file or key, never content)."""


def node_image_dir() -> Path:
    configured = get_live_settings().node_image_dir
    if configured is not None:
        return configured
    return IMAGE_NODE_IMAGE if IMAGE_NODE_IMAGE.is_dir() else REPO_NODE_IMAGE


def parse_versions_lock(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LOCK_LINE.fullmatch(line)
        if m is None:
            raise NodeImageError(f"versions.lock line {number} is not KEY=value")
        key, value = m.groups()
        if key in values:
            raise NodeImageError(f"versions.lock: duplicate {key}")
        values[key] = value
    missing = [k for k in REQUIRED_LOCK_KEYS if k not in values]
    if missing:
        raise NodeImageError(f"versions.lock: missing {', '.join(missing)}")
    for key in ("TELEMT_TARBALL_SHA256", "TELEMT_BINARY_SHA256", "AGENT_SHA256"):
        if not _HEX64.fullmatch(values[key]):
            raise NodeImageError(f"versions.lock: {key} is not a sha256")
    if not values["TELEMT_URL"].startswith("https://github.com/telemt/telemt/releases/download/"):
        raise NodeImageError("versions.lock: TELEMT_URL is not the pinned GitHub release")
    return values


def read_versions_lock(root: Path | None = None) -> dict[str, str]:
    path = (root or node_image_dir()) / "versions.lock"
    try:
        return parse_versions_lock(path.read_text(encoding="ascii"))
    except OSError:
        raise NodeImageError(f"cannot read {path}") from None


@dataclass(frozen=True)
class NodeImage:
    root: Path
    lock: dict[str, str]
    versions_lock: bytes
    provision_sh: bytes
    # node-image/files/<name> -> content, sorted by name.
    files: tuple[tuple[str, bytes], ...]
    template: str


def load_node_image(root: Path | None = None) -> NodeImage:
    base = root or node_image_dir()
    try:
        versions_lock = (base / "versions.lock").read_bytes()
        provision_sh = (base / "provision.sh").read_bytes()
        template = (base / USER_DATA_TEMPLATE).read_text(encoding="utf-8")
        files: list[tuple[str, bytes]] = []
        for path in sorted((base / "files").iterdir()):
            if not path.is_file():
                continue
            if not _FILE_NAME.fullmatch(path.name):
                raise NodeImageError(f"unexpected file name in node-image/files: {path.name!r}")
            files.append((path.name, path.read_bytes()))
    except OSError:
        raise NodeImageError(f"node-image is incomplete under {base}") from None
    if not files:
        raise NodeImageError("node-image/files is empty")
    return NodeImage(
        root=base,
        lock=parse_versions_lock(versions_lock.decode("ascii")),
        versions_lock=versions_lock,
        provision_sh=provision_sh,
        files=tuple(files),
        template=template,
    )
