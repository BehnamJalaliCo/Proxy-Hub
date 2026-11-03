"""Auth API (M1.3): setup link, login, logout, sessions, recovery codes.

Public routes: GET /api/setup/status, POST /api/setup/begin, POST /api/setup/finish and
POST /api/auth/login. Everything else needs the admin session. Every non-GET route needs the CSRF
header. No route reads the client address.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.api.deps import (
    SESSION_COOKIE,
    AdminIdentity,
    ApiError,
    ApiRoute,
    api_error_response,
    db_session,
    require_admin,
    require_csrf,
)
from proxyhub.auth import service
from proxyhub.auth.service import SESSION_ABSOLUTE_TIMEOUT, AuthError, NewSession

router = APIRouter(prefix="/api", route_class=ApiRoute, dependencies=[Depends(require_csrf)], tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(db_session)]
Admin = Annotated[AdminIdentity, Depends(require_admin)]


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)


class SetupBeginIn(_In):
    token: str = Field(max_length=128)


class SetupFinishIn(_In):
    token: str = Field(max_length=128)
    enrollment_id: str = Field(max_length=64)
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)
    totp_code: str = Field(max_length=32)


class LoginIn(_In):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)
    totp_code: str | None = Field(default=None, max_length=32)
    recovery_code: str | None = Field(default=None, max_length=64)


class TotpIn(_In):
    totp_code: str = Field(max_length=32)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _error(exc: AuthError) -> JSONResponse:
    if exc.retry_after_s is not None:
        return api_error_response(
            ApiError(
                exc.code,
                extra={"retry_after_s": exc.retry_after_s},
                headers={"Retry-After": str(exc.retry_after_s)},
            )
        )
    return api_error_response(ApiError(exc.code))


def _set_session_cookie(response: Response, new_session: NewSession) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        new_session.token,
        max_age=int(SESSION_ABSOLUTE_TIMEOUT.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


@router.get("/setup/status")
async def setup_status(session: DbSession) -> dict[str, bool]:
    return {"setup_required": await service.setup_required(session)}


@router.post("/setup/begin")
async def setup_begin(body: SetupBeginIn, session: DbSession) -> Response:
    try:
        result = await service.setup_begin(session, body.token)
    except AuthError as exc:
        return _error(exc)
    return JSONResponse(
        {
            "enrollment_id": str(result.enrollment_id),
            "totp_uri": result.totp_uri,
            "totp_secret": result.totp_secret,
        }
    )


@router.post("/setup/finish")
async def setup_finish(body: SetupFinishIn, request: Request, session: DbSession) -> Response:
    try:
        result = await service.setup_finish(
            session,
            token=body.token,
            enrollment_id=body.enrollment_id,
            username=body.username,
            password=body.password,
            totp_code=body.totp_code,
            user_agent=request.headers.get("user-agent"),
        )
    except AuthError as exc:
        return _error(exc)
    response = JSONResponse({"recovery_codes": result.recovery_codes})
    _set_session_cookie(response, result.session)
    return response


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, session: DbSession) -> Response:
    try:
        result = await service.login(
            session,
            username=body.username,
            password=body.password,
            totp_code=body.totp_code,
            recovery_code=body.recovery_code,
            user_agent=request.headers.get("user-agent"),
            presented_cookie=request.cookies.get(SESSION_COOKIE),
        )
    except AuthError as exc:
        return _error(exc)
    response = JSONResponse({"username": result.username})
    _set_session_cookie(response, result.session)
    return response


@router.post("/auth/logout", status_code=204)
async def logout(admin: Admin, session: DbSession) -> Response:
    await service.logout(session, admin.resolved())
    response = Response(status_code=204)
    _clear_session_cookie(response)
    return response


@router.get("/auth/me")
async def me(admin: Admin, session: DbSession) -> dict[str, Any]:
    info = await service.account_info(session, uuid.UUID(admin.user_id))
    return {
        "username": info.username,
        "created_at": _iso(info.created_at),
        "last_login_at": _iso(info.last_login_at),
    }


@router.get("/auth/sessions")
async def sessions(admin: Admin, session: DbSession) -> list[dict[str, Any]]:
    rows = await service.list_sessions(session, uuid.UUID(admin.user_id))
    return [
        {
            "id": str(row.id),
            "created_at": _iso(row.created_at),
            "last_seen_at": _iso(row.last_seen_at),
            "user_agent": row.user_agent,
            "current": str(row.id) == admin.session_id,
        }
        for row in rows
    ]


@router.delete("/auth/sessions/{session_id}", status_code=204)
async def revoke(session_id: str, admin: Admin, session: DbSession) -> Response:
    try:
        await service.revoke_session(session, admin.resolved(), session_id)
    except AuthError as exc:
        return _error(exc)
    response = Response(status_code=204)
    if uuid.UUID(session_id) == uuid.UUID(admin.session_id):
        _clear_session_cookie(response)
    return response


@router.post("/auth/recovery-codes/regenerate")
async def regenerate_recovery_codes(body: TotpIn, admin: Admin, session: DbSession) -> Response:
    try:
        codes = await service.regenerate_recovery_codes(session, admin.resolved(), body.totp_code)
    except AuthError as exc:
        return _error(exc)
    return JSONResponse({"recovery_codes": codes})
