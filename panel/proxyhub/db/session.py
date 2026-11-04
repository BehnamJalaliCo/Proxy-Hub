from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from proxyhub.config import get_config


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    # The worker runs ~18 loops; each tick holds two connections (advisory lock + work), so the
    # pool allows 30 per process (API and worker: 60 of PostgreSQL's 100). hide_parameters keeps
    # bound values (tags, sealed secrets, names) out of every SQL error message and log line.
    return create_async_engine(
        get_config().async_database_url(),
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        hide_parameters=True,
    )


def get_sessionmaker() -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(get_engine(), expire_on_commit=False)
