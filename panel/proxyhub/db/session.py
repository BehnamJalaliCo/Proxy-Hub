from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from proxyhub.config import get_config


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return create_async_engine(get_config().async_database_url(), pool_pre_ping=True, pool_size=5)


def get_sessionmaker() -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(get_engine(), expire_on_commit=False)
