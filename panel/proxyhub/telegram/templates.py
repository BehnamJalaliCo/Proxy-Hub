"""Channel post template (decision D17): content + proxy block + the channel tag at the end.

``render_proxy_post`` is pure. Its output is Telegram HTML (parse_mode HTML) of at most 4096
characters, counted both as the HTML string and as Telegram counts the visible text (UTF-16 code
units after entity parsing), so it always fits one message.

* The optional content is HTML from the content engine (M3.1). It is rebuilt tag by tag: only the
  tags Telegram supports survive (anything else keeps its text and loses the tag), every text and
  attribute is escaped again, and links may only be http(s) or tg links. If the post would be too
  long, the content (and only the content) is shortened at a word boundary with an ellipsis and every
  open tag is closed.
* Each proxy is a short Persian connect word ("⚡ اتصال ۱", three to a row) whose target is
  ``https://t.me/proxy?server=..&port=..&secret=..`` (core.telegram.org/api/links#mtproxy-links),
  with each value URL-encoded and the whole href HTML-escaped (docs/10 section 3.12: links hide
  behind words so the post stays short).
* The last line is the channel tag (owner's wish: the channel name under every post).

``render_post`` is the publisher's richer layout (posts team, M3.2-M3.4): a bold title and the
body in a blockquote, the MTProto links as connect words numbered by their place in the post, the
VLESS configs each in a code block (``<pre>``: Telegram draws a copy button on it, and a tap copies
it), then the channel tag. The owner (2026-11-05): no «کانفیگ N» next to a config. So a post never
shows a provider's label; the only text by a config is its end time and, for a REALITY config that
comes with its XHTTP link, a tiny transport marker ("TCP" / "XHTTP") as the code block's language,
which Telegram shows in the block's header. The name inside the config itself (``#@ipoxxo …``,
D41) is the provider's and stays.

A link that is no longer active keeps its place with a status mark and LOSES its link or config
(docs/04: nobody should tap or copy a dead config): a connect word is struck through behind the
status mark, a config's code block becomes its status line. When every link of a post is dead a
closing line points to the newer posts. A ``paused`` link (its provider has not listed it for a
while, see proxyhub/posts/publisher.py) keeps its place, without a strike, with a pause mark and
without its link; it is not dead and gets its link back when it is listed again. Everything shown
follows from the links' order, kinds, statuses, end times and markers alone, so the same inputs
always give the same text and a status edit is queued only when something really changed.
"""

from __future__ import annotations

import html
import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Final, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from proxyhub.telegram.schedule import TEHRAN

MAX_MESSAGE_CHARS: Final = 4096
PROXY_HEADER: Final = "📡 <b>پروکسی‌ها</b>"
# An MTProto link is a connect word: "⚡ اتصال ۱". No-break spaces inside a word, so a narrow screen
# never wraps between the mark, the word and its number; two plain spaces between words, three
# words to a row (a grid of buttons on a phone).
CONNECT_WORD: Final = "اتصال"
LIVE_MARK: Final = "⚡"
NBSP: Final = "\u00a0"
WORD_GAP: Final = "  "
WORDS_PER_ROW: Final = 3
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


def connect_word(n: int) -> str:
    """The visible word of the n-th MTProto link of a post: "اتصال ۱" (with a no-break space)."""
    return f"{CONNECT_WORD}{NBSP}{fa_digits(n)}"


def live_word(url: str, n: int) -> str:
    """``⚡ <a href=url>اتصال n</a>``: the mark stays outside the link (a custom emoji may not sit
    inside a text link, see proxyhub/posts/entities.py)."""
    return f'{LIVE_MARK}{NBSP}<a href="{html.escape(url, quote=True)}">{connect_word(n)}</a>'


def word_rows(words: Sequence[str]) -> str:
    """The connect words, :data:`WORDS_PER_ROW` to a row."""
    return "\n".join(WORD_GAP.join(words[i : i + WORDS_PER_ROW]) for i in range(0, len(words), WORDS_PER_ROW))


def proxy_block(proxies: Sequence[tuple[str, int, str]]) -> str:
    words = [
        live_word(proxy_link(server, port, secret), i)
        for i, (server, port, secret) in enumerate(proxies, start=1)
    ]
    return f"{PROXY_HEADER}\n{word_rows(words)}"


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
    if not visible_text(content).strip():
        return tail
    return f"{content}{joiner}{tail}"


def sanitize_html(
    source: str, *, max_html: int, max_visible: int, allowed: frozenset[str] | None = None
) -> str:
    """Rebuild ``source`` with Telegram's tags only, re-escaped, shortened to both budgets.
    ``allowed`` narrows the tags further (any other tag keeps its text and loses the tag)."""
    builder = _Builder(max_html=max_html, max_visible=max_visible, allowed=allowed)
    builder.feed(source)
    builder.close()
    return builder.result()


class _Builder(HTMLParser):
    def __init__(self, *, max_html: int, max_visible: int, allowed: frozenset[str] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.max_html = max(0, max_html)
        self.max_visible = max(0, max_visible)
        self.allowed = allowed
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
        if self.allowed is not None and tag not in self.allowed:
            return None
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


# --- the publisher's post (posts team) -----------------------------------------------------------

LinkKind = Literal["mtproto", "reality"]
LinkStatus = Literal["active", "expired", "full", "burned"]
# How a post shows a link: a link status, or "paused" (shown only; never stored as a status).
ShownStatus = Literal["active", "paused", "expired", "full", "burned"]

MTPROTO_HEADER: Final = "📡 <b>پروکسی تلگرام</b> — روی هر کدام بزنید"
REALITY_HEADER: Final = "🔐 <b>کانفیگ VLESS</b> — برای کپی روی متن بزنید"
ALL_DEAD_LINE: Final = "♻️ <i>کانفیگ‌های این پست دیگر کار نمیکنند؛ کانفیگ تازه در پست‌های جدید کانال است.</i>"
STATUS_MARKS: Final = {"expired": "⌛️", "full": "🔴", "burned": "🚫"}
PAUSED_MARK: Final = "⏸"
PAUSED_LINE: Final = "⏸ فعلاً در دسترس نیست"
# Left-to-right mark before the @name: inside a right-to-left post the "@" stays in front of the
# name ("@ipoxxo", never "ipoxxo@") whatever direction a client gives the line. A middle dot is
# never used as a separator: next to Persian digits it reads as a zero ("۱ ·" looks like "۱۰").
LRM: Final = "\u200e"
MAX_LABEL_CHARS: Final = 32
MAX_TITLE_CHARS: Final = 120
MAX_VLESS_CHARS: Final = 2048
# Tags an owner may use in a content body. A blockquote or pre would nest inside the post's own
# blockquote, which Telegram does not allow, so their text is kept and the tag dropped. No
# tg-emoji: a bot may only send custom emoji in some cases (core.telegram.org/bots/api#html-style),
# and a post the bot cannot send is never sent again (at most once); a later operator-account
# poster (proxyhub/posts/poster.py) may allow it for its own posts.
BODY_TAGS: Final = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "a", "tg-spoiler", "span"}
)
_VLESS_RE = re.compile(r"^vless://[\x21-\x7e]+$")
_SPACES = re.compile(r"\s+")
# A config's transport marker is its code block's language: letters, digits, _ + - only.
_VARIANT_RE = re.compile(r"^[A-Za-z0-9_+-]{1,16}$")


@dataclass(frozen=True)
class StatusTexts:
    """The line a dead Reality config gets (decision D5; owner-editable in the dashboard)."""

    expired: str = "⌛️ زمان این کانفیگ تمام شد"
    full: str = "🔴 ظرفیت این کانفیگ پر شد"
    burned: str = "🚫 این کانفیگ از دسترس خارج شد"

    def for_status(self, status: str) -> str:
        if status == "expired":
            return self.expired
        if status == "full":
            return self.full
        return self.burned


@dataclass(frozen=True)
class PostLink:
    """One link as a post shows it. ``url`` is a checked URL (:func:`mtproto_url`,
    :func:`vless_url`) and is only used while ``status`` is active. No label: a post never shows a
    provider's label (the module doc). ``variant``: a config's tiny transport marker ("TCP",
    "XHTTP"), or None. ``companion``: this config link is another way into the config of the link
    right before it (a REALITY config's XHTTP link after its TCP link), so the two stay together and
    the end time is not repeated. proxyhub/posts/links.py :func:`transport_marks` sets both."""

    kind: LinkKind
    status: ShownStatus
    url: str | None = None
    expires_at: datetime | None = None
    variant: str | None = None
    companion: bool = False

    @property
    def live(self) -> bool:
        return self.status == "active" and self.url is not None

    @property
    def dead(self) -> bool:
        return not self.live and self.status != "paused"


@dataclass(frozen=True)
class RenderedPost:
    text: str
    content_html: str  # the content as it went in (shortened when it had to be)


def mtproto_url(url: str) -> str:
    """A provider's ``https://t.me/proxy?server=..&port=..&secret=..`` checked and rebuilt in
    canonical form (every value validated again). Raises ValueError (the message has no secret)."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or parts.netloc.lower() != "t.me" or parts.path != "/proxy" or parts.fragment:
        raise ValueError("not an https://t.me/proxy link")
    query = parse_qs(parts.query, keep_blank_values=True)
    if set(query) != {"server", "port", "secret"} or any(len(v) != 1 for v in query.values()):
        raise ValueError("the proxy link needs exactly server, port and secret")
    port = query["port"][0]
    if not port.isascii() or not port.isdigit() or len(port) > 5:
        raise ValueError("proxy port must be a number")
    return proxy_link(query["server"][0], int(port), query["secret"][0])


def vless_url(url: str) -> str:
    """A ``vless://`` share link: printable ASCII only (no spaces, no line breaks), bounded."""
    url = url.strip()
    if len(url) > MAX_VLESS_CHARS or not _VLESS_RE.fullmatch(url):
        raise ValueError("not a vless:// link of printable ASCII")
    return url


def clean_label(label: str) -> str:
    """One short line of plain text: a link's label for the owner (dashboard, logs). A post never
    shows it."""
    text = _SPACES.sub(" ", label).strip()
    if not text:
        raise ValueError("empty label")
    return text[:MAX_LABEL_CHARS]


def clean_title(title: str) -> str:
    text = _SPACES.sub(" ", title).strip()
    if not text:
        raise ValueError("empty title")
    return text[:MAX_TITLE_CHARS]


def sanitize_body(body_html: str, *, max_html: int, max_visible: int) -> str:
    """An owner's body rebuilt with :data:`BODY_TAGS` only (see :func:`sanitize_html`)."""
    return sanitize_html(body_html.strip(), max_html=max_html, max_visible=max_visible, allowed=BODY_TAGS)


def compose_content(title: str | None, body_html: str | None) -> str:
    """Bold title, then the body in a blockquote (the content part of a post)."""
    parts: list[str] = []
    if title and title.strip():
        parts.append(f"<b>{html.escape(clean_title(title), quote=False)}</b>")
    if body_html and body_html.strip():
        body = sanitize_body(body_html, max_html=MAX_MESSAGE_CHARS, max_visible=MAX_MESSAGE_CHARS)
        if visible_text(body).strip():
            parts.append(f"<blockquote>{body}</blockquote>")
    return "\n\n".join(parts)


# datetime.weekday(): Monday is 0.
WEEKDAYS_FA: Final = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")


def _tehran_clock(at: datetime) -> str:
    return fa_digits(at.astimezone(TEHRAN).strftime("%H:%M"))


def tehran_until(at: datetime) -> str:
    """Weekday and clock in Tehran ("سه‌شنبه ساعت ۰۹:۰۰"): a config lasts at most a day or so,
    so the weekday makes the end unambiguous for whoever reads the post later."""
    local = at.astimezone(TEHRAN)
    return f"{WEEKDAYS_FA[local.weekday()]} ساعت {_tehran_clock(local)}"


def _mtproto_word(link: PostLink, n: int) -> str:
    """A live link is a connect word; a paused one keeps its word and a pause mark, without the
    link; a dead one is struck through behind its status mark, without the link."""
    if link.live:
        assert link.url is not None
        return live_word(link.url, n)
    if link.status == "paused":
        return f"{PAUSED_MARK}{NBSP}{connect_word(n)}"
    mark = STATUS_MARKS.get(link.status, STATUS_MARKS["burned"])
    return f"{mark}{NBSP}<s>{connect_word(n)}</s>"


def _mtproto_block(links: Sequence[PostLink]) -> str:
    """Numbered by their place among the post's MTProto links, which never changes (an edit
    renders a post's links in the order they were posted)."""
    words = [_mtproto_word(link, n) for n, link in enumerate(links, start=1)]
    return f"{MTPROTO_HEADER}\n{word_rows(words)}"


def _variant(link: PostLink) -> str | None:
    return link.variant if link.variant and _VARIANT_RE.fullmatch(link.variant) else None


def config_block(url: str, variant: str | None = None) -> str:
    """A config in a code block: Telegram draws a copy button on it and a tap copies it. The
    transport marker, if any, is the block's language, shown in the block's header."""
    body = html.escape(url, quote=False)
    if variant and _VARIANT_RE.fullmatch(variant):
        return f'<pre><code class="language-{variant}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _until(link: PostLink) -> str | None:
    return f"⏳ اعتبار تا {tehran_until(link.expires_at)}" if link.expires_at is not None else None


def _ended_line(link: PostLink, texts: StatusTexts) -> str:
    """Where a config that is not live was: its status line (and its marker), nothing to copy."""
    if link.status == "paused":
        line = PAUSED_LINE
    else:
        line = html.escape(
            texts.for_status(link.status if link.status != "active" else "burned"), quote=False
        )
    variant = _variant(link)
    return f"{line} — {variant}" if variant else line


def _reality_block(links: Sequence[PostLink], texts: StatusTexts) -> str:
    """Each config: its end time, then its code block (a companion link's block right under it,
    without the end time again). A config that is not live: one status line in its place."""
    groups: list[str] = []
    before: PostLink | None = None
    for link in links:
        together = link.companion and before is not None and before.live == link.live
        if link.live:
            assert link.url is not None
            block = config_block(link.url, _variant(link))
            head = _until(link)
            if together and before is not None and _until(before) == head:
                groups[-1] += "\n" + block
            else:
                groups.append(f"{head}\n{block}" if head else block)
        elif together:
            groups[-1] += "\n" + _ended_line(link, texts)
        else:
            groups.append(_ended_line(link, texts))
        before = link
    return REALITY_HEADER + "\n" + "\n\n".join(groups)


def post_tail(links: Sequence[PostLink], channel_username: str, texts: StatusTexts) -> str:
    """Everything under the content: the link blocks, the closing line, the channel tag."""
    mtproto = [x for x in links if x.kind == "mtproto"]
    reality = [x for x in links if x.kind == "reality"]
    parts: list[str] = []
    if mtproto:
        parts.append(_mtproto_block(mtproto))
    if reality:
        parts.append(_reality_block(reality, texts))
    if links and all(x.dead for x in links):
        parts.append(ALL_DEAD_LINE)
    parts.append(TAG_PREFIX + LRM + html.escape(channel_tag(channel_username), quote=False))
    return "\n\n".join(parts)


def fits(text: str) -> bool:
    return len(text) <= MAX_MESSAGE_CHARS and utf16_len(visible_text(text)) <= MAX_MESSAGE_CHARS


def render_post(
    content_html: str | None,
    links: Sequence[PostLink],
    channel_username: str,
    texts: StatusTexts | None = None,
) -> RenderedPost:
    """content (optional) + link blocks + channel tag, <= 4096 characters. The content (and only
    the content) is shortened when the whole does not fit; PostTooLong if the tail alone does not."""
    tail = post_tail(links, channel_username, texts or StatusTexts())
    if not fits(tail):
        raise PostTooLong("the link blocks and channel tag alone exceed one Telegram message")
    if not content_html or not content_html.strip():
        return RenderedPost(text=tail, content_html="")
    joiner = "\n\n"
    content = sanitize_html(
        content_html.strip(),
        max_html=MAX_MESSAGE_CHARS - len(tail) - len(joiner),
        max_visible=MAX_MESSAGE_CHARS - utf16_len(visible_text(tail)) - utf16_len(joiner),
    )
    if not visible_text(content).strip():
        return RenderedPost(text=tail, content_html="")
    return RenderedPost(text=f"{content}{joiner}{tail}", content_html=content)
