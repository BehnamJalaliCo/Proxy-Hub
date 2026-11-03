"""Channel post template (decision D17): content + proxy block + the channel tag at the end.

``render_proxy_post`` is pure. Its output is Telegram HTML (parse_mode HTML) of at most 4096
characters, counted both as the HTML string and as Telegram counts the visible text (UTF-16 code
units after entity parsing), so it always fits one message.

* The optional content is HTML from the content engine (M3.1). It is rebuilt tag by tag: only the
  tags Telegram supports survive (anything else keeps its text and loses the tag), every text and
  attribute is escaped again, and links may only be http(s) or tg links. If the post would be too
  long, the content (and only the content) is shortened at a word boundary with an ellipsis and every
  open tag is closed.
* Each proxy is a Persian link word ("پروکسی ۱", ...) whose target is
  ``https://t.me/proxy?server=..&port=..&secret=..`` (core.telegram.org/api/links#mtproxy-links),
  with each value URL-encoded and the whole href HTML-escaped (docs/10 section 3.12: links hide
  behind words so the post stays short).
* The last line is the channel tag (owner's wish: the channel name under every post).
"""

from __future__ import annotations

import html
import ipaddress
import re
from collections.abc import Sequence
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urlencode

MAX_MESSAGE_CHARS: Final = 4096
PROXY_HEADER: Final = "📡 <b>پروکسی‌ها</b>"
PROXY_WORD: Final = "پروکسی"
SEPARATOR: Final = " • "
TAG_PREFIX: Final = "🔗 "
ELLIPSIS: Final = "…"

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_CHANNEL_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_HOST_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
# MTProto proxy secrets in links: 32 hex (classic), "dd" + 32 hex (random padding) or
# "ee" + 32 hex + the fake-TLS domain in hex (docs/11 E9).
_SECRET_RE = re.compile(r"^(?:dd)?[0-9a-f]{32}$|^ee[0-9a-f]{32}(?:[0-9a-f]{2}){1,255}$")

# Telegram's HTML subset (core.telegram.org/bots/api#html-style).
_SIMPLE_TAGS: Final = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "pre", "tg-spoiler"}
)
_LINK_SCHEMES = ("https://", "http://", "tg://")


class PostTooLong(ValueError):
    """Even without any content, the proxy block and the tag do not fit one message."""


def fa_digits(value: int | str) -> str:
    return str(value).translate(_FA_DIGITS)


def utf16_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def visible_text(html_text: str) -> str:
    """The text Telegram shows for an HTML message (tags removed, entities decoded)."""
    parser = _TextOnly()
    parser.feed(html_text)
    parser.close()
    return "".join(parser.parts)


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def channel_tag(username: str) -> str:
    m = _CHANNEL_RE.fullmatch(username.strip())
    if not m:
        raise ValueError("channel username must look like @name (5 to 32 letters, digits or _)")
    return f"@{m.group(1)}"


def _check_server(server: str) -> str:
    server = server.strip()
    try:
        return str(ipaddress.ip_address(server))
    except ValueError:
        pass
    host = server.rstrip(".").lower()
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_HOST_LABEL.fullmatch(lb) for lb in labels):
        raise ValueError("proxy server must be an IP address or a domain name")
    return host


def proxy_link(server: str, port: int, secret: str) -> str:
    """``https://t.me/proxy?...`` with every value validated and URL-encoded (not HTML-escaped)."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("proxy port must be 1 to 65535")
    secret = secret.strip().lower()
    if not _SECRET_RE.fullmatch(secret):
        raise ValueError("proxy secret must be hex (32 hex, dd + 32 hex, or ee + 32 hex + domain hex)")
    query = urlencode({"server": _check_server(server), "port": str(port), "secret": secret})
    return f"https://t.me/proxy?{query}"


def proxy_block(proxies: Sequence[tuple[str, int, str]]) -> str:
    links = [
        f'<a href="{html.escape(proxy_link(server, port, secret), quote=True)}">'
        f"{PROXY_WORD} {fa_digits(i)}</a>"
        for i, (server, port, secret) in enumerate(proxies, start=1)
    ]
    return f"{PROXY_HEADER}\n{SEPARATOR.join(links)}"


def render_proxy_post(
    content_html: str | None, proxies: Sequence[tuple[str, int, str]], channel_username: str
) -> str:
    """content (optional) + proxy block (if any proxies) + channel tag line; <= 4096 characters."""
    tail_parts = []
    if proxies:
        tail_parts.append(proxy_block(proxies))
    tail_parts.append(TAG_PREFIX + html.escape(channel_tag(channel_username), quote=False))
    tail = "\n\n".join(tail_parts)
    if len(tail) > MAX_MESSAGE_CHARS or utf16_len(visible_text(tail)) > MAX_MESSAGE_CHARS:
        raise PostTooLong("the proxy block and channel tag alone exceed one Telegram message")
    if not content_html or not content_html.strip():
        return tail
    joiner = "\n\n"
    budget_html = MAX_MESSAGE_CHARS - len(tail) - len(joiner)
    budget_visible = MAX_MESSAGE_CHARS - utf16_len(visible_text(tail)) - utf16_len(joiner)
    content = sanitize_html(content_html.strip(), max_html=budget_html, max_visible=budget_visible)
    if not content.strip():
        return tail
    return f"{content}{joiner}{tail}"


def sanitize_html(source: str, *, max_html: int, max_visible: int) -> str:
    """Rebuild ``source`` with Telegram's tags only, re-escaped, shortened to both budgets."""
    builder = _Builder(max_html=max_html, max_visible=max_visible)
    builder.feed(source)
    builder.close()
    return builder.result()


class _Builder(HTMLParser):
    def __init__(self, *, max_html: int, max_visible: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_html = max(0, max_html)
        self.max_visible = max(0, max_visible)
        self.out: list[str] = []
        self.html_len = 0
        self.visible = 0
        self.stack: list[str] = []  # open tag names, in order
        self.full = False

    # -- helpers --
    def _closing_len(self, extra: str | None = None) -> int:
        names = [*self.stack, extra] if extra else self.stack
        return sum(len(f"</{name}>") for name in names)

    def _emit(self, piece: str, visible: int = 0) -> None:
        self.out.append(piece)
        self.html_len += len(piece)
        self.visible += visible

    def _open_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        """The sanitized start tag, or None to drop the tag (its text is kept)."""
        values = {k.lower(): v for k, v in attrs}
        if tag in _SIMPLE_TAGS:
            return f"<{tag}>"
        if tag == "span":
            return '<span class="tg-spoiler">' if values.get("class") == "tg-spoiler" else None
        if tag == "blockquote":
            return "<blockquote expandable>" if "expandable" in values else "<blockquote>"
        if tag == "code":
            lang = values.get("class") or ""
            if re.fullmatch(r"language-[A-Za-z0-9_+-]{1,30}", lang):
                return f'<code class="{lang}">'
            return "<code>"
        if tag == "a":
            href = (values.get("href") or "").strip()
            if not href.lower().startswith(_LINK_SCHEMES) or any(c.isspace() for c in href):
                return None
            return f'<a href="{html.escape(href, quote=True)}">'
        if tag == "tg-emoji":
            emoji_id = values.get("emoji-id") or ""
            return f'<tg-emoji emoji-id="{emoji_id}">' if emoji_id.isdigit() else None
        return None

    # -- parser callbacks --
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.full:
            return
        tag = tag.lower()
        if tag == "br":
            self.handle_data("\n")
            return
        start = self._open_tag(tag, attrs)
        if start is None:
            return
        if self.html_len + len(start) + self._closing_len(tag) > self.max_html:
            self.full = True
            return
        self._emit(start)
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.handle_data("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.full:
            return
        tag = tag.lower()
        if tag not in self.stack:
            return  # stray or dropped tag
        while self.stack:
            name = self.stack.pop()
            self._emit(f"</{name}>")
            if name == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.full or not data:
            return
        escaped = html.escape(data, quote=False)
        if self._fits(escaped, utf16_len(data)):
            self._emit(escaped, utf16_len(data))
            return
        self.full = True
        cut = self._longest_prefix(data)
        if cut:
            self._emit(html.escape(cut, quote=False) + ELLIPSIS, utf16_len(cut) + 1)

    def _fits(self, escaped: str, visible: int) -> bool:
        return (
            self.html_len + len(escaped) + self._closing_len() <= self.max_html
            and self.visible + visible <= self.max_visible
        )

    def _longest_prefix(self, data: str) -> str:
        """Longest prefix (ending at a word boundary when possible) that fits with the ellipsis."""
        lo, hi = 0, len(data)
        while lo < hi:  # binary search on the prefix length
            mid = (lo + hi + 1) // 2
            if self._fits(html.escape(data[:mid], quote=False) + ELLIPSIS, utf16_len(data[:mid]) + 1):
                lo = mid
            else:
                hi = mid - 1
        prefix = data[:lo]
        space = max(prefix.rfind(" "), prefix.rfind("\n"))
        if space > len(prefix) // 2:
            prefix = prefix[:space]
        return prefix.rstrip()

    def result(self) -> str:
        closing = "".join(f"</{name}>" for name in reversed(self.stack))
        return "".join(self.out) + closing
