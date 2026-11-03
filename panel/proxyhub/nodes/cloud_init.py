"""User data (cloud-init) of one node, rendered from node-image/cloud-init/node-user-data.yaml.tmpl.

The result contains the node's one-time registration token. It is wrapped in :class:`UserData`,
whose ``repr``/``str`` never show the content; the executor hands ``.text`` only to the Hetzner
create call and never logs it (CLAUDE.md rule 3). Every substituted value is either base64 or
checked here first (UUID, DNS name, token alphabet), so nothing can break out of the YAML.
"""

from __future__ import annotations

import base64
import gzip
import re
import string
import uuid

from proxyhub.agent_api.validation import valid_domain
from proxyhub.nodes.image import NodeImage, NodeImageError

# Hetzner refuses user data larger than 32 KiB.
MAX_USER_DATA_BYTES = 32 * 1024
NODE_DIR = "/opt/proxyhub-node"
ARTIFACT_PATH = "/agent/v1/artifacts/proxyhub-agent"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_PEM = re.compile(r"-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n?")
_TEMPLATE_TELEMT = re.compile(r'"telemt_version": "([^"]+)"')


class UserDataError(ValueError):
    """Rendering refused (names the problem, never a value)."""


class UserData:
    """Rendered user data. Holds a secret (the registration token): it never prints itself."""

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def text(self) -> str:
        return self._text

    @property
    def size(self) -> int:
        return len(self._text.encode("utf-8"))

    def __repr__(self) -> str:
        return f"UserData(<{self.size} bytes, contains a registration token>)"

    __str__ = __repr__

    def __reduce__(self) -> tuple[object, ...]:  # never pickled into a queue or a cache
        raise TypeError("UserData cannot be pickled")


def _gz_b64(data: bytes) -> str:
    # mtime=0: the same files always give the same bytes (reviewable, comparable).
    return base64.b64encode(gzip.compress(data, compresslevel=9, mtime=0)).decode("ascii")


def _file_entry(path: str, mode: str, data: bytes) -> str:
    return (
        f"  - path: {path}\n"
        "    owner: root:root\n"
        f"    permissions: '{mode}'\n"
        "    encoding: gz+b64\n"
        f"    content: {_gz_b64(data)}\n"
    )


def node_image_entries(image: NodeImage) -> str:
    entries = [
        _file_entry(f"{NODE_DIR}/versions.lock", "0600", image.versions_lock),
        _file_entry(f"{NODE_DIR}/provision.sh", "0700", image.provision_sh),
    ]
    entries.extend(_file_entry(f"{NODE_DIR}/files/{name}", "0600", data) for name, data in image.files)
    return "".join(entries).rstrip("\n")


def render_user_data(
    image: NodeImage, *, node_id: uuid.UUID, agent_domain: str, token: str, ca_pem: str
) -> UserData:
    if not isinstance(node_id, uuid.UUID):
        raise UserDataError("node_id must be a UUID")
    if not valid_domain(agent_domain):
        raise UserDataError("agent domain must be a plain DNS name")
    if not _TOKEN.fullmatch(token):
        raise UserDataError("registration token has an unexpected shape")
    if not _PEM.fullmatch(ca_pem):
        raise UserDataError("agent CA must be one PEM certificate")
    pinned = _TEMPLATE_TELEMT.findall(image.template)
    if pinned != [image.lock["TELEMT_VERSION"]]:
        raise NodeImageError("cloud-init template telemt_version differs from versions.lock")
    panel_url = f"https://{agent_domain}"
    try:
        text = string.Template(image.template).substitute(
            NODE_IMAGE_FILES=node_image_entries(image),
            PANEL_AGENT_URL=panel_url,
            NODE_ID=str(node_id),
            AGENT_CA_PEM_B64=base64.b64encode(ca_pem.encode("ascii")).decode("ascii"),
            REGISTRATION_TOKEN_B64=base64.b64encode(token.encode("ascii")).decode("ascii"),
            AGENT_ARTIFACT_URL=panel_url + ARTIFACT_PATH,
        )
    except (KeyError, ValueError):
        raise NodeImageError("cloud-init template has an unknown or malformed placeholder") from None
    data = UserData(text)
    if data.size > MAX_USER_DATA_BYTES:
        raise UserDataError(f"user data is {data.size} bytes; Hetzner accepts at most {MAX_USER_DATA_BYTES}")
    return data
