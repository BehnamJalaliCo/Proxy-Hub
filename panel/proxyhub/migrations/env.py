from __future__ import annotations

import asyncio
import importlib
import logging
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
log = logging.getLogger("proxyhub.migrations")


def _unknown_heads() -> list[str]:
    """Revisions the database is on that this code has no script for: a newer commit migrated it
    and the deployer then rolled the code back to this one."""
    known = {script.revision for script in context.script.walk_revisions()}
    return [head for head in context.get_context().get_current_heads() if head not in known]


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        # Only ``proxyhub migrate`` sets the flag: downgrade and check still fail on such a schema.
        newer = _unknown_heads() if config.attributes.get("tolerate_newer_schema") else []
        if newer:
            # Migrations stay backward compatible (expand/contract), so this code runs on that schema.
            log.warning(
                "database schema is newer than this code (revision %s); "
                "rollback: leaving the schema as it is",
                ", ".join(newer),
            )
            return
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(config.get_main_option("sqlalchemy.url") or "", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


asyncio.run(run_migrations_online())
