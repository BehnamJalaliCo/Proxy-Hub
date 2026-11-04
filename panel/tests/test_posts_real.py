"""REAL bot test (marker ``telegram``) for the publisher's layout: run on the panel server.

Same environment as tests/test_telegram_real.py (PROXYHUB_TELEGRAM_TEST_TOKEN_FILE and
PROXYHUB_TELEGRAM_TEST_CHANNEL). It sends one post in the publisher's full layout (bold title,
blockquote, MTProto link words, a Reality config in <code>, the channel tag) to the private TEST
channel, then edits it to a paused/ended state and to the all-dead state, proving Telegram accepts
every HTML text. The links
point at documentation addresses (RFC 5737, example.com) and a random secret, so nobody can use
them. Nothing is ever removed (rule 6).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_telegram_support import all_logs, assert_no_token

from proxyhub.config import read_secret_file
from proxyhub.telegram.client import BotClient
from proxyhub.telegram.templates import PostLink, compose_content, mtproto_url, render_post, vless_url

pytestmark = pytest.mark.telegram


def _channel() -> str | int:
    value = os.environ.get("PROXYHUB_TELEGRAM_TEST_CHANNEL")
    if not value:
        pytest.skip("PROXYHUB_TELEGRAM_TEST_CHANNEL not set")
    return int(value) if value.lstrip("-").isdigit() else value


async def test_publisher_layout_is_accepted_and_edited_by_telegram() -> None:
    token = read_secret_file(Path(os.environ.get("PROXYHUB_TELEGRAM_TEST_TOKEN_FILE", "")))
    channel = _channel()
    content = compose_content(
        "🧪 آزمایش قالب پست (Proxy-Hub)",
        "این پست را آزمون خودکار پنل در کانال آزمایشی گذاشته است.\n<b>پروکسی‌ها ساختگی‌اند</b> و کار نمیکنند.",
    )
    sec = secrets.token_hex(16)
    live = [
        PostLink(
            kind="mtproto",
            label=f"پروکسی {i}",
            status="active",
            url=mtproto_url(f"https://t.me/proxy?server=192.0.2.{i}&port=8443&secret={sec}"),
        )
        for i in (1, 2, 3)
    ] + [
        PostLink(
            kind="reality",
            label="ریلیتی ۱",
            status="active",
            url=vless_url(
                "vless://00000000-0000-4000-8000-000000000000@192.0.2.1:443"
                "?security=reality&sni=www.example.com&type=tcp#proxyhub-test"
            ),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
    ]
    mixed = [
        PostLink(kind=x.kind, label=x.label, status="paused" if i % 2 else "expired")
        for i, x in enumerate(live)
    ]
    dead = [PostLink(kind=x.kind, label=x.label, status="burned") for x in live]
    with all_logs(logging.DEBUG) as logs:
        async with BotClient(token) as bot:
            chat = await bot.get_chat(channel)
            sent = await bot.send_message(chat.id, render_post(content, live, "@ipoxxo").text)
            middle = await bot.edit_message_text(
                chat.id, sent.message_id, render_post(content, mixed, "@ipoxxo").text
            )
            edited = await bot.edit_message_text(
                chat.id, sent.message_id, render_post(content, dead, "@ipoxxo").text
            )
    assert middle.message_id == edited.message_id == sent.message_id
    assert_no_token(token, logs.getvalue())
    assert sec not in logs.getvalue()
