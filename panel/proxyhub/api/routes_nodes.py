"""Nodes (team live): list, request the one test node, retire it, read a node's events.

POST /api/nodes/test creates nothing on Hetzner by itself: it records a ``requested`` test node
if every safety gate allows it (else 409 with the gate's reasons); the worker's ``nodes`` loop then
creates the server through the gates again, with a live look at the account. POST .../retire
checks the delete gate on a live read of the server; the loop deletes it through DeletePermit.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, db_session, require_admin
from proxyhub.hetzner.client import HetznerReader
from proxyhub.nodes import service
from proxyhub.vault import Vault

router = APIRouter(prefix="/api", tags=["nodes"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]


def get_vault_factory() -> Callable[[], Vault]:
    """The vault is opened only when a retire needs a live Hetzner read (overridable in tests)."""
    return Vault.from_config


def get_reader_factory() -> Callable[[str], HetznerReader]:
    return HetznerReader


class TestNodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location: str | None = Field(default=None, pattern=r"^[a-z]{3}\d?$")


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


def _node_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise _error(service.NodeNotFound()) from None


def _error(exc: service.NodeServiceError) -> ApiError:
    status = {"gate_refused": 409, "test_node_exists": 409, "not_found": 404}.get(exc.code, 400)
    extra = (
        {"reasons": exc.reasons, "reasons_fa": service.reasons_fa(exc.reasons)}
        if isinstance(exc, service.GateRefused)
        else None
    )
    return ApiError(exc.code, status_code=status, message_fa=service.MESSAGES_FA.get(exc.code), extra=extra)


@router.get("/nodes")
async def list_nodes(admin: Admin, db: Db) -> list[dict[str, Any]]:
    return await service.list_nodes(db, datetime.now(UTC))


@router.post("/nodes/test", status_code=202)
async def request_test_node(admin: Admin, db: Db, body: TestNodeIn | None = None) -> JSONResponse:
    try:
        node_id = await service.request_test_node(
            db, actor=_actor(admin), now=datetime.now(UTC), location=body.location if body else None
        )
    except service.NodeServiceError as exc:
        raise _error(exc) from None
    return JSONResponse({"node_id": str(node_id)}, status_code=202)


@router.post("/nodes/{node_id}/retire", status_code=202)
async def retire_node(
    admin: Admin,
    node_id: str,
    db: Db,
    vault_factory: Annotated[Callable[[], Vault], Depends(get_vault_factory)],
    reader_factory: Annotated[Callable[[str], HetznerReader], Depends(get_reader_factory)],
) -> JSONResponse:
    try:
        await service.request_retire(
            db,
            _node_id(node_id),
            actor=_actor(admin),
            now=datetime.now(UTC),
            vault_factory=vault_factory,
            reader_factory=reader_factory,
        )
    except service.NodeServiceError as exc:
        raise _error(exc) from None
    return JSONResponse({}, status_code=202)


@router.get("/nodes/{node_id}/events")
async def node_events(admin: Admin, node_id: str, db: Db) -> list[dict[str, Any]]:
    try:
        return await service.node_events(db, _node_id(node_id))
    except service.NodeServiceError as exc:
        raise _error(exc) from None
