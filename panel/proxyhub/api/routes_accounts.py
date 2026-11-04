"""Hetzner node accounts: list, add (verified for real against Hetzner), drain, retire."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.accounts import service
from proxyhub.api.deps import AdminIdentity, db_session, require_admin
from proxyhub.fleet.http import ApiError, ContractRoute
from proxyhub.vault import Vault, VaultKeyError

router = APIRouter(prefix="/api", tags=["accounts"], route_class=ContractRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]
Db = Annotated[AsyncSession, Depends(db_session)]

_STATUS: dict[type[service.AccountError], int] = {
    service.InvalidName: 400,
    service.NameTaken: 400,
    service.TokenInvalid: 400,
    service.TokenReadOnly: 400,
    service.AccountNotEmpty: 409,
    service.HetznerUnavailable: 502,
    service.AccountNotFound: 404,
    service.IllegalTransition: 409,
    service.AccountHasNodes: 409,
}


def _api_error(exc: service.AccountError) -> ApiError:
    # hetzner_code stays server-side; only contract fields go out.
    extra = {k: v for k, v in exc.extra.items() if k in ("foreign_servers", "live_nodes", "state")}
    return ApiError(_STATUS.get(type(exc), 400), exc.code, **extra)


def get_vault() -> Vault:
    try:
        return Vault.from_config()
    except VaultKeyError:
        raise ApiError(503, "vault_unavailable") from None


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    token: SecretStr = Field(min_length=1, max_length=512)


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


@router.get("/accounts")
async def list_accounts(admin: Admin, db: Db) -> list[dict[str, Any]]:
    return await service.list_accounts(db)


@router.post("/accounts", status_code=201)
async def add_account(
    admin: Admin, body: AccountIn, db: Db, vault: Annotated[Vault, Depends(get_vault)]
) -> dict[str, Any]:
    try:
        token = service.clean_token(body.token.get_secret_value())
        return await service.add_account(
            db, vault, name=body.name, token=token, actor=_actor(admin), now=datetime.now(UTC)
        )
    except service.AccountError as exc:
        await db.rollback()
        raise _api_error(exc) from None


@router.post("/accounts/{account_id}/drain")
async def drain_account(admin: Admin, account_id: uuid.UUID, db: Db) -> dict[str, Any]:
    try:
        await service.drain_account(db, account_id, actor=_actor(admin))
        await db.commit()
        return await service.get_account(db, account_id)
    except service.AccountError as exc:
        await db.rollback()
        raise _api_error(exc) from None


@router.post("/accounts/{account_id}/retire")
async def retire_account(admin: Admin, account_id: uuid.UUID, db: Db) -> dict[str, Any]:
    try:
        await service.retire_account(db, account_id, actor=_actor(admin))
        await db.commit()
        return await service.get_account(db, account_id)
    except service.AccountError as exc:
        await db.rollback()
        raise _api_error(exc) from None
