"""Real PostgreSQL / Redis only (decision D10: no fake versions). Tests marked db/redis are
skipped when PROXYHUB_DATABASE_URL / PROXYHUB_REDIS_URL are not set; CI and the panel server set them."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest

DB_URL = os.environ.get("PROXYHUB_DATABASE_URL")
REDIS_URL = os.environ.get("PROXYHUB_REDIS_URL")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if "db" in item.keywords and not DB_URL:
            item.add_marker(pytest.mark.skip(reason="PROXYHUB_DATABASE_URL not set"))
        if "redis" in item.keywords and not REDIS_URL:
            item.add_marker(pytest.mark.skip(reason="PROXYHUB_REDIS_URL not set"))
        if "hetzner" in item.keywords and not os.environ.get("PROXYHUB_HETZNER_TEST_TOKEN_FILE"):
            item.add_marker(pytest.mark.skip(reason="runs only on the panel server with a real account"))
        if "telegram" in item.keywords and not os.environ.get("PROXYHUB_TELEGRAM_TEST_TOKEN_FILE"):
            item.add_marker(pytest.mark.skip(reason="needs a real test bot (PROXYHUB_TELEGRAM_TEST_TOKEN_FILE)"))
        if "cloudflare" in item.keywords and not os.environ.get("PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE"):
            item.add_marker(pytest.mark.skip(reason="needs a real Cloudflare token (PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE)"))
        if "globalping" in item.keywords and os.environ.get("PROXYHUB_GLOBALPING_TESTS") != "1":
            item.add_marker(pytest.mark.skip(reason="real Globalping API calls (set PROXYHUB_GLOBALPING_TESTS=1)"))


@pytest.fixture(scope="session")
async def migrated_db() -> AsyncIterator[str]:
    """Fresh schema: downgrade to base, upgrade to head, on the real database."""
    from proxyhub.cli import downgrade, migrate

    assert DB_URL
    # Alembic env runs its own event loop, so it must run outside the test loop.
    await asyncio.to_thread(downgrade, "base")
    await asyncio.to_thread(migrate, "head")
    yield DB_URL
