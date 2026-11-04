"""REAL-account analytics checks (opt-in; they run on the panel server, decision D10).

* marker ``hetzner`` (PROXYHUB_HETZNER_TEST_TOKEN_FILE): the live ``GET /servers`` (label selector
  ``managed-by=proxy-hub``) has the fields the offline fixtures copy, and parses; if one of our
  servers exists, its live metrics parse too. Only GET requests.
* marker ``telegram`` (PROXYHUB_TELEGRAM_TEST_TOKEN_FILE + PROXYHUB_TELEGRAM_TEST_CHANNEL): the test
  channel's member count. With PROXYHUB_TELEGRAM_TEST_CREATE_LINK=1 also one named invite link is
  created in the TEST channel (it stays there: links are never revoked).
* marker ``telegram_user`` (PROXYHUB_TELEGRAM_USER_TEST_FILE with a session, ``"stats_channel":
  "@name"`` and ``"stats_channel_id": -100...`` of a channel where the operator account is an
  admin): resolve, getFullChannel and, if
  Telegram allows it, getBroadcastStats, parsed by our code. Nothing is sent to anyone.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_telegram_support import real_bot, telegram_reachable

from proxyhub.accounts.hetzner_io import call_hetzner
from proxyhub.analytics.hetzner_usage import MANAGED_SELECTOR, read_load, read_managed_usage
from proxyhub.config import read_secret_file
from proxyhub.hetzner.client import HetznerReader
from proxyhub.mtproxy import channel_stats
from proxyhub.mtproxy.userclient import ApiCredentials, ChannelScope, operator_session
from proxyhub.vault import SecretValue


def _secret(env: str) -> str:
    path = os.environ.get(env)
    if not path:
        pytest.skip(f"{env} not set")
    return read_secret_file(Path(path))


@pytest.mark.hetzner
async def test_live_servers_and_metrics_parse() -> None:
    reader = HetznerReader(_secret("PROXYHUB_HETZNER_TEST_TOKEN_FILE"))
    raw = await call_hetzner(
        lambda: reader.raw.request("GET", "/servers", params={"label_selector": MANAGED_SELECTOR})
    )
    for server in raw["servers"]:
        assert {
            "outgoing_traffic",
            "ingoing_traffic",
            "included_traffic",
            "server_type",
            "location",
            "labels",
        } <= set(server)
        assert {"cores", "prices"} <= set(server["server_type"])
    usage = await call_hetzner(lambda: read_managed_usage(reader))
    assert len(usage) == len(raw["servers"])
    if usage:
        now = datetime.now(UTC)
        first = usage[0]
        load = await call_hetzner(
            lambda: read_load(reader, first.hetzner_id, now - timedelta(minutes=15), now, first.cores)
        )
        assert load.cpu_avg is None or 0 <= load.cpu_avg <= 100


@pytest.mark.telegram
async def test_member_count_of_the_test_channel() -> None:
    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    channel = os.environ.get("PROXYHUB_TELEGRAM_TEST_CHANNEL")
    if not channel:
        pytest.skip("PROXYHUB_TELEGRAM_TEST_CHANNEL not set")
    chat: int | str = int(channel) if channel.lstrip("-").isdigit() else channel
    async with real_bot(_secret("PROXYHUB_TELEGRAM_TEST_TOKEN_FILE")) as bot:
        assert await bot.get_chat_member_count(chat) >= 1
        if os.environ.get("PROXYHUB_TELEGRAM_TEST_CREATE_LINK") == "1":
            link = await bot.create_chat_invite_link(chat, name="proxyhub-test")
            assert link.invite_link.startswith("https://t.me/") and link.name == "proxyhub-test"


@pytest.mark.telegram_user
async def test_operator_account_reads_channel_statistics() -> None:
    data = json.loads(_secret("PROXYHUB_TELEGRAM_USER_TEST_FILE"))
    if not {"api_id", "api_hash", "session", "stats_channel"} <= set(data):
        pytest.skip("no operator session or stats_channel in the test file")
    creds = ApiCredentials(api_id=int(data["api_id"]), api_hash=SecretValue(str(data["api_hash"])))
    username = str(data["stats_channel"]).lstrip("@").lower()
    chat_id = int(data["stats_channel_id"])  # Bot API form, -100...
    channel_id = channel_stats.bare_channel_id(chat_id)
    assert channel_id is not None
    scope = ChannelScope(channel_ids=frozenset({channel_id}), usernames=frozenset({username}))
    async with operator_session(creds, SecretValue(str(data["session"])), channels=scope) as op:
        ref = await op.resolve_channel(username, channel_id)
        full = await op.channel_full(ref)
        assert isinstance(full.participants_count, int)
        if full.can_view_stats and (full.stats_dc is None or full.stats_dc == op.home_dc):
            snap = channel_stats.Snapshot(role="test", chat_id=chat_id)
            channel_stats.parse_broadcast_stats(await op.broadcast_stats(ref), snap)
            assert snap.numbers.get("followers") is not None
