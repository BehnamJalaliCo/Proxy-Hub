"""The operator account's MTProto client (decision D37, docs/11 section 2.1).

This is the ONLY module that imports Telethon (pinned by tests/test_architecture.py). Over one
short-lived connection (:func:`operator_session`) it offers exactly these operations:

* :meth:`OperatorSession.send_code`: ``auth.sendCode`` (the code arrives in the second account's app),
* :meth:`OperatorSession.sign_in_code`: ``auth.signIn`` with that code,
* :meth:`OperatorSession.sign_in_password`: the optional two-step password
  (``account.getPassword`` + ``auth.checkPassword``); the password is used once and never kept,
* :meth:`OperatorSession.get_me`: ``users.getUsers([self])``,
* :meth:`OperatorSession.resolve_mtproxybot`: ``contacts.resolveUsername("MTProxybot")`` only,
* :meth:`OperatorSession.log_out`: ``auth.logOut``, which ends the session on Telegram's side,
* :meth:`OperatorSession.open_mtproxybot_chat`: the chat with @MTProxybot and nobody else
  (:class:`BotChat`: send a text, read the last few messages of that chat, press an inline button
  of a message in that chat), used by proxyhub/sponsor to register proxies.

and, for OUR channels' statistics only (decision D38; proxyhub/mtproxy/channel_stats.py), on a
connection opened with a :class:`ChannelScope` that names the configured main and test channels:

* :meth:`OperatorSession.resolve_channel`: ``contacts.resolveUsername`` of one of those channels,
* :meth:`OperatorSession.channel_full`: ``channels.getFullChannel`` (participant count, stats DC),
* :meth:`OperatorSession.broadcast_stats` / :meth:`OperatorSession.load_graph`:
  ``stats.getBroadcastStats`` and ``stats.loadAsyncGraph`` (only for tokens Telegram handed out in
  that same answer),
* :meth:`OperatorSession.messages_views`: ``messages.getMessagesViews`` of our posts, never
  incrementing the view counter.

Any other peer, any delete request and any send are refused before a byte leaves.

Locks (docs/11 section 2.1):

* Every request passes :meth:`_OperatorClient._call`, which refuses any request type outside
  :data:`ALLOWED_REQUESTS` and :data:`BOT_CHAT_REQUESTS` and checks the arguments of those that
  could reach further (``users.getUsers`` only for the account itself, ``contacts.resolveUsername``
  only for @MTProxybot). The three chat requests (``messages.sendMessage``, ``messages.getHistory``
  with a small limit, ``messages.getBotCallbackAnswer``) pass only when their peer is the
  @MTProxybot user that :meth:`OperatorSession.open_mtproxybot_chat` resolved on this connection
  AND whose id equals the id the panel stored at login. Nothing here can list chats or contacts,
  read any other chat, or write to anyone else.
* No updates: ``receive_updates=False``, and the library's post-login catch-up
  (``updates.getState`` + ``updates.getDifference``) is replaced by one that only records the
  account's own id, so the message box stays empty and no difference is ever requested.
* Logs: the ``telethon`` loggers stay at WARNING and do not propagate; every record they make goes
  through :class:`_RedactingForwarder`, which removes the secrets in use (api_hash, phone, code,
  password, session string, phone_code_hash) and anything shaped like a phone number, a hex key, a
  session string or a byte string before any handler sees it (tests/test_mtproxy_units.py proves
  it at DEBUG).
* Telethon's exceptions never leave this module: :func:`classify` turns them into
  :class:`MtpError` with a contract code.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import traceback
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from telethon import TelegramClient, errors, functions, types
from telethon import password as tl_password
from telethon.sessions import StringSession

from proxyhub import __version__
from proxyhub.vault import SecretValue

log = logging.getLogger(__name__)

MTPROXYBOT: Final = "MTProxybot"
DEFAULT_DEADLINE_S: Final = 20.0
DISCONNECT_DEADLINE_S: Final = 5.0

# --- logging guard ---------------------------------------------------------------------------------

REDACTED: Final = "[redacted]"
TELETHON_LOGGER: Final = "telethon"
FORWARD_LOGGER: Final = "proxyhub.mtproxy.telethon"
_BYTES_RE = re.compile(r"""b'(?:\\.|[^'\\]){12,}'|b"(?:\\.|[^"\\]){12,}\"""")
_TOKENISH_RE = re.compile(r"[A-Za-z0-9_\-]{32,}={0,2}")  # session strings (urlsafe base64), hashes, keys
# 7 to 15 digits (E.164), single spaces or dashes allowed between them; longer ids are kept.
_PHONE_RE = re.compile(r"(?<![\w+])\+?\d(?:[ \-]?\d){6,14}(?!\w)")


class _LiveSecrets:
    """The secret values in use right now (reference counted), so a log line can be cleaned of
    them even when they do not look like a secret (a 5-digit code, a short password)."""

    MIN_LEN: Final = 3

    def __init__(self) -> None:
        self._live: Counter[str] = Counter()

    @contextlib.contextmanager
    def using(self, *values: str | None) -> Iterator[None]:
        held = [v for v in values if v is not None and len(v) >= self.MIN_LEN]
        for v in held:
            self._live[v] += 1
        try:
            yield
        finally:
            for v in held:
                self._live[v] -= 1
                if self._live[v] <= 0:
                    del self._live[v]

    def redact(self, text: str) -> str:
        for value in sorted(self._live, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        text = _BYTES_RE.sub(REDACTED, text)
        text = _TOKENISH_RE.sub(REDACTED, text)
        return _PHONE_RE.sub(REDACTED, text)


LIVE_SECRETS: Final = _LiveSecrets()


def redact(text: str) -> str:
    """``text`` without any secret in use and without anything shaped like one."""
    return LIVE_SECRETS.redact(text)


class _RedactingForwarder(logging.Handler):
    """The FIRST handler of the ``telethon`` logger (which does not propagate). It cleans each record
    IN PLACE (message formatted and redacted, arguments dropped, traceback and stack redacted), so a
    handler added to that logger later (pytest's capture does this) sees only the clean record, and
    then passes it on to :data:`FORWARD_LOGGER`, from where it reaches the normal handlers. The
    record keeps its logger name, level and time."""

    WITHHELD: Final = "[telethon log line withheld]"

    def __init__(self, target: logging.Logger) -> None:
        super().__init__(logging.NOTSET)
        self._target = target

    @staticmethod
    def clean(record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string: its raw arguments are dropped, not shown
            message = str(record.msg)
        exc_text = record.exc_text
        if record.exc_info and record.exc_info[1] is not None:
            exc_text = "".join(traceback.format_exception(record.exc_info[1]))
        record.msg = redact(message)
        record.args = None
        record.exc_info = None
        record.exc_text = redact(exc_text) if exc_text else None
        record.stack_info = redact(record.stack_info) if record.stack_info else None
        record.__dict__.pop("message", None)  # a formatter's cached copy of the old text

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.clean(record)
        except Exception:  # cannot clean it: withhold the whole line rather than pass it on
            record.msg, record.args, record.exc_info, record.exc_text, record.stack_info = (
                self.WITHHELD,
                None,
                None,
                None,
                None,
            )
        try:
            self._target.handle(record)
        except Exception:  # noqa: S110 - logging must never break the caller
            pass


def _guard_first(telethon_log: logging.Logger) -> None:
    """No direct propagation, and exactly one forwarder, placed before every other handler."""
    telethon_log.propagate = False
    ours = [h for h in telethon_log.handlers if isinstance(h, _RedactingForwarder)]
    forwarder = ours[0] if ours else _RedactingForwarder(logging.getLogger(FORWARD_LOGGER))
    for handler in ours:
        telethon_log.removeHandler(handler)
    telethon_log.handlers.insert(0, forwarder)


def install_log_guard() -> logging.Logger:
    """WARNING, no direct propagation, the redacting forwarder first. Idempotent."""
    telethon_log = logging.getLogger(TELETHON_LOGGER)
    telethon_log.setLevel(logging.WARNING)
    _guard_first(telethon_log)
    return telethon_log


install_log_guard()

# --- errors ----------------------------------------------------------------------------------------


class MtpError(Exception):
    """A failure with one of our contract codes. Never carries a Telethon object or a secret:
    ``seconds`` is a wait Telegram asked for, ``rpc`` Telegram's own upper-case error name."""

    def __init__(self, code: str, *, seconds: int | None = None, rpc: str | None = None) -> None:
        super().__init__(code if rpc is None else f"{code} ({rpc})")
        self.code = code
        self.seconds = seconds
        self.rpc = rpc


class RequestNotAllowed(RuntimeError):
    """A request outside the allowlist was attempted (a programming error, never sent)."""


_RPC_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

_SESSION_ENDED: Final[dict[type[Exception], str]] = {
    errors.AuthKeyUnregisteredError: "AUTH_KEY_UNREGISTERED",
    errors.SessionRevokedError: "SESSION_REVOKED",
    errors.SessionExpiredError: "SESSION_EXPIRED",
    errors.UserDeactivatedError: "USER_DEACTIVATED",
    errors.UserDeactivatedBanError: "USER_DEACTIVATED_BAN",
    errors.AuthKeyInvalidError: "AUTH_KEY_INVALID",
    errors.AuthKeyPermEmptyError: "AUTH_KEY_PERM_EMPTY",
    errors.AuthKeyDuplicatedError: "AUTH_KEY_DUPLICATED",
}

_RPC_CODES: Final[tuple[tuple[type[Exception], str, str], ...]] = (
    (errors.ApiIdInvalidError, "api_id_invalid", "API_ID_INVALID"),
    (errors.ConnectionApiIdInvalidError, "api_id_invalid", "CONNECTION_API_ID_INVALID"),
    (errors.ApiIdPublishedFloodError, "api_id_published", "API_ID_PUBLISHED_FLOOD"),
    (errors.PhoneNumberInvalidError, "phone_number_invalid", "PHONE_NUMBER_INVALID"),
    (errors.PhoneNumberBannedError, "phone_number_banned", "PHONE_NUMBER_BANNED"),
    (errors.PhoneNumberUnoccupiedError, "phone_number_unoccupied", "PHONE_NUMBER_UNOCCUPIED"),
    (
        errors.PhoneNumberAppSignupForbiddenError,
        "phone_number_unoccupied",
        "PHONE_NUMBER_APP_SIGNUP_FORBIDDEN",
    ),
    (errors.PhoneNumberFloodError, "phone_number_flood", "PHONE_NUMBER_FLOOD"),
    (errors.PhonePasswordFloodError, "password_flood", "PHONE_PASSWORD_FLOOD"),
    (errors.PhoneCodeInvalidError, "phone_code_invalid", "PHONE_CODE_INVALID"),
    (errors.PhoneCodeEmptyError, "phone_code_invalid", "PHONE_CODE_EMPTY"),
    (errors.PhoneCodeExpiredError, "phone_code_expired", "PHONE_CODE_EXPIRED"),
    (errors.PhoneCodeHashEmptyError, "phone_code_expired", "PHONE_CODE_HASH_EMPTY"),
    (errors.PhoneHashExpiredError, "phone_code_expired", "PHONE_HASH_EXPIRED"),
    (errors.SessionPasswordNeededError, "password_needed", "SESSION_PASSWORD_NEEDED"),
    (errors.PasswordHashInvalidError, "password_invalid", "PASSWORD_HASH_INVALID"),
    (errors.SendCodeUnavailableError, "send_code_unavailable", "SEND_CODE_UNAVAILABLE"),
    (errors.UpdateAppToLoginError, "update_app_to_login", "UPDATE_APP_TO_LOGIN"),
    (errors.UsernameNotOccupiedError, "mtproxybot_not_found", "USERNAME_NOT_OCCUPIED"),
    (errors.UsernameInvalidError, "mtproxybot_not_found", "USERNAME_INVALID"),
)

_FLOOD: Final = (errors.FloodWaitError, errors.FloodPremiumWaitError, errors.FloodTestPhoneWaitError)
_TELEGRAM_TROUBLE: Final = (
    errors.ServerError,
    errors.TimedOutError,
    errors.RpcCallFailError,
    errors.RpcMcgetFailError,
    errors.InterdcCallErrorError,
    errors.InterdcCallRichErrorError,
)
_TRANSPORT_TROUBLE: Final = (
    OSError,  # ConnectionError, refused, reset, unreachable
    TimeoutError,
    EOFError,  # asyncio.IncompleteReadError
    ValueError,  # Telethon's "Request was unsuccessful N time(s)" after its retries
    errors.InvalidBufferError,
    errors.InvalidChecksumError,
    errors.SecurityError,
    errors.AuthKeyNotFound,
    errors.TypeNotFoundError,
    errors.BadMessageError,
)


def _rpc_name(exc: Exception) -> str | None:
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) and _RPC_NAME_RE.fullmatch(message) else None


def classify(exc: BaseException) -> MtpError | None:
    """Our contract code for an exception raised by Telethon, or None if it is not a Telegram or
    network failure (a programming error, which must not be hidden)."""
    if isinstance(exc, MtpError):
        return exc
    if isinstance(exc, _FLOOD):
        seconds = getattr(exc, "seconds", None)
        return MtpError(
            "flood_wait",
            seconds=int(seconds) if isinstance(seconds, int) else None,
            rpc="FLOOD_WAIT",
        )
    for kind, rpc in _SESSION_ENDED.items():
        if isinstance(exc, kind):
            return MtpError("session_terminated", rpc=rpc)
    for kind, code, rpc in _RPC_CODES:
        if isinstance(exc, kind):
            return MtpError(code, rpc=rpc)
    if isinstance(exc, _TELEGRAM_TROUBLE):
        return MtpError("telegram_unavailable", rpc=_rpc_name(exc))
    if isinstance(exc, errors.RPCError):
        return MtpError("telegram_refused", rpc=_rpc_name(exc))
    if isinstance(exc, _TRANSPORT_TROUBLE):
        return MtpError("telegram_unavailable")
    return None


# --- the client ------------------------------------------------------------------------------------

# Requests that cannot return any chat's content. ``help.getConfig`` and ``updates.getState`` (only
# counters: pts, qts, date, seq, unread_count) are used by the library itself when Telegram moves
# the login to another data centre. The four channel-statistics requests (decision D38) return only
# Telegram's aggregate numbers about OUR channels, and only for the peers in the session's
# :class:`ChannelScope` (the configured main and test channels); without a scope they are refused.
CHANNEL_STATS_REQUESTS: Final[frozenset[type[Any]]] = frozenset(
    {
        functions.channels.GetFullChannelRequest,
        functions.stats.GetBroadcastStatsRequest,
        functions.stats.LoadAsyncGraphRequest,
        functions.messages.GetMessagesViewsRequest,
    }
)
ALLOWED_REQUESTS: Final[frozenset[type[Any]]] = frozenset(
    {
        functions.auth.SendCodeRequest,
        functions.auth.SignInRequest,
        functions.account.GetPasswordRequest,
        functions.auth.CheckPasswordRequest,
        functions.users.GetUsersRequest,
        functions.contacts.ResolveUsernameRequest,
        functions.auth.LogOutRequest,
        functions.help.GetConfigRequest,
        functions.updates.GetStateRequest,
    }
    | CHANNEL_STATS_REQUESTS
)
MAX_VIEWS_IDS: Final = 100


@dataclass
class ChannelScope:
    """The only channels a session may ask about: bare channel ids and their public usernames
    (lower case, no @). ``graph_tokens`` collects the async-graph tokens Telegram handed out in this
    session for those channels; stats.loadAsyncGraph accepts only those."""

    channel_ids: frozenset[int] = frozenset()
    usernames: frozenset[str] = frozenset()
    graph_tokens: set[str] = field(default_factory=set)


def _channel_id(peer: Any) -> int | None:
    if isinstance(peer, types.InputChannel | types.InputPeerChannel):
        return int(peer.channel_id)
    return None



# The chat with @MTProxybot (proxyhub/sponsor). Allowed ONLY with the bot's own peer, see
# :func:`_check_bot_chat_request`.
BOT_CHAT_REQUESTS: Final[frozenset[type[Any]]] = frozenset(
    {
        functions.messages.SendMessageRequest,
        functions.messages.GetHistoryRequest,
        functions.messages.GetBotCallbackAnswerRequest,
    }
)
MAX_HISTORY_LIMIT: Final = 10
MAX_SENT_TEXT: Final = 512
MAX_CALLBACK_DATA: Final = 64
# Fields a request to the bot may carry; every other field must be empty (None or False), so no
# reply markup, entities, scheduling, "send as", paid stars or forwarded content can ride along.
_BOT_FIELDS: Final[dict[type[Any], frozenset[str]]] = {
    functions.messages.SendMessageRequest: frozenset({"peer", "message", "random_id", "no_webpage"}),
    functions.messages.GetHistoryRequest: frozenset(
        {"peer", "offset_id", "offset_date", "add_offset", "limit", "max_id", "min_id", "hash"}
    ),
    functions.messages.GetBotCallbackAnswerRequest: frozenset({"peer", "msg_id", "data"}),
}


def _check_bot_chat_request(request: Any, bot_id: int | None) -> None:
    kind = type(request)
    if bot_id is None:
        raise RequestNotAllowed(f"{kind.__name__} is allowed only in the chat with @MTProxybot")
    peer: Any = getattr(request, "peer", None)
    if type(peer) is not types.InputPeerUser or peer.user_id != bot_id:
        raise RequestNotAllowed(f"{kind.__name__} is allowed only with @MTProxybot")
    extra = [
        name
        for name, value in vars(request).items()
        if name not in _BOT_FIELDS[kind] and value is not None and value is not False
    ]
    if extra:
        raise RequestNotAllowed(f"{kind.__name__}: fields not allowed: {sorted(extra)}")
    if kind is functions.messages.SendMessageRequest:
        text = request.message
        if not isinstance(text, str) or not 0 < len(text) <= MAX_SENT_TEXT:
            raise RequestNotAllowed("messages.sendMessage: a short text only")
    elif kind is functions.messages.GetHistoryRequest:
        limit = request.limit
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= MAX_HISTORY_LIMIT:
            raise RequestNotAllowed(f"messages.getHistory: limit 1 to {MAX_HISTORY_LIMIT}")
    else:
        data = request.data
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_CALLBACK_DATA:
            raise RequestNotAllowed("messages.getBotCallbackAnswer: an inline button's data only")


def check_request(
    request: Any, scope: ChannelScope | None = None, *, bot_id: int | None = None
) -> None:
    """Raise :class:`RequestNotAllowed` unless ``request`` is allowed with these arguments.
    ``scope``: our configured channels (statistics requests are allowed only for them).
    ``bot_id``: @MTProxybot's user id once its chat is open on this connection, else None."""
    kind = type(request)
    if kind in BOT_CHAT_REQUESTS:
        _check_bot_chat_request(request, bot_id)
        return
    scope = scope or ChannelScope()
    if kind not in ALLOWED_REQUESTS:
        raise RequestNotAllowed(f"request not allowed: {kind.__name__}")
    if kind is functions.users.GetUsersRequest and not (
        request.id and all(isinstance(u, types.InputUserSelf) for u in request.id)
    ):
        raise RequestNotAllowed("users.getUsers is allowed only for the account itself")
    if kind is functions.contacts.ResolveUsernameRequest:
        name = str(request.username).lower()
        if name != MTPROXYBOT.lower() and name not in scope.usernames:
            raise RequestNotAllowed("contacts.resolveUsername: only @MTProxybot and our own channels")
    if kind in (functions.channels.GetFullChannelRequest, functions.stats.GetBroadcastStatsRequest):
        if _channel_id(request.channel) not in scope.channel_ids:
            raise RequestNotAllowed(f"{kind.__name__}: only our configured channels")
    if kind is functions.messages.GetMessagesViewsRequest and (
        _channel_id(request.peer) not in scope.channel_ids
        or request.increment
        or not 1 <= len(request.id or []) <= MAX_VIEWS_IDS
    ):
        raise RequestNotAllowed("messages.getMessagesViews: our channels only, never incrementing")
    if kind is functions.stats.LoadAsyncGraphRequest and (
        request.token not in scope.graph_tokens or request.x is not None
    ):
        raise RequestNotAllowed("stats.loadAsyncGraph: only a token Telegram gave for our channel")


class _OperatorClient(TelegramClient):  # type: ignore[misc]  # Telethon ships no type hints
    scope: ChannelScope = ChannelScope()
    # Set only by OperatorSession.open_mtproxybot_chat, after Telegram resolved @MTProxybot to the
    # very user id the panel stored at login.
    _mtproxybot_id: int | None = None

    async def _call(
        self, sender: Any, request: Any, ordered: bool = False, flood_sleep_threshold: Any = None
    ) -> Any:
        for item in request if isinstance(request, list | tuple) else (request,):
            check_request(item, self.scope, bot_id=self._mtproxybot_id)
        return await super()._call(
            sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold
        )

    async def _on_login(self, user: Any) -> Any:
        # The library would call updates.getState and updates.getDifference here (catching up on
        # every chat). We only remember who we are; the message box stays empty, so the update loop
        # never asks for a difference.
        self._mb_entity_cache.set_self_user(user.id, user.bot, user.access_hash)
        self._authorized = True
        return user


@dataclass(frozen=True)
class ApiCredentials:
    api_id: int
    api_hash: SecretValue

    def __repr__(self) -> str:
        return f"ApiCredentials(api_id={self.api_id}, api_hash=***)"


@dataclass(frozen=True)
class Endpoint:
    """Where to connect instead of Telegram's default data centre. Production passes None; tests
    use a local port (a real refusal or a server that never answers)."""

    host: str
    port: int
    dc_id: int = 2


@dataclass(frozen=True)
class ClientOptions:
    endpoint: Endpoint | None = None
    deadline_s: float = DEFAULT_DEADLINE_S


@dataclass(frozen=True)
class CodeSent:
    phone_code_hash: SecretValue
    via: str
    length: int | None

    def __repr__(self) -> str:
        return f"CodeSent(phone_code_hash=***, via={self.via!r}, length={self.length})"


@dataclass(frozen=True)
class Account:
    """The operator account as the panel keeps it: no phone number."""

    user_id: int
    first_name: str
    is_bot: bool


_CODE_VIA: Final[dict[type[Any], str]] = {
    types.auth.SentCodeTypeApp: "app",
    types.auth.SentCodeTypeSms: "sms",
    types.auth.SentCodeTypeFirebaseSms: "sms",
    types.auth.SentCodeTypeCall: "call",
    types.auth.SentCodeTypeFlashCall: "call",
    types.auth.SentCodeTypeMissedCall: "call",
    types.auth.SentCodeTypeFragmentSms: "fragment",
    types.auth.SentCodeTypeEmailCode: "email",
    types.auth.SentCodeTypeSmsWord: "sms_word",
    types.auth.SentCodeTypeSmsPhrase: "sms_word",
}


def _account(user: Any) -> Account:
    return Account(user_id=int(user.id), first_name=str(user.first_name or ""), is_bot=bool(user.bot))


class OperatorSession:
    """One connection to Telegram as the operator account (or as a login in progress)."""

    def __init__(
        self,
        client: _OperatorClient,
        creds: ApiCredentials,
        options: ClientOptions,
        hold: contextlib.ExitStack,
    ) -> None:
        self._client = client
        self._creds = creds
        self._options = options
        self._hold = hold

    def _keep_out_of_logs(self, value: str) -> None:
        """Redact ``value`` from every log line until this connection is closed."""
        self._hold.enter_context(LIVE_SECRETS.using(value))

    async def _run(self, call: Any) -> Any:
        try:
            async with asyncio.timeout(self._options.deadline_s):
                return await call
        except RequestNotAllowed:
            raise
        except Exception as exc:
            mapped = classify(exc)
            if mapped is None:
                raise
            raise mapped from None

    def export(self) -> SecretValue:
        """The session string (auth key and data centre). A secret: vault or encrypted Redis only."""
        value = self._client.session.save()
        if not value:
            raise MtpError("telegram_unavailable")  # no auth key: the connection never completed
        self._keep_out_of_logs(str(value))
        return SecretValue(str(value))

    async def send_code(self, phone: SecretValue) -> CodeSent:
        sent = await self._run(
            self._client(
                functions.auth.SendCodeRequest(
                    phone.reveal(), self._creds.api_id, self._creds.api_hash.reveal(), types.CodeSettings()
                )
            )
        )
        if isinstance(sent, types.auth.SentCodePaymentRequired):
            raise MtpError("send_code_payment_required", rpc="SENT_CODE_PAYMENT_REQUIRED")
        if not isinstance(sent, types.auth.SentCode) or not sent.phone_code_hash:
            raise MtpError("telegram_refused", rpc=type(sent).__name__.upper()[:64])
        length = getattr(sent.type, "length", None)
        self._keep_out_of_logs(str(sent.phone_code_hash))
        return CodeSent(
            phone_code_hash=SecretValue(str(sent.phone_code_hash)),
            via=_CODE_VIA.get(type(sent.type), "other"),
            length=length if isinstance(length, int) else None,
        )

    async def sign_in_code(
        self, phone: SecretValue, phone_code_hash: SecretValue, code: SecretValue
    ) -> Account:
        """Raises ``MtpError("password_needed")`` when the account has a two-step password."""
        self._keep_out_of_logs(code.reveal())
        result = await self._run(
            self._client(
                functions.auth.SignInRequest(
                    phone_number=phone.reveal(),
                    phone_code_hash=phone_code_hash.reveal(),
                    phone_code=code.reveal(),
                )
            )
        )
        if isinstance(result, types.auth.AuthorizationSignUpRequired):
            raise MtpError("phone_number_unoccupied", rpc="SIGN_UP_REQUIRED")
        return _account(result.user)

    async def sign_in_password(self, password: SecretValue) -> Account:
        self._keep_out_of_logs(password.reveal())
        settings = await self._run(self._client(functions.account.GetPasswordRequest()))
        try:
            check = tl_password.compute_check(settings, password.reveal())
        except ValueError:  # no password is set on this account, or an unknown algorithm
            raise MtpError("password_invalid", rpc="PASSWORD_CHECK_FAILED") from None
        result = await self._run(self._client(functions.auth.CheckPasswordRequest(check)))
        return _account(result.user)

    async def get_me(self) -> Account:
        users = await self._run(self._client(functions.users.GetUsersRequest([types.InputUserSelf()])))
        if not users or isinstance(users[0], types.UserEmpty):
            raise MtpError("session_terminated", rpc="USER_EMPTY")
        return _account(users[0])

    async def _mtproxybot_user(self) -> Any:
        found = await self._run(self._client(functions.contacts.ResolveUsernameRequest(username=MTPROXYBOT)))
        peer = getattr(found, "peer", None)
        if not isinstance(peer, types.PeerUser):
            raise MtpError("mtproxybot_not_found")
        for user in getattr(found, "users", []):
            names = {str(user.username or "").lower()} | {
                str(u.username).lower() for u in (getattr(user, "usernames", None) or [])
            }
            if user.id == peer.user_id and user.bot and MTPROXYBOT.lower() in names:
                return user
        raise MtpError("mtproxybot_not_found")

    async def resolve_mtproxybot(self) -> int:
        """@MTProxybot's user id, proving this account can see it."""
        return int((await self._mtproxybot_user()).id)

    async def open_mtproxybot_chat(self, expected_bot_id: int) -> BotChat:
        """The chat with @MTProxybot, and only after Telegram resolved the username to the very user
        id the panel stored at login (``expected_bot_id``). From then on, and only on this
        connection, the three chat requests are allowed with that one peer."""
        user = await self._mtproxybot_user()
        if int(user.id) != expected_bot_id or user.access_hash is None:
            raise MtpError("mtproxybot_changed")
        self._client._mtproxybot_id = int(user.id)
        return BotChat(self, types.InputPeerUser(user_id=int(user.id), access_hash=int(user.access_hash)))

    async def log_out(self) -> None:
        """Ends this session on Telegram (it disappears from Settings > Devices)."""
        await self._run(self._client(functions.auth.LogOutRequest()))

    # --- our channels' statistics (decision D38); every request is checked against the scope ---

    @property
    def home_dc(self) -> int:
        return int(self._client.session.dc_id)

    async def resolve_channel(self, username: str, channel_id: int) -> ChannelRef:
        """One of OUR public channels by its username; its id must be the configured one."""
        found = await self._run(
            self._client(functions.contacts.ResolveUsernameRequest(username=username.lstrip("@")))
        )
        for chat in getattr(found, "chats", []):
            if (
                isinstance(chat, types.Channel)
                and int(chat.id) == channel_id
                and chat.broadcast
                and chat.access_hash is not None
            ):
                return ChannelRef(channel_id=channel_id, access_hash=int(chat.access_hash))
        raise MtpError("channel_not_found")

    async def channel_full(self, ref: ChannelRef) -> Any:
        """channels.getFullChannel -> the ``ChannelFull`` (participants_count, stats_dc, can_view_stats)."""
        result = await self._run(self._client(functions.channels.GetFullChannelRequest(ref.input_channel())))
        return result.full_chat

    async def broadcast_stats(self, ref: ChannelRef) -> Any:
        """stats.getBroadcastStats on the session's own data centre. Tokens of async graphs in the
        answer are remembered so :meth:`load_graph` may ask for exactly those."""
        stats = await self._run(
            self._client(functions.stats.GetBroadcastStatsRequest(channel=ref.input_channel(), dark=False))
        )
        for name in dir(stats):
            graph = getattr(stats, name, None) if name.endswith("_graph") else None
            if isinstance(graph, types.StatsGraphAsync) and graph.token:
                self._client.scope.graph_tokens.add(str(graph.token))
        return stats

    async def load_graph(self, token: str) -> Any:
        return await self._run(self._client(functions.stats.LoadAsyncGraphRequest(token=token)))

    async def messages_views(self, ref: ChannelRef, ids: list[int]) -> list[tuple[int, int, int]]:
        """(message id, views, forwards) of our posts, without counting a view (increment=False)."""
        ids = ids[:MAX_VIEWS_IDS]
        result = await self._run(
            self._client(
                functions.messages.GetMessagesViewsRequest(peer=ref.input_peer(), id=ids, increment=False)
            )
        )
        out: list[tuple[int, int, int]] = []
        for msg_id, item in zip(ids, getattr(result, "views", []), strict=False):
            out.append((msg_id, int(item.views or 0), int(item.forwards or 0)))
        return out


@dataclass(frozen=True)
class ChannelRef:
    channel_id: int
    access_hash: int

    def __repr__(self) -> str:
        return f"ChannelRef(channel_id={self.channel_id})"

    def input_channel(self) -> Any:
        return types.InputChannel(channel_id=self.channel_id, access_hash=self.access_hash)

    def input_peer(self) -> Any:
        return types.InputPeerChannel(channel_id=self.channel_id, access_hash=self.access_hash)


# --- the chat with @MTProxybot (proxyhub/sponsor) ------------------------------------------------------


@dataclass(frozen=True)
class BotButton:
    """An inline button. ``data`` is its callback data; None for any other kind of button (a link,
    a switch, a button that asks for the account's password), which is never pressed."""

    text: str
    data: bytes | None


@dataclass(frozen=True)
class BotMessage:
    """One message of the chat with @MTProxybot as plain data (no Telethon object leaves here)."""

    id: int
    outgoing: bool
    text: str
    edit_date: datetime | None
    buttons: tuple[tuple[BotButton, ...], ...]


def _buttons(markup: Any) -> tuple[tuple[BotButton, ...], ...]:
    if not isinstance(markup, types.ReplyInlineMarkup):
        return ()
    rows: list[tuple[BotButton, ...]] = []
    for row in markup.rows:
        found: list[BotButton] = []
        for button in getattr(row, "buttons", []):
            # This layer: KeyboardInlineButton(text, type=InlineButtonTypeCallback(data, ...)).
            kind = getattr(button, "type", None)
            data = None
            if (
                isinstance(kind, types.InlineButtonTypeCallback)
                and not kind.requires_password
                and isinstance(kind.data, bytes)
            ):
                data = bytes(kind.data)
            found.append(BotButton(text=str(getattr(button, "text", "") or ""), data=data))
        rows.append(tuple(found))
    return tuple(rows)


def _sent_message_id(result: Any) -> int | None:
    """Our new message's id from a messages.sendMessage result, when Telegram says it."""
    if isinstance(result, types.UpdateShortSentMessage):
        return int(result.id)
    for update in getattr(result, "updates", None) or []:
        if isinstance(update, types.UpdateMessageID):
            return int(update.id)
        if isinstance(update, types.UpdateNewMessage) and getattr(update.message, "out", False):
            return int(update.message.id)
    return None


class BotChat:
    """The chat with @MTProxybot on one connection (:meth:`OperatorSession.open_mtproxybot_chat`).
    Every request names the bot's own peer; the client's allowlist refuses any other."""

    def __init__(self, session: OperatorSession, peer: Any) -> None:
        self._session = session
        self._peer = peer

    @property
    def bot_id(self) -> int:
        return int(self._peer.user_id)

    async def send(self, text: str, *, sensitive: bool = False) -> int | None:
        """Send ``text`` to the bot; the new message's id if Telegram says it. ``sensitive``: the
        text is a proxy secret, kept out of every log line until the connection is closed."""
        if sensitive:
            self._session._keep_out_of_logs(text)
        result = await self._session._run(
            self._session._client(
                functions.messages.SendMessageRequest(peer=self._peer, message=text, no_webpage=True)
            )
        )
        return _sent_message_id(result)

    async def recent(self, limit: int = MAX_HISTORY_LIMIT) -> list[BotMessage]:
        """The last ``limit`` messages of this one chat, oldest first (ours and the bot's)."""
        result = await self._session._run(
            self._session._client(
                functions.messages.GetHistoryRequest(
                    peer=self._peer,
                    offset_id=0,
                    offset_date=None,
                    add_offset=0,
                    limit=limit,
                    max_id=0,
                    min_id=0,
                    hash=0,
                )
            )
        )
        found: list[BotMessage] = []
        for message in getattr(result, "messages", None) or []:
            peer = getattr(message, "peer_id", None)
            if not isinstance(message, types.Message) or not (
                isinstance(peer, types.PeerUser) and peer.user_id == self.bot_id
            ):
                continue
            found.append(
                BotMessage(
                    id=int(message.id),
                    outgoing=bool(message.out),
                    text=str(message.message or ""),
                    edit_date=message.edit_date if isinstance(message.edit_date, datetime) else None,
                    buttons=_buttons(message.reply_markup),
                )
            )
        return sorted(found, key=lambda m: m.id)

    async def press(self, message_id: int, data: bytes) -> str | None:
        """Press the inline button with callback ``data`` under the bot's message ``message_id``.
        The bot's short answer (a toast), if any. A bot that does not answer the press in time
        (BOT_RESPONSE_TIMEOUT) may still have changed the message, so that is not an error here."""
        try:
            answer = await self._session._run(
                self._session._client(
                    functions.messages.GetBotCallbackAnswerRequest(
                        peer=self._peer, msg_id=message_id, data=data
                    )
                )
            )
        except MtpError as exc:
            if exc.rpc == "BOT_RESPONSE_TIMEOUT":
                return None
            raise
        text = getattr(answer, "message", None)
        return text if isinstance(text, str) and text else None


def _new_client(
    creds: ApiCredentials,
    session: SecretValue | None,
    options: ClientOptions,
    scope: ChannelScope | None = None,
) -> _OperatorClient:
    client = _OperatorClient(
        StringSession(session.reveal() if session is not None else None),
        creds.api_id,
        creds.api_hash.reveal(),
        receive_updates=False,
        catch_up=False,
        flood_sleep_threshold=0,  # a flood wait is reported (with its seconds), never slept through
        request_retries=3,  # room for a data-centre switch during the login
        connection_retries=1,
        retry_delay=1,
        raise_last_call_error=True,
        auto_reconnect=False,
        timeout=min(10.0, options.deadline_s),
        device_model="Proxy-Hub panel",
        system_version="Linux",
        app_version=f"Proxy-Hub {__version__}",
        lang_code="en",
        system_lang_code="en",
        base_logger=logging.getLogger(TELETHON_LOGGER),
        entity_cache_limit=100,
    )
    client.scope = scope or ChannelScope()
    if options.endpoint is not None:
        client.session.set_dc(options.endpoint.dc_id, options.endpoint.host, options.endpoint.port)
    return client


@contextlib.asynccontextmanager
async def operator_session(
    creds: ApiCredentials,
    session: SecretValue | None,
    *,
    phone: SecretValue | None = None,
    phone_code_hash: SecretValue | None = None,
    options: ClientOptions | None = None,
    channels: ChannelScope | None = None,
) -> AsyncIterator[OperatorSession]:
    """Connect (``session`` None: a fresh login), yield, always disconnect. Every secret involved
    is registered with the log guard for the whole time. Connection failures raise MtpError.
    ``channels``: the only channels this connection may ask statistics about (none by default)."""
    options = options or ClientOptions()
    phone_value = phone.reveal() if phone is not None else None
    secrets = (
        creds.api_hash.reveal(),
        session.reveal() if session is not None else None,
        phone_value,
        phone_value.lstrip("+") if phone_value else None,
        phone_code_hash.reveal() if phone_code_hash is not None else None,
    )
    _guard_first(logging.getLogger(TELETHON_LOGGER))  # still first, whatever was added since
    with contextlib.ExitStack() as hold:
        hold.enter_context(LIVE_SECRETS.using(*secrets))
        client = _new_client(creds, session, options, channels)
        started = time.monotonic()
        try:
            try:
                async with asyncio.timeout(options.deadline_s):
                    await client.connect()
            except RequestNotAllowed:
                raise
            except Exception as exc:
                mapped = classify(exc)
                if mapped is None:
                    raise
                log.warning(
                    "mtproxy: connecting to Telegram failed after %.1fs (%s)",
                    time.monotonic() - started,
                    mapped.code,
                )
                raise mapped from None
            yield OperatorSession(client, creds, options, hold)
        finally:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(DISCONNECT_DEADLINE_S):
                    await client.disconnect()
