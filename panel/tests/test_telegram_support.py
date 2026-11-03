"""Shared helpers for the Telegram team's tests (no imitation of Telegram anywhere, decision D10).

* Token-shaped values are generated at run time (never a literal in the repo) and are NOT real
  tokens: sent to the real Bot API they get Telegram's real ``401 Unauthorized``.
* ``BOT_API_ERRORS`` holds the real error JSON shapes of the Bot API (core.telegram.org/bots/api,
  "Making requests" and ResponseParameters); the 401 and 404 bodies were also received from
  api.telegram.org on 2026-11-03. They are DATA for the parser, never served by a fake server.
* ``closed_port_bot`` points the real client at a local port nobody listens on: a real
  connection refusal, used to exercise our network-failure handling.
* ``SilentServer`` is a real local TCP server that reads a request and never answers anything (a
  hung endpoint), used to exercise deadlines and cancellation. It sends nothing back, so it does
  not imitate Telegram.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import secrets
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from io import StringIO
from types import TracebackType
from typing import Any

import httpx
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.telegram.client import BotClient
from proxyhub.vault import SecretValue

TELEGRAM_TABLES_SQL = (
    "DELETE FROM channel_posts",
    "DELETE FROM telegram_pairings",
    "DELETE FROM telegram_pacing",
    "DELETE FROM outbox",
    "DELETE FROM tg_updates",
    "DELETE FROM settings WHERE key LIKE 'telegram.%'",
    "DELETE FROM secrets WHERE name LIKE 'telegram:%'",
    "DELETE FROM actions_log WHERE action LIKE 'telegram.%'",
)

BOT_API_ERRORS: dict[str, dict[str, Any]] = {
    "unauthorized": {"ok": False, "error_code": 401, "description": "Unauthorized"},
    "not_found": {"ok": False, "error_code": 404, "description": "Not Found"},
    "flood": {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 35",
        "parameters": {"retry_after": 35},
    },
    "chat_not_found": {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
    "not_modified": {
        "ok": False,
        "error_code": 400,
        "description": (
            "Bad Request: message is not modified: specified new message content and reply markup are "
            "exactly the same as a current content and reply markup of the message"
        ),
    },
    "kicked": {
        "ok": False,
        "error_code": 403,
        "description": "Forbidden: bot is not a member of the channel chat",
    },
    "conflict": {
        "ok": False,
        "error_code": 409,
        "description": (
            "Conflict: terminated by other getUpdates request; "
            "make sure that only one bot instance is running"
        ),
    },
    "migrated": {
        "ok": False,
        "error_code": 400,
        "description": "Bad Request: group chat was upgraded to a supergroup chat",
        "parameters": {"migrate_to_chat_id": -1001234567890},
    },
    "server": {"ok": False, "error_code": 502, "description": "Bad Gateway"},
}


def random_token() -> str:
    """Token-SHAPED, random, not a real bot token."""
    return f"{1_000_000_000 + secrets.randbelow(10**9)}:{secrets.token_urlsafe(32)}"


def free_closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def closed_port_bot() -> Any:
    port = free_closed_port()

    def make(token: SecretValue) -> BotClient:
        return BotClient(token, base_url=f"http://127.0.0.1:{port}")

    return make


class SilentServer:
    """A real TCP server on 127.0.0.1 that accepts, reads and never answers (a hung endpoint)."""

    def __init__(self) -> None:
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> SilentServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    def bot(self) -> Callable[[SecretValue], BotClient]:
        port = self.port

        def make(token: SecretValue) -> BotClient:
            return BotClient(token, base_url=f"http://127.0.0.1:{port}")

        return make

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            while await reader.read(65536):  # read and drop the request; never write an answer
                pass
        except ConnectionError:
            pass
        finally:
            writer.close()


def real_bot(token: SecretValue | str) -> BotClient:
    """The real client for the real Bot API, with patient timeouts (this sandbox's proxy is slow)."""
    return BotClient(token, timeout=httpx.Timeout(45.0, connect=20.0))


@functools.cache
def telegram_reachable() -> bool:
    """True if api.telegram.org answers a random token with its real 401 (network available)."""
    try:
        response = httpx.post(f"https://api.telegram.org/bot{random_token()}/getMe", timeout=15)
    except httpx.HTTPError:
        return False
    return response.status_code == 401


async def clean_telegram_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for sql in TELEGRAM_TABLES_SQL:
            await conn.execute(text(sql))


@contextmanager
def all_logs(level: int = logging.DEBUG) -> Iterator[StringIO]:
    """Every log line of every logger at ``level`` (formatted with arguments), into a buffer."""
    buffer = StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def assert_no_token(token: str, *texts: str) -> None:
    secret_half = token.split(":", 1)[1]
    for value in texts:
        assert token not in value, "the bot token leaked"
        assert secret_half not in value, "the secret half of the bot token leaked"


class LiveApi:
    """The real panel app on a real port, startable and stoppable."""

    def __init__(self) -> None:
        self.port = free_closed_port()
        self.server: uvicorn.Server | None = None
        self.task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/livez"

    async def start(self) -> None:
        from proxyhub.api.app import app

        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, lifespan="off", log_level="warning")
        self.server = uvicorn.Server(config)
        self.task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    async def stop(self) -> None:
        if self.server is not None and self.task is not None:
            self.server.should_exit = True
            await self.task
        self.server = self.task = None
