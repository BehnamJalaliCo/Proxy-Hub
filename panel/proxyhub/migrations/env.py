from __future__ import annotations

import asyncio
import importlib
import pkgutil

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import proxyhub.db as db_pkg
from proxyhub.db.models import Base

# Feature teams add models in proxyhub/db/models_<team>.py; import them all so autogenerate and
# metadata see every table.
for _info in pkgutil.iter_modules(db_pkg.__path__):
    if _info.name.startswith("models_"):
        importlib.import_module(f"proxyhub.db.{_info.name}")

config = context.config
target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(config.get_main_option("sqlalchemy.url") or "", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


asyncio.run(run_migrations_online())
