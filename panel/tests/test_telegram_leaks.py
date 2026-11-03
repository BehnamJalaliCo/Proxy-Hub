"""Rule 3 for the bot token: it is in every Bot API URL, so prove it never reaches a log line (at
DEBUG, every logger), an exception text or a traceback.

Real HTTP only: the real client against the panel's own app over ASGI (httpx logs the full request
URL exactly as it would for api.telegram.org), a real refused connection, and the real Bot API
answering a random token with 401.
"""

from __future__ import annotations

import logging
import traceback

import pytest
from httpx import ASGITransport
from test_telegram_support import (
    all_logs,
    assert_no_token,
    closed_port_bot,
    random_token,
    real_bot,
    telegram_reachable,
)

from proxyhub.telegram.client import BotClient, NetworkError, TelegramError, install_log_redaction
from proxyhub.vault import SecretValue


def everything(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc)) + repr(exc) + str(exc)


async def test_httpx_request_log_is_redacted_at_debug() -> None:
    from proxyhub.api.app import app

    token = random_token()
    with all_logs(logging.DEBUG) as logs:
        async with BotClient(token, base_url="http://panel.test", transport=ASGITransport(app=app)) as bot:
            with pytest.raises(TelegramError) as info:
                await bot.get_me()
    text = logs.getvalue()
    assert 'HTTP Request: POST http://panel.test/bot<redacted>/getMe "HTTP/1.1' in text
    assert_no_token(token, text, everything(info.value))


async def test_refused_connection_error_has_no_token() -> None:
    token = random_token()
    with all_logs(logging.DEBUG) as logs:
        async with closed_port_bot()(SecretValue(token)) as bot:
            with pytest.raises(NetworkError) as info:
                await bot.send_message(-1001234567890, "x")
    assert info.value.maybe_sent is False and info.value.kind == "ConnectError"
    # The httpx error (whose request holds the URL) is neither the cause nor shown as context.
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert_no_token(token, logs.getvalue(), everything(info.value))


async def test_real_bot_api_401_leaks_nothing() -> None:
    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    token = random_token()
    with all_logs(logging.DEBUG) as logs:
        async with real_bot(token) as bot:
            with pytest.raises(TelegramError) as info:
                await bot.get_me()
    text = logs.getvalue()
    assert "https://api.telegram.org/bot<redacted>/getMe" in text
    assert str(info.value) == "getMe: 401 Unauthorized"
    assert_no_token(token, text, everything(info.value))


def test_any_httpx_or_httpcore_record_is_redacted() -> None:
    install_log_redaction()
    token = random_token()
    bot_id, secret = token.split(":")
    with all_logs(logging.DEBUG) as logs:
        logging.getLogger("httpx").info(
            "HTTP Request: %s %s", "POST", f"https://api.telegram.org/bot{token}/x"
        )
        logging.getLogger("httpcore.http11").debug("url=%s", f"/bot{bot_id}%3A{secret}/getUpdates")
        logging.getLogger("httpcore.connection").debug("bare %s", token)
        logging.getLogger("httpx").warning("broken %s %s", "only one")  # unformattable: dropped, not printed
    text = logs.getvalue()
    assert text.count("bot<redacted>") == 2 and "bare <redacted>" in text
    assert "<unformattable log record>" in text
    assert_no_token(token, text)


async def test_secret_value_and_client_never_print_the_token() -> None:
    token = random_token()
    async with BotClient(token) as client:
        assert_no_token(token, repr(client), str(SecretValue(token)), repr(SecretValue(token)))
