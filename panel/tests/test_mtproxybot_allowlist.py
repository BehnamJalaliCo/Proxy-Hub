"""The operator client's rules for pressing @MTProxybot's buttons (docs/11 section 2.1 lock 1).

A press passes only for a button of a message FROM the bot that this connection read; the bot's
removal and confirmation buttons (strict shapes) only with the connection's removal permit and under
a message naming the permitted proxy; any other button that takes something away never. Everything
here uses REAL Telethon request and type objects and the REAL operator client: the allowlist refuses
before a byte leaves. Where the client must read the chat, :func:`telegram_answers` replaces only
the library's transport step and answers with REAL Telethon TL objects as data (as in
tests/test_operator_poster_support.py); nothing imitates @MTProxybot's conversation (decision D10).
"""

from __future__ import annotations

import contextlib
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from telethon import functions, types
from test_mtproxy_units import closed_port
from test_operator_poster_support import telegram_answers

from proxyhub.mtproxy import userclient
from proxyhub.mtproxy.userclient import (
    CONFIRM_BUTTON,
    REMOVE_BUTTON,
    ApiCredentials,
    BotButton,
    BotChat,
    BotMessage,
    ClientOptions,
    Endpoint,
    MtpError,
    OperatorSession,
    ReadButtons,
    RequestNotAllowed,
    check_request,
)
from proxyhub.vault import SecretValue

BOT_ID = 93_372_553
BOT = types.InputPeerUser(user_id=BOT_ID, access_hash=7_001)
HOST = "t7aae9f0c.ipoxxo.online"
PERMIT = frozenset({HOST, f"{HOST}:8443"})
MENU = f"Proxy {HOST}:8443\nPromoted channel: @ipoxxo"
NOW = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)


def press(msg_id: int = 42, data: bytes = b"p:1", peer: Any = BOT) -> Any:
    return functions.messages.GetBotCallbackAnswerRequest(peer=peer, msg_id=msg_id, data=data)


def read(*labels: str, msg_id: int = 42, text: str = MENU, outgoing: bool = False) -> ReadButtons:
    found = ReadButtons()
    rows = (tuple(BotButton(label, f"p:{i}".encode()) for i, label in enumerate(labels, 1)),)
    found.remember(BotMessage(id=msg_id, outgoing=outgoing, text=text, edit_date=None, buttons=rows))
    return found


def allowed(request: Any, buttons: ReadButtons | None, removal: frozenset[str] = frozenset()) -> bool:
    try:
        check_request(request, bot_id=BOT_ID, buttons=buttons, removal=removal)
    except RequestNotAllowed:
        return False
    return True


# --- the rules on request objects -------------------------------------------------------------------------


def test_the_removal_shapes_are_exactly_the_documented_ones() -> None:
    """A change of these patterns must be a visible decision (docs: proxyhub/sponsor/botlist.py)."""
    assert REMOVE_BUTTON.pattern == r"(?:🗑️?\s*)?(?:delete|remove)(?:\s+proxy)?"
    assert CONFIRM_BUTTON.pattern == r"(?:✅\s*)?(?:yes|yes,?\s+(?:delete|remove)(?:\s+it)?|confirm)[.!]?"
    assert REMOVE_BUTTON.flags & re.IGNORECASE and CONFIRM_BUTTON.flags & re.IGNORECASE


def test_a_press_needs_a_button_this_connection_read_from_the_bot() -> None:
    menu = read("Set promotion", "« Back")
    assert allowed(press(42, b"p:1"), menu) and allowed(press(42, b"p:2"), menu)
    assert not allowed(press(42, b"p:1"), None)  # nothing was read on this connection
    assert not allowed(press(43, b"p:1"), menu)  # another message
    assert not allowed(press(42, b"p:9"), menu)  # data no button of that message carries
    assert not allowed(press(42, b"p:1"), read("Set promotion", outgoing=True))  # our own message
    with pytest.raises(RequestNotAllowed, match="only with @MTProxybot"):  # the peer is checked first
        check_request(press(peer=types.InputPeerSelf()), bot_id=BOT_ID, buttons=menu, removal=PERMIT)


def test_the_latest_read_of_a_message_is_what_counts() -> None:
    found = read("Set promotion")
    edited = BotMessage(
        id=42, outgoing=False, text="Edited", edit_date=NOW, buttons=((BotButton("Stats", b"s"),),)
    )
    found.remember(edited)
    assert not allowed(press(42, b"p:1"), found) and allowed(press(42, b"s"), found)
    found.remember(BotMessage(id=42, outgoing=False, text="No buttons now", edit_date=NOW, buttons=()))
    assert not allowed(press(42, b"s"), found)


def test_data_shared_by_two_labels_is_never_pressed() -> None:
    found = ReadButtons()
    row = (BotButton("Set promotion", b"x"), BotButton("Delete proxy", b"x"))
    found.remember(BotMessage(id=42, outgoing=False, text=MENU, edit_date=None, buttons=(row,)))
    assert not allowed(press(42, b"x"), found) and not allowed(press(42, b"x"), found, PERMIT)


def test_a_connection_remembers_a_bounded_number_of_messages() -> None:
    found = ReadButtons()
    for msg_id in range(1, userclient.MAX_READ_MESSAGES + 6):
        found.remember(
            BotMessage(
                id=msg_id, outgoing=False, text="x", edit_date=None, buttons=((BotButton("a", b"a"),),)
            )
        )
    assert len(found.messages) == userclient.MAX_READ_MESSAGES and 1 not in found.messages


@pytest.mark.parametrize(
    "label", ["🗑 Delete proxy", "Delete", "Remove proxy", "Yes, delete", "Yes", "Confirm"]
)
def test_a_removal_button_needs_the_permit_and_a_message_naming_that_proxy(label: str) -> None:
    menu = read(label)
    assert not allowed(press(42, b"p:1"), menu)  # no permit on this connection
    assert allowed(press(42, b"p:1"), menu, PERMIT)
    assert allowed(press(42, b"p:1"), read(label, text=f"Delete {HOST}?"), PERMIT)  # the host alone
    for text in ("Are you sure?", "Proxy 5.6.7.8:443", f"Proxy {HOST}:443", f"Proxy x{HOST}"):
        assert not allowed(press(42, b"p:1"), read(label, text=text), PERMIT), text
    assert not allowed(press(42, b"p:1"), menu, frozenset({"5.6.7.8", "5.6.7.8:443"}))  # permit for another


@pytest.mark.parametrize(
    "label",
    [
        "Delete promotion",
        "Remove promotion",
        "Delete all",
        "Delete 1.2.3.4",
        "❌ Delete",
        "Yes, I'm sure",
        "Cancel",
        "Reset",
        "Revoke",
        "Disable",
        "Clear stats",
        "Stop promotion",
        "Turn off",
        "OK",
        "Transfer",
        "Unregister",
    ],
)
def test_any_other_button_that_takes_something_away_is_never_pressed(label: str) -> None:
    assert not allowed(press(42, b"p:1"), read(label), PERMIT)
    assert not allowed(press(42, b"p:1"), read(label))


@pytest.mark.parametrize(
    "label",
    [
        "Set promotion",
        "t7aae9f0c.ipoxxo.online 4f1c9a0b",  # a /myproxies button (P13)
        "stop.example.com 4f1c",  # an address may contain any word
        "»",
        "Next",
        "« Back",
        "Stats",
    ],
)
def test_ordinary_buttons_still_pass_without_a_permit(label: str) -> None:
    assert allowed(press(42, b"p:1"), read(label))


def test_the_permit_opens_no_other_request() -> None:
    peer = types.InputPeerUser(user_id=BOT_ID, access_hash=7_001)
    menu = read("Delete proxy")
    for bad in (
        functions.messages.DeleteMessagesRequest(id=[42], revoke=True),
        functions.messages.DeleteHistoryRequest(peer=peer, max_id=0, revoke=True),
        functions.messages.ForwardMessagesRequest(from_peer=peer, id=[42], to_peer=types.InputPeerSelf()),
        functions.messages.SendMessageRequest(peer=types.InputPeerSelf(), message="/myproxies"),
        functions.contacts.BlockRequest(id=peer),
        functions.messages.GetBotCallbackAnswerRequest(peer=peer, msg_id=42, data=b"p:1", game=True),
    ):
        assert not allowed(bad, menu, PERMIT), type(bad).__name__


def test_only_the_cleanup_ever_asks_for_a_permit() -> None:
    panel = Path(__file__).resolve().parents[1] / "proxyhub"
    callers = {
        p.relative_to(panel).as_posix(): p.read_text(encoding="utf-8").count(".permit_removal(")
        for p in panel.rglob("*.py")
        if ".permit_removal(" in p.read_text(encoding="utf-8")
    }
    assert callers == {"sponsor/inventory.py": 1}


# --- the same on the real operator client -----------------------------------------------------------------


def _session(hold: contextlib.ExitStack) -> OperatorSession:
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)
    op = OperatorSession(userclient._new_client(creds, None, options), creds, options, hold)
    op._client._mtproxybot_id = BOT_ID  # what open_mtproxybot_chat sets after its checks
    return op


def _markup(*labels: str) -> types.ReplyInlineMarkup:
    return types.ReplyInlineMarkup(
        rows=[
            types.KeyboardInlineButtonRow(
                buttons=[
                    types.KeyboardInlineButton(
                        text=label, type=types.InlineButtonTypeCallback(data=f"p:{i}".encode())
                    )
                    for i, label in enumerate(labels, 1)
                ]
            )
        ]
    )


def _history() -> types.messages.Messages:
    """messages.getHistory's answer for the bot's chat: our own message, the bot's menu with
    buttons, and (never in this chat, but checked) a message of another peer with buttons."""
    me, bot = types.PeerUser(user_id=5_000_001), types.PeerUser(user_id=BOT_ID)
    return types.messages.Messages(
        messages=[
            types.Message(
                id=50,
                peer_id=bot,
                from_id=me,
                date=NOW,
                message="/myproxies",
                out=True,
                reply_markup=_markup("Mine"),
            ),
            types.Message(
                id=51,
                peer_id=bot,
                date=NOW,
                message=MENU,
                reply_markup=_markup("Set promotion", "🗑 Delete proxy"),
            ),
            types.Message(
                id=52,
                peer_id=types.PeerUser(user_id=777_000),
                date=NOW,
                message="Other",
                reply_markup=_markup("Other"),
            ),
        ],
        topics=[],
        chats=[],
        users=[],
    )


def _answers(request: Any) -> Any:
    if isinstance(request, functions.messages.GetHistoryRequest):
        return _history()
    if isinstance(request, functions.messages.GetBotCallbackAnswerRequest):
        return types.messages.BotCallbackAnswer(cache_time=0, message="OK")
    raise AssertionError(f"unexpected request {type(request).__name__}")


async def test_the_client_remembers_only_the_bots_buttons_and_presses_only_those(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with contextlib.ExitStack() as hold, telegram_answers(monkeypatch, _answers) as sent:
        op = _session(hold)
        chat = BotChat(op, BOT)
        with pytest.raises(RequestNotAllowed):
            await chat.press(51, b"p:1")  # nothing read yet
        messages = await chat.recent()
        assert [m.id for m in messages] == [50, 51]  # another peer's message is not this chat's
        assert set(op._client._read_buttons.messages) == {51}  # only the bot's own message
        assert await chat.press(51, b"p:1") == "OK"  # "Set promotion"
        for msg_id, data in ((50, b"p:1"), (52, b"p:1"), (51, b"p:3"), (51, b"p:2")):
            with pytest.raises(RequestNotAllowed):
                await chat.press(msg_id, data)  # ours, another peer's, unknown data, removal w/o permit
        chat.permit_removal(frozenset({"5.6.7.8", "5.6.7.8:443"}))
        with pytest.raises(RequestNotAllowed):
            await chat.press(51, b"p:2")  # a permit for another proxy
        chat.permit_removal(PERMIT)
        assert await chat.press(51, b"p:2") == "OK"
    pressed = [r for r in sent if isinstance(r, functions.messages.GetBotCallbackAnswerRequest)]
    assert [(r.msg_id, r.data) for r in pressed] == [(51, b"p:1"), (51, b"p:2")]


async def test_a_permitted_press_still_needs_telegram_and_fails_cleanly_without_it() -> None:
    with contextlib.ExitStack() as hold:
        op = _session(hold)
        assert op._client._read_buttons is not None
        op._client._read_buttons.remember(
            BotMessage(
                id=51, outgoing=False, text=MENU, edit_date=None, buttons=((BotButton("Delete", b"d"),),)
            )
        )
        chat = BotChat(op, BOT)
        with pytest.raises(RequestNotAllowed):
            await chat.press(51, b"d")  # refused before any connection is even tried
        chat.permit_removal(PERMIT)
        with pytest.raises(MtpError) as caught:
            await chat.press(51, b"d")  # passes the allowlist; the network is closed
    assert caught.value.code == "telegram_unavailable"


def test_every_client_starts_with_nothing_read_and_no_permit() -> None:
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    a = userclient._new_client(creds, None, ClientOptions())
    b = userclient._new_client(creds, None, ClientOptions())
    assert a._read_buttons is not None and a._read_buttons is not b._read_buttons
    assert a._read_buttons.messages == {} and a._removal == frozenset() == b._removal
