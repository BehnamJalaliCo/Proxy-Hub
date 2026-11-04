"""HTTP API. For now: health and internal metrics. Dashboard routes come in M1.3/M1.6."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from sqlalchemy import text

from proxyhub import __version__
from proxyhub.api.deps import install_error_handlers
from proxyhub.api.registry import include_all
from proxyhub.config import get_config
from proxyhub.db.models import ACCOUNT_STATES
from proxyhub.db.session import get_engine
from proxyhub.fleet.lifecycle import NodeState

log = logging.getLogger(__name__)

# deploy/monitoring/prometheus/rules/panel.yml alerts on the same threshold (a test keeps them equal).
WORKER_STALE_SECONDS = 90
METRICS_TIMEOUT_SECONDS = 3.0

# --- Panel gauges for Prometheus (M1.7). Aggregates only: no IP or per-user value (rule 7). ------
BUILD_INFO = Gauge(
    "proxyhub_build_info", "Running panel version and git commit (always 1).", ["version", "commit"]
)
DB_UP = Gauge("proxyhub_db_up", "1 if PostgreSQL answered during this scrape, else 0.")
REDIS_UP = Gauge("proxyhub_redis_up", "1 if Redis answered PING during this scrape, else 0.")
WORKER_HEARTBEAT_AGE = Gauge(
    "proxyhub_worker_heartbeat_age_seconds",
    "Seconds since the worker last wrote its heartbeat (+Inf: never written; NaN: database unreachable).",
)
NODES = Gauge("proxyhub_nodes", "Nodes by lifecycle state (deleted included).", ["state"])
HETZNER_ACCOUNTS = Gauge("proxyhub_hetzner_accounts", "Hetzner accounts by state.", ["state"])
OUTBOX_PENDING = Gauge("proxyhub_outbox_pending_jobs", "Telegram outbox jobs that are not done yet.")
OUTBOX_OLDEST_DUE_AGE = Gauge(
    "proxyhub_outbox_oldest_due_age_seconds",
    "Seconds the oldest due, unfinished outbox job has been waiting (0 if none).",
)
METRICS_ERRORS = Counter(
    "proxyhub_metrics_collect_errors", "Failures while collecting panel metrics, by part.", ["part"]
)
_metrics_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = aioredis.from_url(get_config().redis_dsn())
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await get_engine().dispose()


app = FastAPI(
    title="Proxy-Hub",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,  # the API schema is not published
)
install_error_handlers(app)


@app.get("/livez", include_in_schema=False)
async def livez() -> dict[str, str]:
    """Process liveness only (Docker healthcheck). Deploy health is /healthz."""
    return {"status": "alive"}


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


async def _collect_db_metrics() -> None:
    async with asyncio.timeout(METRICS_TIMEOUT_SECONDS), get_engine().connect() as conn:
        await conn.execute(text("SET LOCAL statement_timeout = '2s'"))
        heartbeat_age = (
            await conn.execute(
                text(
                    "SELECT GREATEST(EXTRACT(EPOCH FROM (now() - updated_at)), 0) "
                    "FROM settings WHERE key = 'worker.heartbeat'"
                )
            )
        ).scalar_one_or_none()
        nodes = (await conn.execute(text("SELECT state, count(*) FROM nodes GROUP BY state"))).all()
        accounts = (
            await conn.execute(text("SELECT state, count(*) FROM hetzner_accounts GROUP BY state"))
        ).all()
        pending, oldest_due = (
            await conn.execute(
                text(
                    "SELECT count(*), COALESCE(EXTRACT(EPOCH FROM (now() - min(not_before) "
                    "FILTER (WHERE not_before <= now()))), 0) FROM outbox WHERE done_at IS NULL"
                )
            )
        ).one()
    DB_UP.set(1)
    WORKER_HEARTBEAT_AGE.set(math.inf if heartbeat_age is None else float(heartbeat_age))
    node_counts = {state.value: 0 for state in NodeState} | {str(s): int(n) for s, n in nodes}
    for state, count in node_counts.items():
        NODES.labels(state=state).set(count)
    account_counts = dict.fromkeys(ACCOUNT_STATES, 0) | {str(s): int(n) for s, n in accounts}
    for state, count in account_counts.items():
        HETZNER_ACCOUNTS.labels(state=state).set(count)
    OUTBOX_PENDING.set(int(pending))
    OUTBOX_OLDEST_DUE_AGE.set(float(oldest_due))


def _mark_db_down() -> None:
    """Unknown, not zero: stale numbers must not look healthy while the database is unreachable."""
    DB_UP.set(0)
    WORKER_HEARTBEAT_AGE.set(math.nan)
    NODES.clear()
    HETZNER_ACCOUNTS.clear()
    OUTBOX_PENDING.set(math.nan)
    OUTBOX_OLDEST_DUE_AGE.set(math.nan)


async def collect_panel_metrics(redis: aioredis.Redis | None) -> None:
    """Refresh the panel gauges. Each part fails on its own; errors are counted, never raised."""
    cfg = get_config()
    BUILD_INFO.clear()
    BUILD_INFO.labels(version=__version__, commit=cfg.git_commit).set(1)
    try:
        await _collect_db_metrics()
    except Exception as exc:
        METRICS_ERRORS.labels(part="db").inc()
        log.warning("metrics: database part failed (%s)", type(exc).__name__)
        _mark_db_down()
    try:
        if redis is None:  # lifespan has not run (e.g. an app mounted without it)
            raise ConnectionError("no redis client")
        async with asyncio.timeout(METRICS_TIMEOUT_SECONDS):
            REDIS_UP.set(1 if await redis.ping() else 0)
    except Exception as exc:
        METRICS_ERRORS.labels(part="redis").inc()
        log.warning("metrics: redis part failed (%s)", type(exc).__name__)
        REDIS_UP.set(0)


@app.get("/internal/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape target. Caddy never exposes /internal/* publicly."""
    async with _metrics_lock:
        await collect_panel_metrics(getattr(app.state, "redis", None))
        body = generate_latest()
    return Response(body, media_type=CONTENT_TYPE_LATEST)


# Feature routers (proxyhub/api/routes_*.py) are discovered automatically; keep this last.
include_all(app)
