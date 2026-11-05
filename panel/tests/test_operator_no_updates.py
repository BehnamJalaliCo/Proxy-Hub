"""The operator client never asks Telegram for updates (decision D37).

P11 on the real panel server: after the first message to @MTProxybot the second one failed with
``telegram_unavailable``. Telethon's own update loop tracks pts, sees a gap (the bot's replies are
not delivered to a client without updates, so the pts of the second sent message jumps), asks
``updates.getDifference``, which reads every chat, and our allowlist refuses it. That exception ended
the update loop, which disconnected the client in the middle of the conversation.

These tests run the real Telethon update loop of the real client class. No network is involved: the
loop's input is the queue the library fills from what Telegram answers.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from telethon import functions, types
from telethon.sessions import StringSession

from proxyhub.mtproxy.userclient import RequestNotAllowed, _OperatorClient, check_request


def _client() -> _OperatorClient:
    return _OperatorClient(StringSession(), 1234567, "0" * 32, receive_updates=False, catch_up=False)


def _sent(pts: int, message_id: int) -> types.UpdateShortSentMessage:
    # What messages.sendMessage answers for a private chat: one new message, pts_count 1.
    return types.UpdateShortSentMessage(
        id=message_id, pts=pts, pts_count=1, date=datetime(2026, 11, 5, 0, 30, tzinfo=UTC), out=True
    )


async def _run_loop(client: _OperatorClient, *updates: Any) -> list[Any]:
    """Run the library's update loop on this client while ``updates`` arrive; return every request
    the client tried to send."""
    sent: list[Any] = []

    me = types.User(id=777000123, is_self=True, access_hash=42, first_name="support")

    async def fake_call(sender: Any, request: Any, *a: Any, **kw: Any) -> Any:
        sent.append(request)
        check_request(request, client.scope, bot_id=client._mtproxybot_id)  # the real allowlist
        if isinstance(request, functions.users.GetUsersRequest):
            return [me]  # what Telegram answers for InputUserSelf
        raise RuntimeError("nothing else answers in this test")

    client.is_connected = lambda: True  # type: ignore[method-assign]
    client._call = fake_call  # type: ignore[method-assign]
    client._mb_entity_cache.set_self_user(me.id, False, me.access_hash)  # as _on_login leaves it
    client._authorized = True
    loop = asyncio.create_task(client._update_loop())
    try:
        for u in updates:
            await client._updates_queue.put(u)
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.5)  # past Telethon's possible-gap timeout (0.5 s)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    assert client._updates_error is None, "the loop died; Telethon would disconnect the client"
    return sent


async def test_a_pts_gap_does_not_make_the_client_ask_for_the_difference() -> None:
    client = _client()
    # First message: pts 100. The bot answers three times (not delivered to us), then our second
    # message carries pts 104 with pts_count 1: a gap of three.
    sent = await _run_loop(client, _sent(100, 1), _sent(104, 5))
    assert sent == []  # not getDifference, and nothing else either


async def test_the_allowlist_still_refuses_every_difference_request() -> None:
    # Reading the difference would read every chat of the account: the fix is that nobody asks,
    # never that the allowlist lets it through. (updates.getState stays allowed: it returns counters.)
    for request in (
        functions.updates.GetDifferenceRequest(pts=1, date=datetime(2026, 11, 5, tzinfo=UTC), qts=0),
        functions.updates.GetChannelDifferenceRequest(
            channel=types.InputChannelEmpty(), filter=types.ChannelMessagesFilterEmpty(), pts=1, limit=10
        ),
    ):
        with pytest.raises(RequestNotAllowed):
            check_request(request)


async def test_the_loop_ends_when_the_client_disconnects() -> None:
    client = _client()
    state = {"up": True}
    client.is_connected = lambda: state["up"]  # type: ignore[method-assign]
    loop = asyncio.create_task(client._update_loop())
    await asyncio.sleep(0.05)
    assert not loop.done()  # waiting for updates it will throw away
    loop.cancel()  # what Telethon's disconnect does
    await asyncio.gather(loop, return_exceptions=True)
    assert loop.done()
