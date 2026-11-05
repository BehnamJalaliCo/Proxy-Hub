"""The operator account may talk to @MTProxybot and nobody else (docs/11 section 2.1 lock 1, D37).

Everything here uses REAL Telethon request and type objects and a real, unconnected (or locally
connected, never answered) Telethon client: the allowlist refuses before a byte leaves, and nothing
imitates Telegram or @MTProxybot (decision D10).
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from telethon import functions, types
from test_mtproxy_support import session_string
from test_mtproxy_units import closed_port
from test_telegram_support import all_logs

from proxyhub.mtproxy import userclient
from proxyhub.mtproxy.userclient import (
    LIVE_SECRETS,
    MAX_CALLBACK_DATA,
    MAX_HISTORY_LIMIT,
    MAX_SENT_TEXT,
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
    operator_session,
)
from proxyhub.vault import SecretValue

BOT_ID = 93_372_553  # any id: what matters is that it equals the one stored at login
BOT = types.InputPeerUser(user_id=BOT_ID, access_hash=7_001)


def _history(peer: object = BOT, limit: int = 5) -> object:
    return functions.messages.GetHistoryRequest(
        peer=peer, offset_id=0, offset_date=None, add_offset=0, limit=limit, max_id=0, min_id=0, hash=0
    )


def _send(peer: object = BOT, message: str = "/myproxies", **kw: object) -> object:
    return functions.messages.SendMessageRequest(peer=peer, message=message, **kw)


def _press(peer: object = BOT, data: bytes | None = b"\x01promo", **kw: object) -> object:
    return functions.messages.GetBotCallbackAnswerRequest(peer=peer, msg_id=42, data=data, **kw)


# --- the allowlist on request objects -------------------------------------------------------------------


def _read(*labels: tuple[str, bytes], msg_id: int = 42, text: str = "Here is the list") -> ReadButtons:
    """The bot's buttons as a connection read them (what BotChat.recent remembers)."""
    read = ReadButtons()
    buttons = (tuple(BotButton(label, data) for label, data in labels),)
    read.remember(BotMessage(id=msg_id, outgoing=False, text=text, edit_date=None, buttons=buttons))
    return read


READ = _read(("Set promotion", b"\x01promo"))


@pytest.mark.parametrize("request_", [_send(), _history(), _press(), _send(no_webpage=True)])
def test_the_three_chat_requests_pass_only_in_the_bots_open_chat(request_: object) -> None:
    check_request(request_, bot_id=BOT_ID, buttons=READ)
    with pytest.raises(RequestNotAllowed, match="only in the chat with @MTProxybot"):
        check_request(request_, buttons=READ)  # the chat was not opened on this connection
    with pytest.raises(RequestNotAllowed, match="only with @MTProxybot"):
        check_request(request_, bot_id=BOT_ID + 1, buttons=READ)  # the stored bot id is another user


OTHER_PEERS = [
    types.InputPeerSelf(),  # Saved Messages
    types.InputPeerUser(user_id=777_000, access_hash=1),  # Telegram's service account
    types.InputPeerUser(user_id=BOT_ID + 1, access_hash=7_001),
    types.InputPeerChat(chat_id=BOT_ID),  # a group with the same number
    types.InputPeerChannel(channel_id=BOT_ID, access_hash=7_001),  # a channel with the same number
    types.InputPeerUserFromMessage(peer=types.InputPeerSelf(), msg_id=1, user_id=BOT_ID),
    types.InputPeerEmpty(),
    "MTProxybot",  # a username the library would resolve later: refused before that
    BOT_ID,
]


@pytest.mark.parametrize("peer", OTHER_PEERS, ids=lambda p: type(p).__name__)
def test_any_other_peer_is_refused_for_every_chat_request(peer: object) -> None:
    for request_ in (_send(peer), _history(peer), _press(peer)):
        with pytest.raises(RequestNotAllowed, match="only with @MTProxybot"):
            check_request(request_, bot_id=BOT_ID)


@pytest.mark.parametrize(
    "request_",
    [
        _send(message=""),
        _send(message="x" * (MAX_SENT_TEXT + 1)),
        _send(reply_markup=types.ReplyKeyboardHide()),
        _send(entities=[types.MessageEntityBold(offset=0, length=1)]),
        _send(schedule_date=datetime(2030, 1, 1, tzinfo=UTC)),
        _send(send_as=types.InputPeerSelf()),
        _send(silent=True),
        _send(allow_paid_stars=1),
        _send(reply_to=types.InputReplyToMessage(reply_to_msg_id=1)),
        _history(limit=0),
        _history(limit=MAX_HISTORY_LIMIT + 1),
        _history(limit=100),
        _press(data=None),
        _press(data=b""),
        _press(data=b"x" * (MAX_CALLBACK_DATA + 1)),
        _press(game=True),
        _press(password=types.InputCheckPasswordEmpty()),
    ],
)
def test_the_bots_peer_does_not_carry_anything_else_along(request_: object) -> None:
    with pytest.raises(RequestNotAllowed):
        check_request(request_, bot_id=BOT_ID)


@pytest.mark.parametrize(
    "request_",
    [
        functions.messages.GetDialogsRequest(
            offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=10, hash=0
        ),
        functions.messages.ReadHistoryRequest(peer=BOT, max_id=0),
        functions.messages.ForwardMessagesRequest(from_peer=BOT, id=[1], to_peer=types.InputPeerSelf()),
        functions.messages.SearchGlobalRequest(
            q="proxy",
            filter=types.InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=types.InputPeerEmpty(),
            offset_id=0,
            limit=5,
        ),
        functions.messages.StartBotRequest(bot=types.InputUser(BOT_ID, 7_001), peer=BOT, start_param="x"),
        functions.channels.JoinChannelRequest(channel=types.InputChannel(BOT_ID, 7_001)),
        functions.contacts.ResolveUsernameRequest(username="ipoxxo"),
        functions.updates.GetDifferenceRequest(pts=1, date=datetime(2026, 11, 4, tzinfo=UTC), qts=0),
    ],
    ids=lambda r: type(r).__name__,
)
def test_an_open_bot_chat_opens_nothing_else(request_: object) -> None:
    with pytest.raises(RequestNotAllowed):
        check_request(request_, bot_id=BOT_ID)


# --- the same on a real Telethon client -----------------------------------------------------------------


def _client() -> userclient._OperatorClient:
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    return userclient._new_client(creds, None, ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port())))


async def test_the_client_refuses_chat_requests_until_the_bots_chat_is_open() -> None:
    client = _client()
    assert client._mtproxybot_id is None
    for request_ in (_send(), _history(), _press()):
        with pytest.raises(RequestNotAllowed):
            await client(request_)  # refused before any connection is even tried
    client._mtproxybot_id = BOT_ID  # what open_mtproxybot_chat sets after its checks
    with pytest.raises(RequestNotAllowed):
        await client(_send(types.InputPeerUser(user_id=777_000, access_hash=1)))
    with pytest.raises(RequestNotAllowed):
        await client([_history(), _send(types.InputPeerSelf())])  # one bad item refuses the batch
    # The bot's own peer passes the allowlist and goes on to the (closed) network: a real failure.
    with pytest.raises(ConnectionError):
        await client(_send())


async def test_opening_the_chat_needs_telegram_and_fails_cleanly_without_it() -> None:
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=3)
    with pytest.raises(MtpError) as caught:
        async with operator_session(creds, SecretValue(session_string(closed_port())), options=options) as op:
            await op.open_mtproxybot_chat(BOT_ID)
    assert caught.value.code == "telegram_unavailable"


async def test_the_chat_opens_only_for_the_bot_id_stored_at_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/11 lock 1: if the username @MTProxybot now leads to another user (it was taken over),
    no chat opens, and the client keeps refusing every chat request: the secret never goes there.
    Telegram's answer to the username lookup is a REAL Telethon ``User`` object, as data."""
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)
    with contextlib.ExitStack() as hold:
        op = OperatorSession(userclient._new_client(creds, None, options), creds, options, hold)
        answer: list[types.User] = []

        async def resolved() -> types.User:
            return answer[-1]

        monkeypatch.setattr(op, "_mtproxybot_user", resolved)
        for user in (
            types.User(id=BOT_ID + 1, access_hash=7_001, bot=True, username="MTProxybot"),  # another user
            types.User(id=BOT_ID, access_hash=None, bot=True, username="MTProxybot"),  # no way to address it
        ):
            answer.append(user)
            with pytest.raises(MtpError) as caught:
                await op.open_mtproxybot_chat(BOT_ID)
            assert caught.value.code == "mtproxybot_changed"
            assert op._client._mtproxybot_id is None
            with pytest.raises(RequestNotAllowed):
                await op._client(_send(types.InputPeerUser(user_id=user.id, access_hash=7_001)))
        answer.append(types.User(id=BOT_ID, access_hash=7_001, bot=True, username="MTProxybot"))
        chat = await op.open_mtproxybot_chat(BOT_ID)
        assert chat.bot_id == BOT_ID and op._client._mtproxybot_id == BOT_ID
        assert chat._peer == types.InputPeerUser(user_id=BOT_ID, access_hash=7_001)


@pytest.fixture
def telethon_debug() -> Iterator[None]:
    logger = logging.getLogger("telethon")
    old = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        logger.setLevel(old)


async def test_a_secret_sent_to_the_bot_stays_out_of_every_log_line(telethon_debug: None) -> None:
    secret = secrets.token_hex(16)
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)
    with contextlib.ExitStack() as hold:  # what operator_session holds for one connection
        op = OperatorSession(userclient._new_client(creds, None, options), creds, options, hold)
        op._client._mtproxybot_id = BOT_ID
        chat = BotChat(op, BOT)
        with all_logs(logging.DEBUG) as logs:
            with pytest.raises(MtpError) as caught:
                await chat.send(secret, sensitive=True)  # a real client with no connection: refused
            assert secret in LIVE_SECRETS._live  # registered before the request was attempted ...
            logging.getLogger("telethon.network.mtprotosender").info("echo %s", secret)
            logging.getLogger("telethon.client.users").warning("upper %s", secret.upper())
    assert caught.value.code == "telegram_unavailable"
    text = logs.getvalue()
    assert "echo [redacted]" in text and "upper [redacted]" in text
    assert secret not in text and secret.upper() not in text
    assert secret not in LIVE_SECRETS._live  # ... and released with the connection


# --- Telegram's answers as plain data -------------------------------------------------------------------


def test_inline_buttons_become_plain_data_and_unsafe_buttons_are_never_pressable() -> None:
    callback, url = types.InlineButtonTypeCallback, types.InlineButtonTypeUrl
    markup = types.ReplyInlineMarkup(
        rows=[
            types.KeyboardInlineButtonRow(
                buttons=[
                    types.KeyboardInlineButton(text="Set promotion", type=callback(data=b"p:1")),
                    types.KeyboardInlineButton(
                        text="Transfer", type=callback(data=b"t:1", requires_password=True)
                    ),
                ]
            ),
            types.KeyboardInlineButtonRow(
                buttons=[types.KeyboardInlineButton(text="Docs", type=url(url="https://t.me/x"))]
            ),
        ]
    )
    # The markup survives Telegram's own wire format unchanged (serialised and read back).
    from telethon.extensions import BinaryReader

    markup = BinaryReader(bytes(markup)).tgread_object()
    assert userclient._buttons(markup) == (
        (BotButton("Set promotion", b"p:1"), BotButton("Transfer", None)),
        (BotButton("Docs", None),),
    )
    assert userclient._buttons(types.ReplyKeyboardHide()) == ()
    assert userclient._buttons(None) == ()


def test_the_new_message_id_is_read_from_every_shape_of_answer() -> None:
    now = datetime(2026, 11, 4, tzinfo=UTC)
    assert (
        userclient._sent_message_id(types.UpdateShortSentMessage(id=55, pts=1, pts_count=1, date=now)) == 55
    )
    updates = types.Updates(
        updates=[types.UpdateMessageID(id=56, random_id=1)], users=[], chats=[], date=now, seq=0
    )
    assert userclient._sent_message_id(updates) == 56
    assert userclient._sent_message_id(types.UpdatesTooLong()) is None
