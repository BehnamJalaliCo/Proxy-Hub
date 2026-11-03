"""Control loop "nodes": drives owner-requested TEST nodes through their lifecycle
(:mod:`proxyhub.nodes.executor`). The 9-slot fleet loop stays dry-run.

Unlike the other loops it does not run inside one transaction: the executor commits each state
change BEFORE the Hetzner call it leads to (crash safety), so it gets the engine. The advisory
lock still guarantees that only one worker executes at a time.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from proxyhub.nodes.executor import NodeExecutor
from proxyhub.reconcilers.base import Reconciler, lock_key


class NodesReconciler(Reconciler):
    name = "nodes"
    interval_seconds = 10.0

    def __init__(self, executor: NodeExecutor | None = None) -> None:
        self.executor = executor or NodeExecutor()

    async def run_once(self, conn: AsyncConnection) -> None:  # pragma: no cover - not used
        raise NotImplementedError("NodesReconciler manages its own transactions (try_run_once)")

    async def try_run_once(self, engine: AsyncEngine) -> bool:
        async with engine.connect() as lock_conn:
            got = (
                await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key(self.name)})
            ).scalar_one()
            if not got:
                return False
            try:
                await self.executor.run_once(engine)
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(self.name)})
                await lock_conn.commit()
        return True
