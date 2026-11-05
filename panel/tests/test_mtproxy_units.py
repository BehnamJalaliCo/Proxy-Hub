"""The operator account's pure parts and its MTProto wrapper without Telegram (decision D10: no
imitation of Telegram anywhere).

* Telegram's errors are the REAL Telethon exception classes, built as data (the way Telethon builds
  them from an ``rpc_error``), and fed to our classifier.
* The request allowlist and the post-login override are exercised on a real, unconnected Telethon
  client: a refused request raises before any byte is sent.
* The log guard is proven at DEBUG with real Telethon log lines: the library's own struct-error line
  that prints a whole request object, and a real connection attempt to a local port nobody listens
  on (a real refusal).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from telethon import TelegramClient, errors, functions, types
from test_telegram_support import SilentServer, all_logs

from proxyhub.mtproxy import messages, state, userclient, validation
from proxyhub.mtproxy.state import AccountRow
from proxyhub.mtproxy.userclient import (
    ALLOWED_REQUESTS,
    LIVE_SECRETS,
    ApiCredentials,
    ClientOptions,
    Endpoint,
    MtpError,
    RequestNotAllowed,
    check_request,
    classify,
    operator_session,
)
from proxyhub.mtproxy.validation import InputError, mask_phone
from proxyhub.vault import SecretValue

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
# Ofcom's range for drama (+44 7700 900000-900999) is never assigned to a real subscriber.
PHONE = "+447700900123"


def random_hash() -> str:
    return secrets.token_hex(16)


def closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- input formats ----------------------------------------------------------------------------------


def test_api_id() -> None:
    assert validation.parse_api_id("1234567") == 1234567
    assert validation.parse_api_id(" ۱۲۳۴۵۶۷ ") == 1234567
    assert validation.parse_api_id(94575) == 94575
    for bad in ["", "0", "0123", "12a", "-5", "1" * 11, str(2**31), "12 34", True, 0, -1]:
        with pytest.raises(InputError) as caught:
            validation.parse_api_id(bad)  # type: ignore[arg-type]
        assert caught.value.code == "api_id_format"


def test_api_hash() -> None:
    value = random_hash()
    assert validation.parse_api_hash(f" {value.upper()}\n") == value
    for bad in ["", value[:31], value + "0", "g" * 32, value[:16] + " " + value[16:]]:
        with pytest.raises(InputError) as caught:
            validation.parse_api_hash(bad)
        assert caught.value.code == "api_hash_format"
        assert str(caught.value) == "api_hash_format"  # the input never comes back


def test_phone_is_normalised_to_e164() -> None:
    for typed in [
        "+44 7700 900123",
        "+44 (7700) 900-123",
        "0044 7700 900123",
        "+۴۴۷۷۰۰۹۰۰۱۲۳",
        "‎+447700900123",
    ]:
        assert validation.parse_phone(typed) == PHONE, typed
    for bad in [
        "447700900123",
        "07700900123",
        "+0447700900123",
        "+123456",
        "+1234567890123456",
        "+44abc",
        "",
    ]:
        with pytest.raises(InputError) as caught:
            validation.parse_phone(bad)
        assert caught.value.code == "phone_format"
        assert bad not in str(caught.value) or bad == ""


def test_code_and_password() -> None:
    assert validation.parse_code("12345") == "12345"
    assert validation.parse_code(" 12 345 ") == "12345"
    assert validation.parse_code("۱۲۳۴۵") == "12345"
    for bad in ["", "1234", "123456789", "12a45", "١٢٣٤"]:
        with pytest.raises(InputError) as caught:
            validation.parse_code(bad)
        assert caught.value.code == "code_format"
    assert validation.parse_password(" with spaces ") == " with spaces "
    for bad in ["", "x" * 257]:
        with pytest.raises(InputError) as caught:
            validation.parse_password(bad)
        assert caught.value.code == "password_format"


def test_mask_phone_shows_four_digits_and_never_the_length() -> None:
    assert mask_phone(PHONE) == "+44•••••23"
    assert mask_phone("+989121234567") == "+98•••••67"
    short, long = mask_phone("+3531234567"), mask_phone("+861391234567890")
    assert len(short) == len(long) == len("+44•••••23")
    for masked in (short, long, mask_phone(PHONE)):
        assert sum(c.isdigit() for c in masked) == 4
    assert mask_phone("not a phone") == "+•••••"


# --- Telegram's errors (real Telethon classes, built as data) ------------------------------------------


def _send_code_request(api_hash: str) -> object:
    return functions.auth.SendCodeRequest(PHONE, 123456, api_hash, types.CodeSettings())


@pytest.mark.parametrize(
    ("exc", "code", "rpc"),
    [
        (errors.ApiIdInvalidError(None), "api_id_invalid", "API_ID_INVALID"),
        (errors.ApiIdPublishedFloodError(None), "api_id_published", "API_ID_PUBLISHED_FLOOD"),
        (errors.PhoneNumberInvalidError(None), "phone_number_invalid", "PHONE_NUMBER_INVALID"),
        (errors.PhoneNumberBannedError(None), "phone_number_banned", "PHONE_NUMBER_BANNED"),
        (errors.PhoneNumberUnoccupiedError(None), "phone_number_unoccupied", "PHONE_NUMBER_UNOCCUPIED"),
        (errors.PhoneNumberFloodError(None), "phone_number_flood", "PHONE_NUMBER_FLOOD"),
        (errors.PhoneCodeInvalidError(None), "phone_code_invalid", "PHONE_CODE_INVALID"),
        (errors.PhoneCodeExpiredError(None), "phone_code_expired", "PHONE_CODE_EXPIRED"),
        (errors.SessionPasswordNeededError(None), "password_needed", "SESSION_PASSWORD_NEEDED"),
        (errors.PasswordHashInvalidError(None), "password_invalid", "PASSWORD_HASH_INVALID"),
        (errors.PhonePasswordFloodError(None), "password_flood", "PHONE_PASSWORD_FLOOD"),
        (errors.AuthKeyUnregisteredError(None), "session_terminated", "AUTH_KEY_UNREGISTERED"),
        (errors.SessionRevokedError(None), "session_terminated", "SESSION_REVOKED"),
        (errors.UserDeactivatedError(None), "session_terminated", "USER_DEACTIVATED"),
        (errors.UserDeactivatedBanError(None), "session_terminated", "USER_DEACTIVATED_BAN"),
        (errors.UsernameNotOccupiedError(None), "mtproxybot_not_found", "USERNAME_NOT_OCCUPIED"),
        (errors.SendCodeUnavailableError(None), "send_code_unavailable", "SEND_CODE_UNAVAILABLE"),
        (errors.ServerError(None, "INTERNAL", 500), "telegram_unavailable", "INTERNAL"),
        (
            errors.RPCError(None, "SOMETHING_NEW_FROM_TELEGRAM", 400),
            "telegram_refused",
            "SOMETHING_NEW_FROM_TELEGRAM",
        ),
        (errors.BadRequestError(None, "lower case is not a name", 400), "telegram_refused", None),
    ],
)
def test_telegram_errors_map_to_contract_codes(exc: Exception, code: str, rpc: str | None) -> None:
    mapped = classify(exc)
    assert mapped is not None
    assert (mapped.code, mapped.rpc) == (code, rpc)


def test_flood_wait_keeps_its_seconds() -> None:
    for kind in (errors.FloodWaitError, errors.FloodPremiumWaitError, errors.FloodTestPhoneWaitError):
        mapped = classify(kind(None, capture=4231))
        assert mapped is not None and mapped.code == "flood_wait" and mapped.seconds == 4231


def test_network_failures_are_unavailable_and_programming_errors_are_not_hidden() -> None:
    for exc in [
        ConnectionRefusedError(111, "refused"),
        ConnectionResetError(),
        TimeoutError(),
        asyncio.IncompleteReadError(b"", 8),
        ValueError("Request was unsuccessful 4 time(s)"),
        errors.InvalidBufferError(b"\x6c\xfe\xff\xff"),
    ]:
        mapped = classify(exc)
        assert mapped is not None and mapped.code == "telegram_unavailable", repr(exc)
    assert classify(TypeError("bug")) is None
    assert classify(KeyError("bug")) is None


def test_a_mapped_error_carries_nothing_from_the_request() -> None:
    api_hash = random_hash()
    request = _send_code_request(api_hash)
    for exc in [
        errors.ApiIdInvalidError(request),
        errors.FloodWaitError(request, capture=60),
        errors.RPCError(request, "WEIRD", 400),
    ]:
        assert api_hash in str(request)  # the request itself holds them ...
        mapped = classify(exc)
        assert mapped is not None
        text = f"{mapped} {mapped!r} {vars(mapped)}"
        assert api_hash not in text and PHONE not in text and PHONE[1:] not in text  # ... we do not


# --- the request allowlist (on a real, unconnected client) ----------------------------------------------


def test_only_six_operations_worth_of_requests_are_allowed() -> None:
    names = {kind.__name__ for kind in ALLOWED_REQUESTS}
    assert names == {
        "SendCodeRequest",
        "SignInRequest",
        "GetPasswordRequest",
        "CheckPasswordRequest",
        "GetUsersRequest",
        "ResolveUsernameRequest",
        "LogOutRequest",
        "GetConfigRequest",
        "GetStateRequest",
        # D38: our own channels' statistics, each only for the peers of a ChannelScope
        # (tests/test_analytics_units.py proves the argument checks).
        "GetFullChannelRequest",
        "GetBroadcastStatsRequest",
        "LoadAsyncGraphRequest",
        "GetMessagesViewsRequest",
        # D38, the operator poster's two reads: the account's OWN rights in our channels and the
        # owner's one emoji pack (tests/test_operator_poster_allowlist.py proves the argument checks).
        "GetParticipantRequest",
        "GetStickerSetRequest",
    }
    # Nothing here sends: the messages namespace has the read-only views counter and the emoji pack.
    # (A post or an edit is CHANNEL_POST_REQUESTS, allowed only for a scope's post channels.)
    messages = {kind.__name__ for kind in ALLOWED_REQUESTS if kind.__module__.endswith(".messages")}
    assert messages == {"GetMessagesViewsRequest", "GetStickerSetRequest"}


@pytest.mark.parametrize(
    "request_",
    [
        functions.messages.GetDialogsRequest(
            offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=10, hash=0
        ),
        functions.contacts.GetContactsRequest(hash=0),
        functions.messages.GetHistoryRequest(
            peer=types.InputPeerSelf(),
            offset_id=0,
            offset_date=None,
            add_offset=0,
            limit=1,
            max_id=0,
            min_id=0,
            hash=0,
        ),
        functions.messages.SendMessageRequest(peer=types.InputPeerSelf(), message="hello"),
        functions.updates.GetDifferenceRequest(pts=1, date=NOW, qts=0),
        functions.users.GetUsersRequest([types.InputUser(user_id=777000, access_hash=1)]),
        functions.users.GetUsersRequest([]),
        functions.contacts.ResolveUsernameRequest(username="durov"),
        functions.account.GetAuthorizationsRequest(),
    ],
)
def test_requests_outside_the_allowlist_are_refused(request_: object) -> None:
    with pytest.raises(RequestNotAllowed):
        check_request(request_)


def test_allowed_requests_pass() -> None:
    check_request(functions.users.GetUsersRequest([types.InputUserSelf()]))
    check_request(functions.contacts.ResolveUsernameRequest(username="MTProxybot"))
    check_request(functions.contacts.ResolveUsernameRequest(username="mtproxybot"))
    check_request(functions.auth.LogOutRequest())


def _client() -> TelegramClient:
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(random_hash()))
    return userclient._new_client(creds, None, ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port())))


async def test_the_client_refuses_before_sending_anything() -> None:
    client = _client()
    assert not client.is_connected()
    with pytest.raises(RequestNotAllowed):
        await client(functions.contacts.GetContactsRequest(hash=0))
    with pytest.raises(RequestNotAllowed):
        await client(
            [
                functions.auth.LogOutRequest(),
                functions.messages.GetDialogsRequest(
                    offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=10, hash=0
                ),
            ]
        )
    # An allowed request goes on to the (closed) network: a different, real failure.
    with pytest.raises(ConnectionError):
        await client(functions.auth.LogOutRequest())


async def test_login_does_not_catch_up_on_any_chat() -> None:
    user = types.User(id=5_000_001, bot=False, access_hash=42, first_name="Op")
    # Control: the library's own post-login step asks Telegram for the update state (and then the
    # difference); on an unconnected client that is a real connection error.
    with pytest.raises(ConnectionError):
        await TelegramClient._on_login(_client(), user)
    client = _client()
    assert await client._on_login(user) is user  # ours sends nothing at all
    assert client._authorized is True
    assert client._message_box.is_empty()  # so the update loop never asks for a difference


# --- the log guard -------------------------------------------------------------------------------------


@pytest.fixture
def telethon_debug() -> Iterator[None]:
    logger = logging.getLogger("telethon")
    old = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        logger.setLevel(old)


def test_telethon_logs_stay_at_warning_and_never_propagate_directly() -> None:
    logger = logging.getLogger("telethon")
    assert logger.level == logging.WARNING
    assert logger.propagate is False
    assert sum(isinstance(h, userclient._RedactingForwarder) for h in logger.handlers) == 1
    assert userclient.install_log_guard() is logger  # idempotent
    assert sum(isinstance(h, userclient._RedactingForwarder) for h in logger.handlers) == 1
    with all_logs(logging.DEBUG) as logs:
        logging.getLogger("telethon.network.mtprotosender").debug("debug is not even created")
    assert "debug is not even created" not in logs.getvalue()


def _assert_clean(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle not in text, "a secret reached a log line"
        assert needle.encode().hex() not in text, "a hex-encoded secret reached a log line"


def test_secrets_never_reach_a_log_line_at_debug(telethon_debug: None) -> None:
    api_hash, code, password = random_hash(), "73519", "correct horse battery"
    session = "1" + secrets.token_urlsafe(260)
    phone_code_hash = secrets.token_hex(9)
    send_code = functions.auth.SendCodeRequest(PHONE, 123456, api_hash, types.CodeSettings())
    sign_in = functions.auth.SignInRequest(
        phone_number=PHONE, phone_code_hash=phone_code_hash, phone_code=code
    )
    library = logging.getLogger("telethon.network.mtprotosender")
    with all_logs(logging.DEBUG) as logs, LIVE_SECRETS.using(code, password, phone_code_hash):
        # Telethon's own line (mtprotosender.py) prints the whole request object at ERROR.
        library.error("Request caused struct.error: %s: %s", "required argument is not an integer", send_code)
        library.debug("Assigned msg_id to %s", sign_in)
        library.info("raw values %s %s %s %s", PHONE[1:], session, password, b"\x01\x02" * 40)
        library.warning("a broken format %s %s", PHONE)  # too few arguments: still cleaned
        try:
            raise RuntimeError(f"boom {api_hash} {PHONE} {code}")
        except RuntimeError:
            library.exception("with a traceback")
        library.info("stack", stack_info=True)
    text = logs.getvalue()
    assert "Request caused struct.error" in text and "with a traceback" in text  # lines are kept ...
    assert userclient.REDACTED in text
    _assert_clean(text, api_hash, PHONE, PHONE[1:], "7700 900123", code, password, session, phone_code_hash)


async def test_a_session_keeps_its_secrets_out_of_every_log_line_while_it_runs(telethon_debug: None) -> None:
    # 18 hex characters, like Telegram's phone_code_hash: no pattern would recognise it.
    phone_code_hash = secrets.token_hex(9)
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(random_hash()))

    async def attempt(port: int) -> None:
        options = ClientOptions(endpoint=Endpoint("127.0.0.1", port), deadline_s=3)
        with pytest.raises(MtpError):
            async with operator_session(
                creds,
                None,
                phone=SecretValue(PHONE),
                phone_code_hash=SecretValue(phone_code_hash),
                options=options,
            ):
                pass

    async with SilentServer() as silent:  # a real connection that hangs: the session stays open
        task = asyncio.create_task(attempt(silent.port))
        for _ in range(100):
            if silent.connections:
                break
            await asyncio.sleep(0.05)
        assert silent.connections == 1 and not task.done()  # the connection is in flight
        with all_logs(logging.DEBUG) as logs:
            logging.getLogger("telethon.network.mtprotosender").info(
                "hash in a library line: %s", phone_code_hash
            )
        await task
    assert "hash in a library line: [redacted]" in logs.getvalue()
    assert phone_code_hash not in logs.getvalue()
    assert phone_code_hash not in LIVE_SECRETS._live  # released once the connection is closed


async def test_a_real_connection_attempt_logs_no_secret(telethon_debug: None) -> None:
    api_hash = random_hash()
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(api_hash))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=10)
    with all_logs(logging.DEBUG) as logs:
        with pytest.raises(MtpError) as caught:
            async with operator_session(creds, None, phone=SecretValue(PHONE), options=options) as op:
                await op.send_code(SecretValue(PHONE))
    assert caught.value.code == "telegram_unavailable"
    text = logs.getvalue()
    assert "telethon.network.mtprotosender" in text  # the library's lines did come through
    _assert_clean(text, api_hash, PHONE, PHONE[1:])
    assert not LIVE_SECRETS._live  # nothing stays registered after the connection is closed


def test_redaction_patterns() -> None:
    assert userclient.redact(f"phone {PHONE} and +44 7700 900123") == "phone [redacted] and [redacted]"
    assert userclient.redact("+44 7700 900123 7212345678901234567") == "[redacted] 7212345678901234567"
    assert userclient.redact("447700900123") == "[redacted]"
    assert (
        userclient.redact("msg_id 7212345678901234567 port 55432") == "msg_id 7212345678901234567 port 55432"
    )
    assert userclient.redact("hash " + random_hash()) == "hash [redacted]"
    assert userclient.redact("Connecting to 149.154.167.51:443/TcpFull...") == (
        "Connecting to 149.154.167.51:443/TcpFull..."
    )
    with LIVE_SECRETS.using("abc12"):
        assert userclient.redact("code abc12.") == "code [redacted]."
    assert userclient.redact("code abc12.") == "code abc12."


# --- the status row and its transitions ----------------------------------------------------------------


def _connected(**kw: object) -> AccountRow:
    base = state.connected(
        state.code_sent(login_id="l1", phone_masked="+44•••••23", via="app", length=5, now=NOW),
        session_id="s1",
        user_id=5_000_001,
        first_name="Op",
        mtproxybot_id=None,
        check_error=None,
        now=NOW,
    )
    return AccountRow(**{**base.to_json(), **kw})  # type: ignore[arg-type]


def test_fresh_status_shape() -> None:
    assert state.public_status(AccountRow(), now=NOW, kill_switch=True) == {
        "state": "none",
        "phone_masked": None,
        "account": None,
        "login": None,
        "mtproxybot_ok": None,
        "connected_at": None,
        "last_check_at": None,
        "last_check_error": None,
        "disconnected_at": None,
        "disconnect_reason": None,
        "error": None,
        "notice": None,
        "kill_switch": True,
        "check_interval_min": 30,
    }


def test_a_pending_login_expires_after_five_minutes() -> None:
    row = state.code_sent(login_id="l1", phone_masked="+44•••••23", via="app", length=5, now=NOW)
    shown = state.public_status(row, now=NOW + timedelta(minutes=4, seconds=59), kill_switch=False)
    assert shown["state"] == "code_sent"
    assert shown["login"] == {
        "code_via": "app",
        "code_length": 5,
        "code_sent_at": "2026-11-04T12:00:00Z",
        "expires_at": "2026-11-04T12:05:00Z",
    }
    later = state.public_status(row, now=NOW + timedelta(minutes=5), kill_switch=False)
    assert later["state"] == "none" and later["notice"] == "login_expired" and later["login"] is None
    password = state.password_needed(row, NOW + timedelta(minutes=4))
    assert state.public_status(password, now=NOW + timedelta(minutes=8), kill_switch=False)["state"] == (
        "password_needed"
    )


def test_connected_status_and_checks() -> None:
    row = _connected()
    shown = state.public_status(row, now=NOW, kill_switch=False)
    assert shown["state"] == "connected" and shown["phone_masked"] == "+44•••••23"
    assert shown["account"] == {"first_name": "Op", "user_id": 5_000_001}
    assert shown["mtproxybot_ok"] is False and shown["connected_at"] == "2026-11-04T12:00:00Z"
    assert not state.check_due(row, NOW + timedelta(minutes=29))
    assert state.check_due(row, NOW + timedelta(minutes=30))
    assert not state.check_due(state.cleared(), NOW + timedelta(days=1))

    later = NOW + timedelta(minutes=30)
    ok = state.after_check(row, code=None, first_name="Renamed", mtproxybot_id=93372553, now=later)
    assert (ok.state, ok.first_name, ok.mtproxybot_id, ok.last_check_error) == (
        "connected",
        "Renamed",
        93372553,
        None,
    )
    flaky = state.after_check(row, code="telegram_unavailable", now=later)
    assert flaky.state == "connected" and flaky.last_check_error == "telegram_unavailable"
    gone = state.after_check(row, code="session_terminated", rpc="SESSION_REVOKED", now=later)
    assert (gone.state, gone.session_id, gone.disconnect_reason) == ("disconnected", None, "session_revoked")
    shown = state.public_status(gone, now=later, kill_switch=False)
    assert shown["disconnect_reason"] == "session_revoked" and shown["account"] is not None
    broken = state.after_check(row, code="api_id_invalid", now=later)
    assert broken.state == "error" and state.public_status(broken, now=later, kill_switch=False)["error"] == (
        "api_id_invalid"
    )


def test_a_stored_row_is_read_defensively() -> None:
    assert AccountRow.from_json(None) == AccountRow()
    assert AccountRow.from_json({"state": "hacked"}) == AccountRow()
    row = AccountRow.from_json({"state": "connected", "user_id": "5", "wrong_codes": True, "extra": 1})
    assert (row.state, row.user_id, row.wrong_codes) == ("connected", None, 0)
    assert AccountRow.from_json(_connected().to_json()) == _connected()


# --- Persian text -------------------------------------------------------------------------------------


def test_every_code_has_persian_text() -> None:
    for code, text in messages.ERRORS_FA.items():
        assert any("؀" <= ch <= "ۿ" for ch in text), code
    for code in messages.STATUS:
        assert code in messages.ERRORS_FA
    mapped_codes = {code for _, code, _ in userclient._RPC_CODES} - {
        "password_needed",
        "mtproxybot_not_found",
    }
    assert mapped_codes <= set(messages.ERRORS_FA)
    assert messages.wait_text(42) == "۴۲ ثانیه"
    assert messages.wait_text(61) == "۲ دقیقه"
    assert messages.wait_text(7300) == "حدود ۳ ساعت"
    assert "۲ دقیقه" in messages.error_message("flood_wait", seconds=120)
    assert messages.error_message("no_such_code") == messages.ERRORS_FA["telegram_refused"]


def test_the_owner_alert_escapes_its_reason() -> None:
    text = messages.session_ended_alert("<b>SESSION_REVOKED</b>")
    assert "&lt;b&gt;SESSION_REVOKED&lt;/b&gt;" in text and "دستگاه‌ها" in text
