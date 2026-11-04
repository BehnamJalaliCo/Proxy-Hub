"""The operator account for @MTProxybot (decision D37, docs/11 section 2.1): a SECOND Telegram
account, never the owner's personal one.

GET  /api/mtproxy/account           status: state, first name, Telegram user id, MASKED phone, times
PUT  /api/mtproxy/account           {api_id, api_hash, phone}: real auth.sendCode
POST /api/mtproxy/account/code      {code}: sign in (may move to password_needed)
POST /api/mtproxy/account/password  {password}: the two-step password, used once and never stored
POST /api/mtproxy/account/logout    real auth.logOut, then the vault is emptied (also cancels a login)

Owner only (``require_admin``: session cookie and CSRF on every write). Errors are
``{"error", "message_fa"}`` (plus ``retry_after_s`` for a flood wait, ``attempts_left`` after a
wrong code or password, ``telegram_error`` with Telegram's own error name). No response ever holds
the api_hash, the full phone, a code, a password or a session string, and input values are never
echoed back. This slice sends no message to @MTProxybot or anyone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, SecretStr, StrictInt, StrictStr

from proxyhub.api.deps import AdminIdentity, ApiError, ApiRoute, require_admin
from proxyhub.db.session import get_engine
from proxyhub.mtproxy import service, validation
from proxyhub.mtproxy.messages import STATUS, error_message
from proxyhub.mtproxy.service import Deps, ServiceError
from proxyhub.mtproxy.userclient import ClientOptions
from proxyhub.vault import Vault, VaultError

router = APIRouter(prefix="/api/mtproxy/account", tags=["mtproxy"], route_class=ApiRoute)

Admin = Annotated[AdminIdentity, Depends(require_admin)]


def _error(code: str, *, seconds: int | None = None, extra: dict[str, Any] | None = None) -> ApiError:
    return ApiError(
        code,
        status_code=STATUS.get(code, 400),
        message_fa=error_message(code, seconds=seconds),
        extra=extra,
    )


def _from_service(exc: ServiceError) -> ApiError:
    extra: dict[str, Any] = {}
    if exc.seconds is not None:
        extra["retry_after_s"] = exc.seconds
    if exc.attempts_left is not None:
        extra["attempts_left"] = exc.attempts_left
    if exc.rpc is not None:
        extra["telegram_error"] = exc.rpc
    return _error(exc.code, seconds=exc.seconds, extra=extra)


def get_vault() -> Vault:
    try:
        return Vault.from_config()
    except VaultError:
        raise _error("vault_unavailable") from None


def get_client_options() -> ClientOptions:
    """How the panel reaches Telegram (tests point it at a local port)."""
    return ClientOptions()


def get_lock_wait_s() -> float:
    return service.DEFAULT_LOCK_WAIT_S


def get_deps(
    request: Request,
    vault: Annotated[Vault, Depends(get_vault)],
    options: Annotated[ClientOptions, Depends(get_client_options)],
    lock_wait_s: Annotated[float, Depends(get_lock_wait_s)],
) -> Deps:
    return Deps(
        engine=get_engine(),
        vault=vault,
        redis=getattr(request.app.state, "redis", None),
        options=options,
        lock_wait_s=lock_wait_s,
    )


DepsDep = Annotated[Deps, Depends(get_deps)]


def _actor(admin: AdminIdentity) -> str:
    return f"admin:{admin.username}"


async def _status(deps: Deps) -> dict[str, Any]:
    return await service.status(deps.engine, now=deps.clock())


@router.get("")
async def get_account(admin: Admin) -> dict[str, Any]:
    # Reading the status decrypts nothing, so it needs neither the vault nor Redis.
    return await service.status(get_engine(), now=datetime.now(UTC))


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_id: StrictInt | StrictStr
    api_hash: SecretStr
    phone: SecretStr


@router.put("")
async def start_login(admin: Admin, body: LoginIn, deps: DepsDep) -> dict[str, Any]:
    try:
        api_id = validation.parse_api_id(body.api_id)
        api_hash = validation.parse_api_hash(body.api_hash.get_secret_value())
        phone = validation.parse_phone(body.phone.get_secret_value())
    except validation.InputError as exc:
        raise _error(exc.code) from None
    try:
        await service.start_login(deps, api_id=api_id, api_hash=api_hash, phone=phone, actor=_actor(admin))
    except ServiceError as exc:
        raise _from_service(exc) from None
    return await _status(deps)


class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr


@router.post("/code")
async def submit_code(admin: Admin, body: CodeIn, deps: DepsDep) -> dict[str, Any]:
    try:
        code = validation.parse_code(body.code.get_secret_value())
    except validation.InputError as exc:
        raise _error(exc.code) from None
    try:
        await service.submit_code(deps, code=code, actor=_actor(admin))
    except ServiceError as exc:
        raise _from_service(exc) from None
    return await _status(deps)


class PasswordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr


@router.post("/password")
async def submit_password(admin: Admin, body: PasswordIn, deps: DepsDep) -> dict[str, Any]:
    try:
        password = validation.parse_password(body.password.get_secret_value())
    except validation.InputError as exc:
        raise _error(exc.code) from None
    try:
        await service.submit_password(deps, password=password, actor=_actor(admin))
    except ServiceError as exc:
        raise _from_service(exc) from None
    return await _status(deps)


@router.post("/logout")
async def logout(admin: Admin, deps: DepsDep) -> dict[str, Any]:
    try:
        await service.logout(deps, actor=_actor(admin))
    except ServiceError as exc:
        raise _from_service(exc) from None
    return await _status(deps)
