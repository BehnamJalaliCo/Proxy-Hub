"""Shared FastAPI dependencies. Feature teams IMPORT these; only the auth team edits this file.

- ``db_session``: one ``AsyncSession`` per request.
- ``require_admin``: the owner's session (cookie ``ph_session``). It also enforces the CSRF rule
  (every non-GET request needs ``X-Requested-With: proxyhub``), so any route that depends on it is
  protected against both. Returns :class:`AdminIdentity`.
- ``require_csrf``: the CSRF rule alone, for the few public POST routes (setup and login).
- ``ApiError``: raise it for a contract error body ``{"error": ..., "message_fa": ...}``.
  ``ApiRoute`` (``APIRouter(route_class=ApiRoute)``) renders it in that shape inside a router;
  ``install_error_handlers(app)`` does the same for the whole app.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.auth.messages import MESSAGES_FA, STATUS
from proxyhub.auth.service import ResolvedSession, resolve_session
from proxyhub.db.session import get_sessionmaker

SESSION_COOKIE = "ph_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "proxyhub"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class AdminIdentity:
    user_id: str
    username: str
    session_id: str

    def resolved(self) -> ResolvedSession:
        return ResolvedSession(
            user_id=uuid.UUID(self.user_id), username=self.username, session_id=uuid.UUID(self.session_id)
        )


class ApiError(HTTPException):
    """An API error in the contract shape. Without a handler it still has the right status code."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        message_fa: str | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body: dict[str, Any] = {
            "error": code,
            "message_fa": message_fa or MESSAGES_FA.get(code, MESSAGES_FA["invalid_request"]),
            **(extra or {}),
        }
        super().__init__(status_code=status_code or STATUS.get(code, 400), detail=self.body, headers=headers)


def api_error_response(exc: ApiError) -> JSONResponse:
    return JSONResponse(exc.body, status_code=exc.status_code, headers=exc.headers)


class ApiRoute(APIRoute):
    """Renders ``ApiError`` and request validation errors as ``{"error", "message_fa"}`` (input
    values are never echoed back) and marks every response ``Cache-Control: no-store``."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except ApiError as exc:
                response = api_error_response(exc)
            except RequestValidationError:
                response = api_error_response(ApiError("invalid_request"))
            response.headers.setdefault("Cache-Control", "no-store")
            return response

        return handler


def install_error_handlers(app: FastAPI) -> None:
    """App-wide: render ``ApiError`` from any router in the contract shape."""

    async def _handle(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, ApiError)
        return api_error_response(exc)

    async def _invalid(request: Request, exc: Exception) -> Response:
        # FastAPI's default 422 body echoes the submitted input; a token could come back (rule 3).
        return api_error_response(ApiError("invalid_request", status_code=422))

    app.add_exception_handler(ApiError, _handle)
    app.add_exception_handler(RequestValidationError, _invalid)


async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


def require_csrf(request: Request) -> None:
    """Every non-GET /api request must carry ``X-Requested-With: proxyhub``. Browsers cannot add a
    custom header to a cross-site request without a CORS preflight, which this API never grants."""
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise ApiError("csrf")


async def require_admin(
    request: Request, session: Annotated[AsyncSession, Depends(db_session)]
) -> AdminIdentity:
    """The owner's live session, or 401. Checks CSRF first. Leaves no transaction open."""
    require_csrf(request)
    resolved = await resolve_session(session, request.cookies.get(SESSION_COOKIE))
    if resolved is None:
        raise ApiError("unauthenticated")
    return AdminIdentity(
        user_id=str(resolved.user_id), username=resolved.username, session_id=str(resolved.session_id)
    )
