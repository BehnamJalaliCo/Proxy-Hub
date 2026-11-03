"""/internal/metrics panel gauges (M1.7) against real PostgreSQL and Redis.

The alert rules in deploy/monitoring/prometheus/rules/ are built on these names;
test_ops_deploy_files.py keeps the rules and this code in step.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

pytestmark = [pytest.mark.db, pytest.mark.redis]


def _samples(body: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for family in text_string_to_metric_families(body):
        for s in family.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def _one(samples: dict[tuple[str, tuple[tuple[str, str], ...]], float], name: str, **labels: str) -> float:
    return samples[(name, tuple(sorted(labels.items())))]


@pytest.fixture
async def client(migrated_db: str) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    yield eng
    await eng.dispose()


async def _scrape(client: AsyncClient) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    r = await client.get("/internal/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    return _samples(r.text)


async def test_worker_heartbeat_age(client: AsyncClient, engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM settings WHERE key = 'worker.heartbeat'")
    m = await _scrape(client)
    assert _one(m, "proxyhub_db_up") == 1
    assert _one(m, "proxyhub_redis_up") == 1
    assert math.isinf(_one(m, "proxyhub_worker_heartbeat_age_seconds")), "no heartbeat must read as +Inf"

    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO settings (key, value, updated_at) VALUES "
            "('worker.heartbeat', '{}'::jsonb, now() - interval '200 seconds')"
        )
    age = _one(await _scrape(client), "proxyhub_worker_heartbeat_age_seconds")
    assert 199 <= age < 260, age

    from proxyhub.reconcilers.base import Heartbeat

    assert await Heartbeat().try_run_once(engine)
    age = _one(await _scrape(client), "proxyhub_worker_heartbeat_age_seconds")
    assert 0 <= age < 10, age


async def test_fleet_and_outbox_gauges(client: AsyncClient, engine: AsyncEngine) -> None:
    from proxyhub.db.models import ACCOUNT_STATES
    from proxyhub.fleet.lifecycle import NodeState

    before = await _scrape(client)
    for state in NodeState:  # every state is exported, also with zero nodes
        assert ("proxyhub_nodes", (("state", state.value),)) in before
    for state in ACCOUNT_STATES:
        assert ("proxyhub_hetzner_accounts", (("state", state),)) in before

    account_id = uuid.uuid4()
    tag = f"metrics-test-{account_id.hex[:8]}"
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO hetzner_accounts (id, name, token_secret_name, state) "
            "VALUES ($1, $2, 'not-a-real-secret-file', 'active')",
            (account_id, tag),
        )
        for i, state in enumerate(["ready", "ready", "burned"]):
            await conn.exec_driver_sql(
                "INSERT INTO nodes (id, account_id, name, location, server_type, state, is_test) "
                "VALUES ($1, $2, $3, 'fsn1', 'cpx02', $4, true)",
                (uuid.uuid4(), account_id, f"{tag}-{i}", state),
            )
        await conn.exec_driver_sql(
            "INSERT INTO outbox (kind, payload, not_before, attempts) VALUES "
            "('alert', '{\"test\": true}'::jsonb, now() - interval '120 seconds', 0), "
            "('alert', '{\"test\": true}'::jsonb, now() + interval '1 hour', 0)"
        )
    try:
        after = await _scrape(client)

        def delta(name: str, **labels: str) -> float:
            return _one(after, name, **labels) - _one(before, name, **labels)

        assert delta("proxyhub_nodes", state="ready") == 2
        assert delta("proxyhub_nodes", state="burned") == 1
        assert delta("proxyhub_nodes", state="creating") == 0
        assert delta("proxyhub_hetzner_accounts", state="active") == 1
        assert delta("proxyhub_outbox_pending_jobs") == 2
        # Only the due job counts for the waiting age; the job scheduled in an hour does not.
        assert _one(after, "proxyhub_outbox_oldest_due_age_seconds") >= 119
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("DELETE FROM outbox WHERE payload = '{\"test\": true}'::jsonb")
            await conn.exec_driver_sql("DELETE FROM nodes WHERE account_id = $1", (account_id,))
            await conn.exec_driver_sql("DELETE FROM hetzner_accounts WHERE id = $1", (account_id,))


async def test_build_info_and_no_secret_in_output(client: AsyncClient) -> None:
    from proxyhub import __version__
    from proxyhub.config import get_config

    r = await client.get("/internal/metrics")
    m = _samples(r.text)
    assert _one(m, "proxyhub_build_info", version=__version__, commit=get_config().git_commit) == 1
    # Rule 3: the database password in the URL never reaches the scrape output.
    password = get_config().async_database_url().split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    assert password and password not in r.text


async def test_unreachable_database_and_redis_read_as_down(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real refused connections (nothing listens on port 1): gauges say 'down' and 'unknown'."""
    import proxyhub.api.app as app_module

    dead_engine = create_async_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    dead_redis = aioredis.from_url("redis://127.0.0.1:1/0")
    live_redis = app_module.app.state.redis
    monkeypatch.setattr(app_module, "get_engine", lambda: dead_engine)
    app_module.app.state.redis = dead_redis
    try:
        r = await client.get("/internal/metrics")
        assert r.status_code == 200, "the scrape itself must still succeed"
        m = _samples(r.text)
        assert _one(m, "proxyhub_db_up") == 0
        assert _one(m, "proxyhub_redis_up") == 0
        assert math.isnan(_one(m, "proxyhub_worker_heartbeat_age_seconds"))
        assert not any(name == "proxyhub_nodes" for name, _ in m), "stale node counts must disappear"
        assert _one(m, "proxyhub_metrics_collect_errors_total", part="db") >= 1
        assert _one(m, "proxyhub_metrics_collect_errors_total", part="redis") >= 1
    finally:
        app_module.app.state.redis = live_redis
        monkeypatch.undo()
        await dead_engine.dispose()
        await dead_redis.aclose()
    m = await _scrape(client)
    assert _one(m, "proxyhub_db_up") == 1 and _one(m, "proxyhub_redis_up") == 1
