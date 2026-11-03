"""HTTP API. For now: health and internal metrics. Dashboard routes come in M1.3/M1.6."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from proxyhub import __version__
from proxyhub.config import get_config
from proxyhub.db.session import get_engine

WORKER_STALE_SECONDS = 90


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = aioredis.from_url(get_config().redis_dsn())
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await get_engine().dispose()


app = FastAPI(title="Proxy-Hub", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/healthz")
async def healthz(response: Response) -> dict[str, Any]:
    """Liveness for the deployer: DB, Redis and worker heartbeat must all be fine."""
    result: dict[str, Any] = {"version": __version__, "commit": get_config().git_commit}
    ok = True
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
            row = (
                await conn.execute(text("SELECT updated_at FROM settings WHERE key = 'worker.heartbeat'"))
            ).first()
        result["db"] = True
        if row is None:
            result["worker"] = "no_heartbeat"
            ok = False
        else:
            age = (datetime.now(UTC) - row[0]).total_seconds()
            result["worker_heartbeat_age_s"] = round(age, 1)
            if age > WORKER_STALE_SECONDS:
                result["worker"] = "stale"
                ok = False
    except Exception as exc:
        result["db"] = False
        result["db_error"] = type(exc).__name__
        ok = False
    try:
        result["redis"] = bool(await app.state.redis.ping())
    except Exception as exc:
        result["redis"] = False
        result["redis_error"] = type(exc).__name__
        ok = False
    result["status"] = "ok" if ok else "degraded"
    if not ok:
        response.status_code = 503
    return result


@app.get("/internal/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape target. Caddy never exposes /internal/* publicly."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
