"""Real PostgreSQL / Redis only (decision D10: no fake versions). Tests marked db/redis are
skipped when PROXYHUB_DATABASE_URL / PROXYHUB_REDIS_URL are not set; CI and the panel server set them."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterator

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
            item.add_marker(
                pytest.mark.skip(reason="needs a real test bot (PROXYHUB_TELEGRAM_TEST_TOKEN_FILE)")
            )
        if "telegram_user" in item.keywords and not os.environ.get("PROXYHUB_TELEGRAM_USER_TEST_FILE"):
            item.add_marker(
                pytest.mark.skip(
                    reason="real MTProto as the operator account (PROXYHUB_TELEGRAM_USER_TEST_FILE)"
                )
            )
        if "cloudflare" in item.keywords and not os.environ.get("PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE"):
            item.add_marker(
                pytest.mark.skip(reason="needs a real Cloudflare token (PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE)")
            )
        if "claude" in item.keywords and os.environ.get("PROXYHUB_CLAUDE_BRIDGE_TESTS") != "1":
            item.add_marker(
                pytest.mark.skip(reason="real Claude Code, logged in (set PROXYHUB_CLAUDE_BRIDGE_TESTS=1)")
            )
        if "anthropic_api" in item.keywords and not os.environ.get("PROXYHUB_ANTHROPIC_TEST_KEY_FILE"):
            item.add_marker(
                pytest.mark.skip(reason="needs a real Anthropic API key (PROXYHUB_ANTHROPIC_TEST_KEY_FILE)")
            )
        if "globalping" in item.keywords and os.environ.get("PROXYHUB_GLOBALPING_TESTS") != "1":
            item.add_marker(
                pytest.mark.skip(reason="real Globalping API calls (set PROXYHUB_GLOBALPING_TESTS=1)")
            )


# A test that talks to the real api.telegram.org starts with `if not telegram_reachable(): skip`.
# When Telegram stops answering DURING such a test (the client's NetworkError: no HTTP answer at
# all, e.g. a ReadTimeout), the test could not exercise Telegram's behaviour either: it is the same
# "Telegram unreachable" case, so it is reported as skipped with the reason, never as passed. Only
# for those tests and only for that error; any other failure stays a failure. (CI on 2026-11-06:
# twice, in two different real-Telegram tests, api.telegram.org timed out from the runner.)
_TELEGRAM_UNANSWERED = ("network error (ReadTimeout", "network error (ConnectTimeout",
                        "network error (ConnectError", "network error (RemoteProtocolError",
                        "network error (ReadError", "network error (PoolTimeout")  # fmt: skip


def _talks_to_real_telegram(item: pytest.Item) -> bool:
    import inspect

    function = getattr(item, "function", None)
    try:
        return function is not None and "telegram_reachable()" in inspect.getsource(function)
    except (OSError, TypeError):
        return False


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> object:
    report = yield
    if (
        isinstance(report, pytest.TestReport)
        and report.when == "call"
        and report.failed
        and _talks_to_real_telegram(item)
    ):
        text = report.longreprtext + "\n" + report.caplog
        if any(marker in text for marker in _TELEGRAM_UNANSWERED):
            report.outcome = "skipped"
            report.longrepr = (
                str(item.path),
                item.location[1] or 0,
                "Skipped: api.telegram.org did not answer during the test (network)",
            )
    return report


@pytest.fixture(autouse=True)
def _http_library_logs_open() -> Iterator[None]:
    """proxyhub.cli quiets httpx to WARNING in production, and a test that runs the CLI changes
    that process-wide. Leak tests must see every line the HTTP libraries can print, so each test
    starts with those loggers inheriting the root level again."""
    libs = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    old = [lib.level for lib in libs]
    for lib in libs:
        lib.setLevel(logging.NOTSET)
    yield
    for lib, level in zip(libs, old, strict=True):
        lib.setLevel(level)


@pytest.fixture(scope="session")
async def migrated_db() -> AsyncIterator[str]:
    """Fresh schema: downgrade to base, upgrade to head, on the real database."""
    from proxyhub.cli import downgrade, migrate

    assert DB_URL
    # Alembic env runs its own event loop, so it must run outside the test loop.
    await asyncio.to_thread(downgrade, "base")
    await asyncio.to_thread(migrate, "head")
    yield DB_URL
