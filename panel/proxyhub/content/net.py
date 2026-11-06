"""Fetching untrusted pages, feeds and media (decision D51): https only, every hop checked.

Everything the content engine reads comes from places the owner names (a channel's public preview
page, a feed) and from addresses THOSE pages name (photo and video URLs). None of it is trusted, so
:class:`SafeHttp` is the only way the engine reaches the network:

* **https only**, on port 443, to a DNS name (no IP literal, no user:password@, no single-label or
  ``.local``/``.internal``/``.localhost`` name): :func:`check_url`.
* **Every connection is checked where it is made**: the connection pool's network backend
  (:class:`GuardedBackend`) resolves the name itself and connects only when EVERY address the name
  resolves to is a public unicast address (:func:`public_address`: no private, loopback,
  link-local, CGNAT, multicast, reserved, documentation or unspecified range; an IPv4 address
  inside IPv6 is judged as IPv4), and then connects to exactly the address it checked. So a
  redirect, a media URL from a feed, or a DNS answer that changes between two lookups (DNS
  rebinding) can never reach the panel's own network, the cloud metadata address or localhost.
  TLS is still verified for the NAME (SNI and certificate).
* **Redirects** are followed by hand, at most :data:`MAX_REDIRECTS`, and each new URL goes through
  :func:`check_url` again (and its connection through the backend).
* **Bounded**: a connect, read and total deadline per request; the body is read in chunks and
  refused as soon as it passes the caller's byte limit (``Content-Length`` is checked first, but
  never trusted). The request asks for no compression; a gzip or deflate body that comes anyway is
  inflated with the same byte limit (no decompression bomb). Nothing is cached, no cookie is kept,
  no environment proxy is used.
* Nothing fetched is ever executed or rendered here: callers parse it with a real parser
  (proxyhub/content/telegram_preview.py, rss.py) and keep plain text and checked URLs only.

Errors are :class:`FetchError` with a short code (``blocked_address``, ``too_big``, ``http_404``,
``timeout`` ...); the dashboard shows the code's Persian text (proxyhub/content/messages.py). Their
text never holds the fetched content.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
import zlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Final
from urllib.parse import quote, urljoin, urlsplit

import httpcore
import httpx

MAX_REDIRECTS: Final = 3
CONNECT_TIMEOUT_S: Final = 10.0
READ_TIMEOUT_S: Final = 20.0
PAGE_DEADLINE_S: Final = 30.0
MEDIA_DEADLINE_S: Final = 120.0
MAX_URL_CHARS: Final = 2048
USER_AGENT: Final = "Mozilla/5.0 (compatible; ProxyHub-content/1.0)"
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_URL_SAFE: Final = "/%:@!$&'()*+,;=-._~"
_PRIVATE_SUFFIXES: Final = (
    ".local",
    ".localhost",
    ".internal",
    ".home",
    ".lan",
    ".arpa",
    ".test",
    ".invalid",
)

Resolver = Callable[[str, int], Awaitable[list[str]]]
AddressCheck = Callable[[str], bool]


class FetchError(Exception):
    """A fetch that did not give a usable answer. ``code`` is short and safe to store and show."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- URLs and addresses ---------------------------------------------------------------------------


def check_url(url: str) -> str:
    """``url`` if it is an https URL on port 443 to a public-looking DNS name, else FetchError
    (``bad_url``). Fragments are dropped."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS or any(c.isspace() for c in url):
        raise FetchError("bad_url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError("bad_url") from None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        raise FetchError("bad_url")
    if port not in (None, 443):
        raise FetchError("bad_url")
    host = (parts.hostname or "").rstrip(".")
    if not valid_hostname(host):
        raise FetchError("bad_url")
    path = quote(parts.path or "/", safe=_URL_SAFE)
    query = f"?{quote(parts.query, safe=_URL_SAFE + '?')}" if parts.query else ""
    return f"https://{host}{path}{query}"


def valid_hostname(host: str) -> bool:
    """A DNS name of at least two labels, lower case, not an IP literal, not a private suffix."""
    if not host or len(host) > 253 or host != host.lower() or not host.isascii():
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return False
    if labels[-1].isdigit():
        return False
    return not (host == "localhost" or host.endswith(_PRIVATE_SUFFIXES))


def public_address(address: str) -> bool:
    """True only for a global unicast address (see the module doc)."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None:
            return public_address(str(inner))
        if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return False  # NAT64: the IPv4 address inside could be anything
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


async def system_resolver(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (A and AAAA), as text."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise FetchError("dns_failed") from None
    return [str(info[4][0]) for info in infos]


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """httpcore's network backend with the address check in front of every connection: the name
    is resolved here, every address must pass ``allow`` (:func:`public_address` in production),
    and the connection goes to the checked address itself (TLS still verifies the name)."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        allow: AddressCheck = public_address,
        connect_port: int | None = None,
    ) -> None:
        self._inner = httpcore.AnyIOBackend()
        self._resolver = resolver
        self._allow = allow
        self._connect_port = connect_port  # tests only: their local TLS server is not on 443

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if not valid_hostname(host.rstrip(".")) or port != 443:
            raise FetchError("blocked_address")
        addresses = await self._resolver(host, port)
        if not addresses or not all(self._allow(a) for a in addresses):
            raise FetchError("blocked_address")
        last: Exception | None = None
        for address in dict.fromkeys(addresses):  # in order, each once
            try:
                return await self._inner.connect_tcp(
                    address,
                    self._connect_port or port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        assert last is not None
        raise last

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise FetchError("blocked_address")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


# --- responses ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fetched:
    url: str  # the final URL (after redirects)
    content_type: str  # lower case, without parameters ("text/html")
    charset: str | None
    body: bytes


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return None


def _content_type(value: str | None) -> tuple[str, str | None]:
    if not value:
        return "", None
    main, *params = value.split(";")
    charset = None
    for param in params:
        key, _, val = param.strip().partition("=")
        if key.lower() == "charset" and val:
            charset = val.strip().strip('"').lower()[:40]
    return main.strip().lower(), charset


class _Limited:
    """The body, inflated if it came compressed, refused past ``limit`` bytes."""

    def __init__(self, limit: int, encoding: str | None) -> None:
        self.limit = limit
        self.parts: list[bytes] = []
        self.size = 0
        enc = (encoding or "identity").lower()
        if enc in ("identity", ""):
            self._inflate = None
        elif enc in ("gzip", "x-gzip"):
            self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif enc == "deflate":
            self._inflate = zlib.decompressobj()
        else:
            raise FetchError("bad_encoding")

    def add(self, chunk: bytes) -> None:
        if self._inflate is not None:
            try:
                chunk = self._inflate.decompress(chunk, self.limit + 1 - self.size)
            except zlib.error:
                raise FetchError("bad_encoding") from None
            if self._inflate.unconsumed_tail:
                raise FetchError("too_big")
        self.size += len(chunk)
        if self.size > self.limit:
            raise FetchError("too_big")
        self.parts.append(chunk)

    def body(self) -> bytes:
        return b"".join(self.parts)


class SafeHttp:
    """One connection pool for one round of the engine. ``async with SafeHttp() as http: ...``.

    ``resolver``, ``allow`` and ``ssl_context`` exist for the tests' local TLS server; production
    uses the system resolver, :func:`public_address`, the default certificate store and port 443."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        allow: AddressCheck = public_address,
        ssl_context: ssl.SSLContext | None = None,
        connect_port: int | None = None,
    ) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or httpx.create_ssl_context(),
            max_connections=4,
            max_keepalive_connections=2,
            keepalive_expiry=10.0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=GuardedBackend(resolver=resolver, allow=allow, connect_port=connect_port),
        )

    async def __aenter__(self) -> SafeHttp:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._pool.aclose()

    async def get(
        self,
        url: str,
        *,
        limit: int,
        accept: str = "*/*",
        deadline_s: float = PAGE_DEADLINE_S,
    ) -> Fetched:
        """GET ``url`` (redirects checked hop by hop). FetchError on anything but a 200 whose body
        fits ``limit`` bytes."""
        current = check_url(url)
        try:
            async with asyncio.timeout(deadline_s):
                for _ in range(MAX_REDIRECTS + 1):
                    result = await self._one(current, limit=limit, accept=accept)
                    if isinstance(result, Fetched):
                        return result
                    current = check_url(urljoin(current, result))
        except TimeoutError:
            raise FetchError("timeout") from None
        raise FetchError("too_many_redirects")

    async def _one(self, url: str, *, limit: int, accept: str) -> Fetched | str:
        """One request: the answer, or the Location of a redirect."""
        headers = [
            (b"User-Agent", USER_AGENT.encode()),
            (b"Accept", accept.encode()),
            (b"Accept-Encoding", b"identity"),
            (b"Accept-Language", b"fa,en;q=0.5"),
        ]
        timeouts = {
            "connect": CONNECT_TIMEOUT_S,
            "read": READ_TIMEOUT_S,
            "write": READ_TIMEOUT_S,
            "pool": 5.0,
        }
        try:
            async with self._pool.stream(
                "GET", url, headers=headers, extensions={"timeout": timeouts}
            ) as resp:
                status = resp.status
                raw = list(resp.headers)
                if status in _REDIRECTS:
                    location = _header(raw, b"location")
                    if not location:
                        raise FetchError("bad_redirect")
                    return location
                if status != 200:
                    raise FetchError(f"http_{status}" if 100 <= status <= 599 else "bad_status")
                length = _header(raw, b"content-length")
                if length is not None and (not length.isdigit() or int(length) > limit):
                    raise FetchError("too_big")
                body = _Limited(limit, _header(raw, b"content-encoding"))
                async for chunk in resp.aiter_stream():
                    body.add(chunk)
                content_type, charset = _content_type(_header(raw, b"content-type"))
                return Fetched(url=url, content_type=content_type, charset=charset, body=body.body())
        except FetchError:
            raise
        except (httpcore.ConnectTimeout, httpcore.ReadTimeout, httpcore.WriteTimeout, httpcore.PoolTimeout):
            raise FetchError("timeout") from None
        except httpcore.ConnectError as exc:
            cause = exc.__cause__ or exc.__context__
            raise FetchError("tls_failed" if isinstance(cause, ssl.SSLError) else "connect_failed") from None
        except (httpcore.ProtocolError, httpcore.NetworkError, httpcore.UnsupportedProtocol):
            raise FetchError("connect_failed") from None


def decode_text(fetched: Fetched) -> str:
    """The body as text (the declared charset, else UTF-8), undecodable bytes replaced."""
    charset = fetched.charset or "utf-8"
    try:
        return fetched.body.decode(charset, errors="replace")
    except LookupError:
        return fetched.body.decode("utf-8", errors="replace")
