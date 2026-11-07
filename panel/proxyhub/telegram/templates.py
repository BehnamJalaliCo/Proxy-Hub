"""Channel post template (decision D17): content + proxy block + the channel tag at the end.

``render_proxy_post`` is pure. Its output is Telegram HTML (parse_mode HTML) of at most 4096
characters, counted both as the HTML string and as Telegram counts the visible text (UTF-16 code
units after entity parsing), so it always fits one message.

* The optional content is HTML from the content engine (M3.1). It is rebuilt tag by tag: only the
  tags Telegram supports survive (anything else keeps its text and loses the tag), every text and
  attribute is escaped again, and links may only be http(s) or tg links. If the post would be too
  long, the content (and only the content) is shortened at a word boundary with an ellipsis and every
  open tag is closed.
* Each proxy is the short Persian word «پروکسی ۱» (decision D48: two to a row, " • " between them)
  whose target is ``https://t.me/proxy?server=..&port=..&secret=..``
  (core.telegram.org/api/links#mtproxy-links), with each value URL-encoded and the whole href
  HTML-escaped (docs/10 section 3.12: links hide behind words so the post stays short).
* The last line is the channel tag (owner's wish: the channel name under every post).

``render_post`` is the publisher's compact layout (posts team, M3.2-M3.4; decision D48, 2026-11-06:
the link part stays short so news can sit above it):

* a bold title line with one emoji (:data:`TITLE_MARK` when the title has none), and the body as
  plain text under it (:func:`compose_content`; the owner, 2026-11-07: "معمولی و تمیز", no quote);
* the MTProto links as words, two to a row with " • " between them, and no header (the words say
  what they are). A link's word is «پروکسی», or an operator's name («همراه اول», «رایتل»; never
  «ایرانسل», the owner's choice) when the post was made with the panel's own fresh probe from that
  operator's network (decision D50, proxyhub/posts/operators.py; stored with the post, so an edit
  never renames a link). Operator-named links come first (MCI, then Rightel), then «پروکسی». A word that shows
  once has no number; a word that repeats is numbered by its place among the same words
  («همراه اول ۱», «همراه اول ۲», «پروکسی ۱» …), Persian digits, no-break spaces inside;
* each VLESS config as ONE short line, «🔒 کانفیگ V2Ray — اعتبار ۴۸ ساعت» (the owner, 2026-11-07: the
  period, not a day and a clock; :data:`CONFIG_VALIDITY_HOURS`), and under it
  each of its links (the config and, for a REALITY config, its XHTTP link) as a tiny label line
  ("TCP" / "XHTTP") over its OWN collapsed quote (``<blockquote expandable>``) that holds nothing but
  that link as inline code. One quote, one config: a tap on the code, or copying the quote, gives
  exactly one config (the owner on iPhone, 2026-11-06: a quote that held both gave V2Box only one).
  Telegram shows about three lines of a collapsed quote. No ``<pre>`` there: Telegram nests neither
  a pre nor a quote inside a quote, while inline code inside a quote is meant to be;
* a short line «—», then the channel tag (decision D50, the competitor's sign-off).

The owner (2026-11-05): no «کانفیگ N» next to a config, so a post never shows a provider's label.
The name inside the config itself (``#@ipoxxo …``, D41) is the provider's and stays.

A link that is no longer active keeps its place with a status mark and LOSES its link or config
(docs/04: nobody should tap or copy a dead config): a proxy word is struck through behind the status
mark; a link of a config that is not live keeps its label and shows its status line, outside any
quote, where its quote was; when no link of the pair is left, the lock line and the quotes become one
status line. When every link
of a post is dead a closing line points to the newer posts. A ``paused`` link (its provider has not
listed it for a while, see proxyhub/posts/publisher.py) keeps its place, without a strike, with a
pause mark and without its link; it is not dead and gets its link back when it is listed again.
Everything shown follows from the links' order, kinds, statuses, end times and markers alone, so the
same inputs always give the same text and a status edit is queued only when something really
changed (which is also how a sent post reaches a new layout: its next render differs once).
"""

from __future__ import annotations

import html
import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from html.parser import HTMLParser
from typing import Final, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from proxyhub.posts.entities import emoji_in
from proxyhub.telegram.schedule import TEHRAN

MAX_MESSAGE_CHARS: Final = 4096
# A photo's or video's caption (decision D51). Telegram allows 1024 characters after entities
# parsing to a bot (core.telegram.org/bots/api#sendphoto) and to a user account without Premium
# (``caption_length_limit_default``, core.telegram.org/api/config), 4096 to a Premium account
# (``caption_length_limit_premium``). Every media post uses 1024, whoever sends it: a post the
# operator account (Premium) cannot send goes to the bot with the very same caption
# (proxyhub/posts/poster.py), and a lapsed Premium must not break a later caption edit.
CAPTION_LIMIT: Final = 1024
# Our own bound on a caption's HTML (the proxy words' hidden URLs count here, not in Telegram's limit).
MAX_CAPTION_HTML: Final = 8192
# An MTProto link is the word «پروکسی ۱» (D48). A no-break space between the word and its number
# (and between a status mark and the word), so a narrow screen never wraps inside a word; two words
# to a row with a bullet between them (a grid on a phone).
PROXY_WORD: Final = "پروکسی"
NBSP: Final = "\u00a0"
WORD_SEPARATOR: Final = " • "
WORDS_PER_ROW: Final = 2
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


def proxy_word(n: int) -> str:
    """The visible word of the n-th MTProto link of a post: «پروکسی ۱» (with a no-break space)."""
    return f"{PROXY_WORD}{NBSP}{fa_digits(n)}"


def numbered(words: Sequence[str]) -> list[str]:
    """The words as a post shows them (D50): a word that shows once has no number; a word that
    repeats is numbered by its place among the same words. No-break spaces inside each, so a word
    never wraps."""
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    seen: dict[str, int] = {}
    out: list[str] = []
    for word in words:
        seen[word] = seen.get(word, 0) + 1
        text = word.replace(" ", NBSP)
        out.append(text if counts[word] == 1 else f"{text}{NBSP}{fa_digits(seen[word])}")
    return out


def link_word(url: str, text: str) -> str:
    """``<a href=url>text</a>``: the whole word is the link, with no mark (D48)."""
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def live_word(url: str, n: int) -> str:
    """``<a href=url>پروکسی n</a>``."""
    return link_word(url, proxy_word(n))


def word_rows(words: Sequence[str]) -> str:
    """The proxy words, :data:`WORDS_PER_ROW` to a row, :data:`WORD_SEPARATOR` between two."""
    return "\n".join(
        WORD_SEPARATOR.join(words[i : i + WORDS_PER_ROW]) for i in range(0, len(words), WORDS_PER_ROW)
    )


def proxy_block(proxies: Sequence[tuple[str, int, str]]) -> str:
    """The proxy words alone: they say «پروکسی» themselves, so no header (D48)."""
    links = [proxy_link(server, port, secret) for server, port, secret in proxies]
    words = numbered([PROXY_WORD] * len(links))
    return word_rows([link_word(url, text) for url, text in zip(links, words, strict=True)])


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

# The one emoji of the title line when the title brings none (D48); the fallback title has it too.
TITLE_MARK: Final = "📡"
# The one line above a config's quote (D48): «🔒 کانفیگ V2Ray — اعتبار ۴۸ ساعت».
REALITY_MARK: Final = "🔒"
REALITY_NAME: Final = "کانفیگ V2Ray"
# A config's lifetime as the post states it: the default of proxyhub/reality/settings.py ``cap_hours``
# (tests/test_posts_units.py keeps the two equal). A config that ends sooner shows its status mark.
CONFIG_VALIDITY_HOURS: Final = 48
# The line over the channel tag, under the link part (decision D50).
SIGN_OFF: Final = "—"
# An MTProto link's word when the post names it after an operator (decision D50), in the order a
# post shows them; any other link is «پروکسی».
OPERATOR_WORDS: Final[dict[str, str]] = {"mci": "همراه اول", "irancell": "ایرانسل", "rightel": "رایتل"}
ALL_DEAD_LINE: Final = "♻️ <i>کانفیگ‌های این پست دیگر کار نمیکنند؛ کانفیگ تازه در پست‌های جدید کانال است.</i>"
STATUS_MARKS: Final = {"expired": "⌛️", "full": "🔴", "burned": "🚫"}
PAUSED_MARK: Final = "⏸"
PAUSED_LINE: Final = "⏸ فعلاً در دسترس نیست"
# Left-to-right mark before the @name: inside a right-to-left post the "@" stays in front of the
# name ("@ipoxxo", never "ipoxxo@") whatever direction a client gives the line. A middle dot is
# never used as a separator: next to Persian digits it reads as a zero ("۱ ·" looks like "۱۰"); the
# words of the proxy grid are separated by a bullet with a space on each side (D48).
LRM: Final = "\u200e"
MAX_LABEL_CHARS: Final = 32
MAX_TITLE_CHARS: Final = 120
MAX_VLESS_CHARS: Final = 2048
# Tags an owner may use in a content body. No blockquote or pre: the body is plain text under the
# title (and Telegram nests neither inside a quote), so their text is kept and the tag dropped. No
# tg-emoji: a bot may only send custom emoji in some cases (core.telegram.org/bots/api#html-style),
# and a post the bot cannot send is never sent again (at most once); a later operator-account
# poster (proxyhub/posts/poster.py) may allow it for its own posts.
BODY_TAGS: Final = frozenset(
    {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "a", "tg-spoiler", "span"}
)
_VLESS_RE = re.compile(r"^vless://[\x21-\x7e]+$")
_SPACES = re.compile(r"\s+")
# A config's transport marker is the label line over its quote ("TCP"): letters, digits, _ + - only.
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
    "XHTTP"), shown as the label line over its quote, or None. ``companion``: this config link is
    another way into the config of the link right before it (a REALITY config's XHTTP link after its
    TCP link), so the two share one lock line (each still in its own quote). proxyhub/posts/links.py
    :func:`transport_marks` sets both."""

    kind: LinkKind
    status: ShownStatus
    url: str | None = None
    expires_at: datetime | None = None
    variant: str | None = None
    companion: bool = False
    # The operator a post names this MTProto link after (decision D50), stored with the post.
    operator: str | None = None

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


def title_line(title: str) -> str:
    """The bold title line of a post, with one emoji (D48): the title's own, else :data:`TITLE_MARK`
    in front of it. An owner's title may already start with an emoji (the dashboard asks for one;
    every seeded title has one), and then nothing is added."""
    text = clean_title(title)
    if next(emoji_in(text), None) is None:
        text = f"{TITLE_MARK} {text}"
    return f"<b>{html.escape(text, quote=False)}</b>"


def compose_content(title: str | None, body_html: str | None) -> str:
    """Bold title line, then the body as plain text under it (the content part of a post)."""
    parts: list[str] = []
    if title and title.strip():
        parts.append(title_line(title))
    if body_html and body_html.strip():
        body = sanitize_body(body_html, max_html=MAX_MESSAGE_CHARS, max_visible=MAX_MESSAGE_CHARS)
        if visible_text(body).strip():
            parts.append(body)
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


def _mtproto_word(link: PostLink, text: str) -> str:
    """A live link is its word; a paused one keeps its word and a pause mark, without the link; a
    dead one is struck through behind its status mark, without the link. An operator's name stays
    in every state (D50)."""
    if link.live:
        assert link.url is not None
        return link_word(link.url, text)
    if link.status == "paused":
        return f"{PAUSED_MARK}{NBSP}{text}"
    mark = STATUS_MARKS.get(link.status, STATUS_MARKS["burned"])
    return f"{mark}{NBSP}<s>{text}</s>"


def _word_of(link: PostLink) -> str:
    return OPERATOR_WORDS.get(link.operator or "", PROXY_WORD)


def _word_rank(link: PostLink) -> int:
    order = list(OPERATOR_WORDS)
    return order.index(link.operator) if link.operator in OPERATOR_WORDS else len(order)


def _mtproto_block(links: Sequence[PostLink]) -> str:
    """The proxy grid (D50): operator-named links first (MCI, Rightel, Irancell), then «پروکسی»,
    each group in the order the links were posted, which never changes (an edit renders a post's
    links in that order, with the names stored with them). No header: the words say what they are."""
    ordered = sorted(links, key=_word_rank)  # stable: the posted order within a word
    words = numbered([_word_of(link) for link in ordered])
    return word_rows([_mtproto_word(link, text) for link, text in zip(ordered, words, strict=True)])


def _marker(variant: str | None) -> str | None:
    """A transport marker only when it is a plain word (anything else shows no label)."""
    return variant if variant and _VARIANT_RE.fullmatch(variant) else None


def reality_line(until: datetime | None) -> str:
    """The one line above a config's quote: «🔒 کانفیگ V2Ray — اعتبار ۴۸ ساعت» for a config with an
    end (the period, not the end's day and clock: the owner, 2026-11-07)."""
    head = f"{REALITY_MARK} {REALITY_NAME}"
    return f"{head} — اعتبار {fa_digits(CONFIG_VALIDITY_HOURS)} ساعت" if until is not None else head


def _labelled(variant: str | None, body: str) -> str:
    """``body`` under its transport marker's label line ("TCP"), or alone without a marker."""
    marker = _marker(variant)
    return f"{marker}\n{body}" if marker else body


def config_quote(url: str, variant: str | None = None) -> str:
    """One link of a config: its transport marker as a tiny label line ("TCP"), then its OWN collapsed
    quote holding nothing but the link as inline code. Copying the quote or tapping the code gives
    exactly this one config. Never a ``<pre>``: Telegram nests no pre inside a quote."""
    code = f"<code>{html.escape(url, quote=False)}</code>"
    return _labelled(variant, f"<blockquote expandable>{code}</blockquote>")


def _status_line(link: PostLink, texts: StatusTexts) -> str:
    """The line a config that is not live shows (decision D5); an active link without a URL counts
    as burned."""
    if link.status == "paused":
        return PAUSED_LINE
    return html.escape(texts.for_status(link.status if link.status != "active" else "burned"), quote=False)


def _config_groups(links: Sequence[PostLink]) -> list[list[PostLink]]:
    """The configs in order, each with its companion (the XHTTP link right after its TCP link)."""
    groups: list[list[PostLink]] = []
    for link in links:
        if link.companion and groups and len(groups[-1]) == 1:
            groups[-1].append(link)
        else:
            groups.append([link])
    return groups


def _config_group(group: Sequence[PostLink], texts: StatusTexts) -> str:
    """One config: its lock line (with the earliest end of its live links; a config and its XHTTP
    link end together), then each link under its label, a live one in its own collapsed quote, one
    that is not live as its status line outside any quote. With nothing live left: ONE status line
    and no quote (the first ended link's status; the pause line only when every link is merely
    paused)."""
    live = [x for x in group if x.live]
    if not live:
        ended = [x for x in group if x.status != "paused"]
        return _status_line(ended[0] if ended else group[0], texts)
    ends = [x.expires_at for x in live if x.expires_at is not None]
    entries = [
        config_quote(x.url, x.variant) if x.live and x.url else _labelled(x.variant, _status_line(x, texts))
        for x in group
    ]
    links = "\n\n".join(entries)
    return f"{reality_line(min(ends) if ends else None)}\n{links}"


def _reality_block(links: Sequence[PostLink], texts: StatusTexts) -> str:
    """Every config of the post (D48 puts one in a new post; an older post may carry more), each as
    its line and its quote, in the order they were posted."""
    return "\n\n".join(_config_group(group, texts) for group in _config_groups(links))


def post_tail(links: Sequence[PostLink], channel_username: str, texts: StatusTexts) -> str:
    """Everything under the content: the proxy grid, the configs, the closing line, then «—» and the
    channel tag (D50)."""
    mtproto = [x for x in links if x.kind == "mtproto"]
    reality = [x for x in links if x.kind == "reality"]
    parts: list[str] = []
    if mtproto:
        parts.append(_mtproto_block(mtproto))
    if reality:
        parts.append(_reality_block(reality, texts))
    if links and all(x.dead for x in links):
        parts.append(ALL_DEAD_LINE)
    parts.append(f"{SIGN_OFF}\n{TAG_PREFIX}{LRM}{html.escape(channel_tag(channel_username), quote=False)}")
    return "\n\n".join(parts)


def _limits(caption: bool) -> tuple[int, int]:
    """(visible UTF-16 units, HTML characters) of a text message or of a media caption."""
    return (CAPTION_LIMIT, MAX_CAPTION_HTML) if caption else (MAX_MESSAGE_CHARS, MAX_MESSAGE_CHARS)


def fits(text: str, *, caption: bool = False) -> bool:
    visible, html_limit = _limits(caption)
    return len(text) <= html_limit and utf16_len(visible_text(text)) <= visible


def later_states(links: Sequence[PostLink]) -> list[list[PostLink]]:
    """The link part as it is now and in its longest later states (a dead proxy word carries a mark
    and a strike; a config that ended shows a status line instead of its quote, and with every link
    dead a closing line comes): every MTProto link dead, and every link dead with each status."""
    out = [list(links)]
    for status in ("expired", "full", "burned"):
        out.append([replace(x, status=status, url=None) if x.kind == "mtproto" else x for x in links])
        out.append([replace(x, status=status, url=None) for x in links])
    return out


def tail_always_fits(
    links: Sequence[PostLink],
    channel_username: str,
    texts: StatusTexts | None = None,
    *,
    caption: bool = False,
) -> bool:
    """The link part fits now AND in every later state (:func:`later_states`), so a status edit of
    the post can always show it, shortening only the content."""
    texts = texts or StatusTexts()
    return all(fits(post_tail(v, channel_username, texts), caption=caption) for v in later_states(links))


def render_post(
    content_html: str | None,
    links: Sequence[PostLink],
    channel_username: str,
    texts: StatusTexts | None = None,
    *,
    caption: bool = False,
) -> RenderedPost:
    """content (optional) + link blocks + channel tag, within one message (4096) or, with
    ``caption``, within a media caption (:data:`CAPTION_LIMIT`). The content (and only the content)
    is shortened when the whole does not fit; PostTooLong if the tail alone does not."""
    visible_max, html_max = _limits(caption)
    tail = post_tail(links, channel_username, texts or StatusTexts())
    if not fits(tail, caption=caption):
        raise PostTooLong("the link blocks and channel tag alone exceed one Telegram message or caption")
    if not content_html or not content_html.strip():
        return RenderedPost(text=tail, content_html="")
    joiner = "\n\n"
    content = sanitize_html(
        content_html.strip(),
        max_html=html_max - len(tail) - len(joiner),
        max_visible=visible_max - utf16_len(visible_text(tail)) - utf16_len(joiner),
    )
    if not visible_text(content).strip():
        return RenderedPost(text=tail, content_html="")
    return RenderedPost(text=f"{content}{joiner}{tail}", content_html=content)
