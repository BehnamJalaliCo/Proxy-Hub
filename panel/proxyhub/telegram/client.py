"""A small typed async client for the Telegram Bot API (https://core.telegram.org/bots/api).

Only the methods Proxy-Hub needs: getMe, getUpdates, sendMessage, editMessageText, getChat,
getChatMember, getChatMemberCount (channel size for the «آمار» page) and createChatInviteLink (a
named tracking link per campaign; links are never revoked or edited). Nothing else can be sent: the
one request path (:meth:`BotClient._call`) refuses any
method name outside :data:`ALLOWED_METHODS` before a request is built, so no message can be removed
and nobody banned through this client, not even with a computed method name (CLAUDE.md rule 6).

Token safety (CLAUDE.md rule 3). The Bot API puts the token in the URL path
(``https://api.telegram.org/bot<token>/<method>``), so:

* httpx logs every request URL at INFO on the logger ``httpx``. :func:`install_log_redaction` puts
  a filter on the httpx/httpcore loggers that rewrites any ``bot<id>:<secret>`` (and any bare
  ``<id>:<secret>`` token) to ``bot<redacted>`` before a handler sees the record. It is installed
  when this module is imported.
* Every error this module raises is one of the :class:`TelegramError` classes, built from the
  status code and Telegram's JSON only, raised ``from None`` so the httpx exception (whose
  ``request`` holds the URL) is not chained into tracebacks; their text goes through :func:`redact`.
* The token is held as a :class:`~proxyhub.vault.SecretValue` and revealed only to build the path.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final

import httpx

from proxyhub.vault import SecretValue

API_BASE: Final = "https://api.telegram.org"
# A Bot API token is "<bot id>:<secret>" (BotFather). Checked before use, so nothing else (a "/",
# "?" or "#") can ever be spliced into the request path.
TOKEN_RE: Final = re.compile(r"^[0-9]{3,20}:[A-Za-z0-9_-]{30,100}$")
_TOKEN_IN_PATH = re.compile(r"bot[0-9]{3,20}(?::|%3[Aa])[A-Za-z0-9_-]+")
_BARE_TOKEN = re.compile(r"\b[0-9]{3,20}:[A-Za-z0-9_-]{30,100}")
_RETRY_IN_TEXT = re.compile(r"retry after (\d+)", re.IGNORECASE)

# The ONLY Bot API methods this client can call. Removing posts, banning, leaving a chat or
# changing the webhook are deliberately absent (rule 6; and a webhook would stop getUpdates).
ALLOWED_METHODS: Final = frozenset(
    {
        "getMe",
        "getUpdates",
        "sendMessage",
        "editMessageText",
        "getChat",
        "getChatMember",
        "getChatMemberCount",
        "createChatInviteLink",
    }
)
# Telegram's limit for an invite link's name (Bot API, createChatInviteLink).
INVITE_LINK_NAME_MAX: Final = 32

DEFAULT_TIMEOUT: Final = httpx.Timeout(15.0, connect=10.0)
MAX_LONG_POLL_SECONDS: Final = 25
DEFAULT_RETRY_AFTER: Final = 5
# Telegram's admin flag for removing posts in a channel. It is only READ, to show the owner that
# the bot should not have it (docs/04: the bot may post and edit, never remove).
CAN_REMOVE_POSTS_FLAG: Final = "can_delete_messages"

_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


def redact(text: str) -> str:
    """Remove anything that looks like a bot token (in a URL path or bare)."""
    return _BARE_TOKEN.sub("<redacted>", _TOKEN_IN_PATH.sub("bot<redacted>", text))


class _RedactTokens(logging.Filter):
    """Rewrites a log record whose formatted message contains a bot token."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken record: drop its arguments rather than risk printing them
            record.msg, record.args = "<unformattable log record>", None
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, None
        return True


def install_log_redaction() -> None:
    """Idempotent: one redacting filter on each httpx/httpcore logger."""
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactTokens) for f in logger.filters):
            logger.addFilter(_RedactTokens())


install_log_redaction()


# --- errors --------------------------------------------------------------------------------------


class TelegramError(Exception):
    """Base class. ``str()`` never holds the token or the request URL."""

    def __init__(self, method: str, message: str) -> None:
        self.method = method
        super().__init__(redact(f"{method}: {message}"))


class InvalidToken(TelegramError):
    """The token does not have the Bot API token shape (nothing was sent)."""

    def __init__(self) -> None:
        super().__init__("token", "not a Bot API token")


class NetworkError(TelegramError):
    """No usable HTTP answer. ``maybe_sent`` is True when the request may have reached Telegram
    (the connection broke or timed out after sending), so a resend could duplicate a message."""

    def __init__(self, method: str, kind: str, *, maybe_sent: bool) -> None:
        self.kind = kind
        self.maybe_sent = maybe_sent
        super().__init__(method, f"network error ({kind}{', may have been sent' if maybe_sent else ''})")


class InvalidResponse(TelegramError):
    """Telegram answered with something that is not the documented JSON shape."""

    def __init__(self, method: str, detail: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(method, f"invalid response ({detail}, HTTP {status})")


class ApiError(TelegramError):
    """``{"ok": false, "error_code": ..., "description": ..., "parameters": {...}}``."""

    def __init__(
        self, method: str, error_code: int, description: str, parameters: dict[str, Any] | None = None
    ) -> None:
        self.error_code = error_code
        self.description = redact(description)
        self.parameters = dict(parameters or {})
        super().__init__(method, f"{error_code} {self.description}")


class BadRequest(ApiError):
    """400: wrong parameters (bad HTML, chat not found, message to edit not found, ...)."""

    @property
    def not_modified(self) -> bool:
        """editMessageText with the same text: the post already shows it (counts as done)."""
        return "message is not modified" in self.description.lower()

    @property
    def chat_not_found(self) -> bool:
        return "chat not found" in self.description.lower()


class Unauthorized(ApiError):
    """401: the token is wrong or was revoked in @BotFather."""


class Forbidden(ApiError):
    """403: the bot is not in that chat, was removed, or the user blocked it."""


class NotFound(ApiError):
    """404: unknown method or malformed token path."""


class Conflict(ApiError):
    """409: another getUpdates poller or a webhook is active for this bot."""


class RetryAfter(ApiError):
    """429 flood control: wait ``seconds`` before the same chat/method is tried again."""

    def __init__(
        self, method: str, error_code: int, description: str, parameters: dict[str, Any] | None = None
    ) -> None:
        super().__init__(method, error_code, description, parameters)
        self.seconds = retry_after_seconds(self.parameters, self.description)


class ServerError(ApiError):
    """5xx from Telegram. The request may or may not have been carried out."""


def retry_after_seconds(parameters: dict[str, Any], description: str) -> int:
    """``parameters.retry_after`` (documented), else "retry after N" in the text, else 5 s."""
    value = parameters.get("retry_after")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    m = _RETRY_IN_TEXT.search(description)
    return int(m.group(1)) if m else DEFAULT_RETRY_AFTER


_ERROR_CLASSES: dict[int, type[ApiError]] = {
    400: BadRequest,
    401: Unauthorized,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    429: RetryAfter,
}


def parse_api_response(method: str, status: int, body: bytes) -> Any:
    """Bot API answer -> ``result`` on success, or the matching :class:`ApiError`.

    Shape (core.telegram.org/bots/api#making-requests): always a JSON object with ``ok``; on
    success ``result``; on error ``error_code``, ``description`` and optional ``parameters``
    (``retry_after``, ``migrate_to_chat_id``).
    """
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise InvalidResponse(method, "not JSON", status=status) from None
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise InvalidResponse(method, "no 'ok' field", status=status)
    if data["ok"]:
        if "result" not in data:
            raise InvalidResponse(method, "no 'result' field", status=status)
        return data["result"]
    code = data.get("error_code")
    if not isinstance(code, int) or isinstance(code, bool):
        code = status
    description = data.get("description")
    if not isinstance(description, str):
        description = ""
    parameters = data.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
    cls = _ERROR_CLASSES.get(code, ServerError if code >= 500 else ApiError)
    raise cls(method, code, description, parameters)


# --- result types --------------------------------------------------------------------------------


def _int(obj: dict[str, Any], key: str, method: str) -> int:
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidResponse(method, f"missing integer {key!r}")
    return value


def _str(obj: dict[str, Any], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) else None


def _obj(value: Any, method: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvalidResponse(method, f"{what} is not an object")
    return value


@dataclass(frozen=True)
class BotUser:
    id: int
    username: str
    first_name: str

    @classmethod
    def from_result(cls, result: Any) -> BotUser:
        obj = _obj(result, "getMe", "result")
        username = _str(obj, "username")
        if not username or obj.get("is_bot") is not True:
            raise InvalidResponse("getMe", "not a bot user")
        return cls(id=_int(obj, "id", "getMe"), username=username, first_name=_str(obj, "first_name") or "")


@dataclass(frozen=True)
class ChatInfo:
    id: int
    type: str  # "private" | "group" | "supergroup" | "channel"
    title: str | None
    username: str | None

    @classmethod
    def from_result(cls, result: Any, method: str = "getChat") -> ChatInfo:
        obj = _obj(result, method, "chat")
        kind = _str(obj, "type")
        if kind is None:
            raise InvalidResponse(method, "chat without type")
        return cls(
            id=_int(obj, "id", method), type=kind, title=_str(obj, "title"), username=_str(obj, "username")
        )


@dataclass(frozen=True)
class MemberRights:
    """The bot's own membership in a chat (getChatMember with the bot's id)."""

    status: str  # "creator" | "administrator" | "member" | "restricted" | "left" | "kicked"
    can_post: bool
    can_edit: bool
    can_remove: bool  # the owner should NOT grant this (docs/04)

    @classmethod
    def from_result(cls, result: Any) -> MemberRights:
        obj = _obj(result, "getChatMember", "member")
        status = _str(obj, "status")
        if status is None:
            raise InvalidResponse("getChatMember", "member without status")
        if status == "creator":
            return cls(status=status, can_post=True, can_edit=True, can_remove=True)
        if status != "administrator":
            return cls(status=status, can_post=False, can_edit=False, can_remove=False)
        return cls(
            status=status,
            can_post=obj.get("can_post_messages") is True,
            can_edit=obj.get("can_edit_messages") is True,
            can_remove=obj.get(CAN_REMOVE_POSTS_FLAG) is True,
        )


@dataclass(frozen=True)
class SentMessage:
    message_id: int
    chat_id: int

    @classmethod
    def from_result(cls, result: Any, method: str) -> SentMessage:
        obj = _obj(result, method, "message")
        chat = _obj(obj.get("chat"), method, "message.chat")
        return cls(message_id=_int(obj, "message_id", method), chat_id=_int(chat, "id", method))


@dataclass(frozen=True)
class InviteLink:
    """A ChatInviteLink this bot created. Only the link and its name are read (not its creator)."""

    invite_link: str
    name: str | None
    creates_join_request: bool
    is_revoked: bool

    @classmethod
    def from_result(cls, result: Any, method: str = "createChatInviteLink") -> InviteLink:
        obj = _obj(result, method, "invite link")
        link = _str(obj, "invite_link")
        if not link or not link.startswith("https://t.me/"):
            raise InvalidResponse(method, "invite link without a t.me link")
        return cls(
            invite_link=link,
            name=_str(obj, "name"),
            creates_join_request=obj.get("creates_join_request") is True,
            is_revoked=obj.get("is_revoked") is True,
        )


@dataclass(frozen=True)
class Update:
    """One update. Only the fields Proxy-Hub reads are parsed; ``raw`` is never stored."""

    update_id: int
    raw: dict[str, Any] = field(repr=False, compare=False)

    @classmethod
    def from_result(cls, item: Any) -> Update:
        obj = _obj(item, "getUpdates", "update")
        return cls(update_id=_int(obj, "update_id", "getUpdates"), raw=obj)

    def private_text(self) -> tuple[int, str] | None:
        """(chat id, text) of a text message in a private chat, else None."""
        message = self.raw.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or chat.get("type") != "private" or not isinstance(text, str):
            return None
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return None
        return chat_id, text


# --- client --------------------------------------------------------------------------------------

# Exceptions after which the request cannot have been processed by Telegram.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError)


def as_secret(token: SecretValue | str) -> SecretValue:
    """The token as a SecretValue, or :class:`InvalidToken` if it is not Bot API token shaped."""
    plain = token.reveal() if isinstance(token, SecretValue) else token
    if not TOKEN_RE.fullmatch(plain):
        raise InvalidToken
    return token if isinstance(token, SecretValue) else SecretValue(plain)


class BotClient:
    """Async Bot API client. Use as ``async with BotClient(token) as bot: ...``."""

    def __init__(
        self,
        token: SecretValue | str,
        *,
        base_url: str = API_BASE,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = as_secret(token)
        self._timeout = timeout
        install_log_redaction()
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": "Proxy-Hub"},
        )

    def __repr__(self) -> str:
        return "BotClient(token=***)"

    async def __aenter__(self) -> BotClient:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, params: dict[str, Any], *, read_timeout: float | None = None) -> Any:
        """POST one Bot API method with JSON parameters; returns ``result`` or raises.

        Private on purpose: callers use the typed methods below. A method outside
        :data:`ALLOWED_METHODS` raises ``ValueError`` before any request is built or sent.
        """
        if method not in ALLOWED_METHODS:
            raise ValueError("Bot API method not allowed in Proxy-Hub")
        timeout = self._timeout
        if read_timeout is not None:  # long poll: the server holds the answer up to its own timeout
            timeout = httpx.Timeout(self._timeout.connect or 10.0, read=read_timeout)
        try:
            response = await self._http.post(
                f"/bot{self._token.reveal()}/{method}", json=params, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise NetworkError(
                method, type(exc).__name__, maybe_sent=not isinstance(exc, _NOT_SENT)
            ) from None
        return parse_api_response(method, response.status_code, response.content)

    async def get_me(self) -> BotUser:
        return BotUser.from_result(await self._call("getMe", {}))

    async def get_updates(
        self, *, offset: int | None, long_poll_s: int, allowed_updates: Sequence[str], limit: int = 100
    ) -> list[Update]:
        wait = max(0, min(long_poll_s, MAX_LONG_POLL_SECONDS))
        params: dict[str, Any] = {
            "timeout": wait,
            "limit": limit,
            "allowed_updates": list(allowed_updates),
        }
        if offset is not None:
            params["offset"] = offset
        result = await self._call("getUpdates", params, read_timeout=wait + 15.0)
        if not isinstance(result, list):
            raise InvalidResponse("getUpdates", "result is not a list")
        return [Update.from_result(item) for item in result]

    async def send_message(
        self, chat_id: int | str, text: str, *, disable_notification: bool = False
    ) -> SentMessage:
        """HTML text, link previews off."""
        result = await self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
                "disable_notification": disable_notification,
            },
        )
        return SentMessage.from_result(result, "sendMessage")

    async def edit_message_text(self, chat_id: int | str, message_id: int, text: str) -> SentMessage:
        result = await self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )
        return SentMessage.from_result(result, "editMessageText")

    async def get_chat(self, chat_id: int | str) -> ChatInfo:
        return ChatInfo.from_result(await self._call("getChat", {"chat_id": chat_id}))

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> MemberRights:
        return MemberRights.from_result(
            await self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
        )

    async def get_chat_member_count(self, chat_id: int | str) -> int:
        """How many members a chat has (a channel's subscriber count). Only a number comes back."""
        result = await self._call("getChatMemberCount", {"chat_id": chat_id})
        if not isinstance(result, int) or isinstance(result, bool) or result < 0:
            raise InvalidResponse("getChatMemberCount", "result is not a count")
        return result

    async def create_chat_invite_link(self, chat_id: int | str, *, name: str) -> InviteLink:
        """A new, plain (no join request, no limit, no expiry) invite link named ``name``."""
        if not 1 <= len(name) <= INVITE_LINK_NAME_MAX:
            raise ValueError("invite link name must be 1 to 32 characters")
        result = await self._call(
            "createChatInviteLink", {"chat_id": chat_id, "name": name, "creates_join_request": False}
        )
        return InviteLink.from_result(result)
