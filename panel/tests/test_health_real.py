"""/healthz against real PostgreSQL and Redis."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from proxyhub.reconcilers.base import Heartbeat

pytestmark = [pytest.mark.db, pytest.mark.redis]


async def test_healthz_reports_worker_heartbeat(migrated_db: str) -> None:
    from proxyhub.api.app import app, lifespan

    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            engine = create_async_engine(migrated_db)
            async with engine.begin() as conn:
                await conn.exec_driver_sql("DELETE FROM settings WHERE key = 'worker.heartbeat'")
            await engine.dispose()
            r = await client.get("/healthz")
            assert r.status_code == 503 and r.json()["worker"] == "no_heartbeat"

            engine = create_async_engine(migrated_db)
            assert await Heartbeat().try_run_once(engine)
            await engine.dispose()
            r = await client.get("/healthz")
            body = r.json()
            assert r.status_code == 200, body
            assert body["db"] is True and body["redis"] is True and body["status"] == "ok"
