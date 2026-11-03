"""The only module allowed to create or delete Hetzner servers.

Every function takes a permit issued by ``proxyhub.safety.gates``. Deletion re-reads the
server from Hetzner right before deleting and refuses if anything changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from hcloud.images import Image
from hcloud.locations import Location
from hcloud.server_types import ServerType

from proxyhub.hetzner.client import HetznerReader, _delete_protected
from proxyhub.safety.gates import (
    MANAGED_LABEL,
    MANAGED_VALUE,
    CreatePermit,
    DeletePermit,
    PermitError,
)


@dataclass(frozen=True)
class CreateSpec:
    name: str
    server_type: str
    location: str
    image: str
    role: str
    node_id: str
    user_data: str | None = None
    is_test: bool = False


def create_server(reader: HetznerReader, permit: CreatePermit, spec: CreateSpec) -> int:
    if not isinstance(permit, CreatePermit) or not permit.decision.allowed:
        raise PermitError("a valid CreatePermit is required")
    labels = {MANAGED_LABEL: MANAGED_VALUE, "role": spec.role, "node-id": spec.node_id}
    if spec.is_test:
        labels["purpose"] = "test"
    resp = reader.raw.servers.create(
        name=spec.name,
        server_type=ServerType(name=spec.server_type),
        image=Image(name=spec.image),
        location=Location(name=spec.location),
        labels=labels,
        user_data=spec.user_data,
    )
    server_id = resp.server.id
    if server_id is None:
        raise RuntimeError("Hetzner returned a server without id")
    return int(server_id)


def delete_server(reader: HetznerReader, permit: DeletePermit) -> None:
    if not isinstance(permit, DeletePermit) or not permit.decision.allowed:
        raise PermitError("a valid DeletePermit is required")
    server = reader.raw.servers.get_by_id(permit.hetzner_id)
    labels = dict(server.labels or {})
    if labels.get(MANAGED_LABEL) != MANAGED_VALUE or labels.get("role") not in {"node", "builder"}:
        raise PermitError("server labels changed since the permit was issued")
    if _delete_protected(server.protection):
        raise PermitError("delete protection is on")
    reader.raw.servers.delete(server)
