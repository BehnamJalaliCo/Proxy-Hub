"""Shared FastAPI dependencies. Feature teams IMPORT these; only the auth team edits this file.

``require_admin`` is implemented by the auth team (proxyhub/auth). Until then it refuses every
request with 503, so no protected route can ever be reached without authentication.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from proxyhub.db.session import get_sessionmaker


@dataclass(frozen=True)
class AdminIdentity:
    user_id: str
    username: str
    session_id: str


async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


async def require_admin(request: Request) -> AdminIdentity:
    raise HTTPException(status_code=503, detail="authentication not installed yet")
