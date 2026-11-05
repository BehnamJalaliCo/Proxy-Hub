"""A post's Telegram HTML as plain text + entities, and the owner's animated emoji (decision D38).

The bot sends a post as HTML (parse_mode HTML) and Telegram turns it into text + entities. The
operator account (a Premium user account, proxyhub/posts/operator.py) posts through MTProto, which
takes the text and its entities separately, with offsets and lengths counted in UTF-16 code units
(core.telegram.org/api/entities). Two pure steps:

* :func:`parse_html` turns the HTML that proxyhub/telegram/templates.py renders (Telegram's tag subset
  only) into the same text and entities the Bot API makes of it: ``b``/``strong``, ``i``/``em``,
  ``u``/``ins``, ``s``/``strike``/``del``, ``tg-spoiler`` and ``span class="tg-spoiler"``, ``code``,
  ``pre`` (with ``code class="language-x"`` inside), ``a href``, ``blockquote`` (``expandable``) and
  ``tg-emoji``. Any other tag keeps its text and loses the tag; character references are decoded.
* :func:`with_custom_emoji` marks every plain emoji of that text which the owner's emoji pack has an
  animated twin for (same ``alt`` emoji, variation selectors ignored) with a custom-emoji entity over
  exactly that emoji's UTF-16 range. An emoji inside ``code``/``pre`` stays plain (Telegram draws
  nothing inside code), as does one inside a link (Telegram's nesting rules let no entity but bold,
  italic, underline, strike and spoiler sit inside a link), and every emoji after
  :data:`MAX_CUSTOM_EMOJI`.

The bot path never uses this module: it keeps sending the HTML with plain emoji. Nothing here imports
Telethon or touches the database; proxyhub/mtproxy/userclient.py builds the TL objects from
:class:`TextEntity`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from typing import Final, Literal

EntityKind = Literal[
    "bold",
    "italic",
    "underline",
    "strike",
    "spoiler",
    "code",
    "pre",
    "text_url",
    "blockquote",
    "custom_emoji",
]
# Custom emoji per message. Telegram documents no number; a post carries a dozen emoji at most, and
# anything beyond this simply stays a plain emoji.
MAX_CUSTOM_EMOJI: Final = 50

_TAG_KIND: Final[dict[str, EntityKind]] = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "ins": "underline",
    "s": "strike",
    "strike": "strike",
    "del": "strike",
    "tg-spoiler": "spoiler",
    "span": "spoiler",
    "code": "code",
    "pre": "pre",
    "a": "text_url",
    "blockquote": "blockquote",
    "tg-emoji": "custom_emoji",
}
# core.telegram.org/bots/api#formatting-options: pre and code contain no other entity, and a link
# none but bold, italic, underline, strike and spoiler. So a custom emoji goes in none of them.
_NO_EMOJI_INSIDE: Final = frozenset({"code", "pre", "text_url"})


@dataclass(frozen=True)
class TextEntity:
    """One MessageEntity as data. ``offset``/``length``: UTF-16 code units of :attr:`MessageText.text`."""

    kind: EntityKind
    offset: int
    length: int
    url: str | None = None  # text_url
    language: str | None = None  # pre
    document_id: int | None = None  # custom_emoji
    collapsed: bool = False  # blockquote expandable

    @property
    def end(self) -> int:
        return self.offset + self.length


@dataclass(frozen=True)
class MessageText:
    text: str
    entities: tuple[TextEntity, ...] = ()

    def custom_emoji_count(self) -> int:
        return sum(1 for e in self.entities if e.kind == "custom_emoji")


def utf16_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units): 2 for a code point above U+FFFF."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def utf16_slice(text: str, offset: int, length: int) -> str:
    """The part of ``text`` an entity at (offset, length) in UTF-16 code units covers."""
    units = text.encode("utf-16-le")
    return units[2 * offset : 2 * (offset + length)].decode("utf-16-le")


# --- HTML -> text + entities ---------------------------------------------------------------------------


@dataclass
class _Open:
    kind: EntityKind
    start: int
    url: str | None = None
    language: str | None = None
    document_id: int | None = None
    collapsed: bool = False

    def close(self, end: int) -> TextEntity:
        return TextEntity(
            kind=self.kind,
            offset=self.start,
            length=end - self.start,
            url=self.url,
            language=self.language,
            document_id=self.document_id,
            collapsed=self.collapsed,
        )


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.pos = 0  # UTF-16 code units written so far
        self.stack: list[tuple[str, _Open | None]] = []
        self.done: list[TextEntity] = []

    def _innermost_pre(self) -> _Open | None:
        for _, opened in reversed(self.stack):
            if opened is not None and opened.kind == "pre":
                return opened
        return None

    def _open(self, tag: str, values: Mapping[str, str | None]) -> _Open | None:
        kind = _TAG_KIND.get(tag)
        if kind is None:
            return None
        if tag == "span" and values.get("class") != "tg-spoiler":
            return None
        if tag == "a":
            href = (values.get("href") or "").strip()
            return _Open("text_url", self.pos, url=href) if href else None
        if tag == "tg-emoji":
            emoji_id = values.get("emoji-id") or ""
            if not emoji_id.isascii() or not emoji_id.isdigit():
                return None
            return _Open("custom_emoji", self.pos, document_id=int(emoji_id))
        if tag == "code":
            pre = self._innermost_pre()
            if pre is not None:  # <pre><code class="language-x">: the language of the pre block
                lang = values.get("class") or ""
                if lang.startswith("language-") and pre.language is None:
                    pre.language = lang.removeprefix("language-")
                return None
        if tag == "blockquote":
            return _Open("blockquote", self.pos, collapsed="expandable" in values)
        return _Open(kind, self.pos)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self.handle_data("\n")
            return
        self.stack.append((tag, self._open(tag, {k.lower(): v for k, v in attrs})))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.handle_data("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag not in (name for name, _ in self.stack):
            return  # a stray end tag
        while self.stack:
            name, opened = self.stack.pop()
            self._finish(opened)
            if name == tag:
                break

    def _finish(self, opened: _Open | None) -> None:
        if opened is not None and self.pos > opened.start:
            self.done.append(opened.close(self.pos))

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)
            self.pos += utf16_len(data)

    def result(self) -> MessageText:
        while self.stack:
            self._finish(self.stack.pop()[1])
        return _stripped("".join(self.parts), self.done)


def _ordered(entities: list[TextEntity]) -> tuple[TextEntity, ...]:
    """By offset; at the same offset the longer (outer) entity first."""
    return tuple(sorted(entities, key=lambda e: (e.offset, -e.length)))


def _stripped(text: str, entities: list[TextEntity]) -> MessageText:
    """Leading and trailing white space removed (Telegram does the same), entities moved and clipped."""
    lead = len(text) - len(text.lstrip())
    body = text.strip()
    size = utf16_len(body)
    out: list[TextEntity] = []
    for e in entities:
        start = min(max(e.offset - lead, 0), size)
        end = min(max(e.end - lead, 0), size)
        if end > start:
            out.append(replace(e, offset=start, length=end - start))
    return MessageText(text=body, entities=_ordered(out))


def parse_html(html_text: str) -> MessageText:
    """The text and entities Telegram makes of this HTML (see the module doc)."""
    parser = _Parser()
    parser.feed(html_text)
    parser.close()
    return parser.result()


# --- emoji -------------------------------------------------------------------------------------------------

_VARIATION: Final = frozenset({0xFE0E, 0xFE0F})
_ZWJ: Final = 0x200D
_KEYCAP: Final = 0x20E3


def _regional(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


_SYMBOL_BLOCKS: Final = (
    (0x2194, 0x2199),  # ↔ ↕ ↖ ↗ ↘ ↙ (the plain arrows ← → are text, not emoji)
    (0x21A9, 0x21AA),  # ↩ ↪
    (0x2300, 0x23FF),  # technical (⌛ ⏳ ⏸ ⏰)
    (0x24C2, 0x24C2),  # Ⓜ
    (0x25AA, 0x25AB),  # ▪ ▫
    (0x25B6, 0x25B6),  # ▶
    (0x25C0, 0x25C0),  # ◀
    (0x25FB, 0x25FE),  # ◻ ◼ ◽ ◾
    (0x2600, 0x27BF),  # symbols and dingbats (☀ ♻ ⚠ ✅ ❌ ➡)
    (0x2934, 0x2935),  # ⤴ ⤵
    (0x2B05, 0x2B55),  # ⬅ ⬆ ⬇ ⬛ ⬜ ⭐ ⭕
)


def _pictographic(cp: int) -> bool:
    """Code points that start (or join) an emoji: the pictograph planes and the symbol blocks that
    hold emoji. Math operators, dashes, bullets and letters are not."""
    if 0x1F000 <= cp <= 0x1FAFF:
        return True
    if cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299):
        return True
    return any(lo <= cp <= hi for lo, hi in _SYMBOL_BLOCKS)


def _modifier(cp: int) -> bool:
    """Code points that belong to the emoji before them: variation selectors, the keycap mark,
    skin tones and the tag characters of subdivision flags."""
    return cp in _VARIATION or cp == _KEYCAP or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F


def emoji_key(emoji: str) -> str:
    """The emoji without variation selectors: "⌛️" and "⌛" are the same emoji."""
    return "".join(c for c in emoji if ord(c) not in _VARIATION)


@dataclass(frozen=True)
class Emoji:
    """One emoji of a text: its UTF-16 range and the emoji itself (all its code points)."""

    offset: int
    length: int
    emoji: str


def emoji_in(text: str) -> Iterator[Emoji]:
    """Every emoji of ``text``, whole: a ZWJ sequence, a flag (two regional indicators), a keycap or
    an emoji with its variation selector or skin tone is ONE emoji. Letters (Persian or Latin),
    digits and punctuation are never an emoji; a Persian ZWJ between letters joins nothing here."""
    i, n, pos = 0, len(text), 0
    while i < n:
        start = i
        base = ord(text[i])
        i += 1
        if _regional(base):
            if i < n and _regional(ord(text[i])):
                i += 1
        else:
            while i < n:
                cp = ord(text[i])
                if _modifier(cp):
                    i += 1
                elif cp == _ZWJ and i + 1 < n and _pictographic(base) and _pictographic(ord(text[i + 1])):
                    i += 2
                else:
                    break
        cluster = text[start:i]
        size = utf16_len(cluster)
        if _pictographic(base) or (len(cluster) > 1 and ord(cluster[-1]) == _KEYCAP):
            yield Emoji(offset=pos, length=size, emoji=cluster)
        pos += size


def pack_map(entries: list[tuple[int, str]]) -> dict[str, int]:
    """(document_id, alt emoji) pairs of a pack -> alt (without variation selectors) -> the FIRST
    document with that alt (a pack may hold several animations of the same emoji)."""
    out: dict[str, int] = {}
    for document_id, alt in entries:
        key = emoji_key(alt)
        if key and key not in out:
            out[key] = document_id
    return out


def with_custom_emoji(
    message: MessageText, pack: Mapping[str, int], limit: int = MAX_CUSTOM_EMOJI
) -> MessageText:
    """``message`` with a custom-emoji entity over every plain emoji the pack has an animated twin
    for (see the module doc). The text itself never changes."""
    if not pack:
        return message
    blocked = [
        (e.offset, e.end) for e in message.entities if e.kind in _NO_EMOJI_INSIDE or e.kind == "custom_emoji"
    ]
    added: list[TextEntity] = []
    room = limit - message.custom_emoji_count()
    for e in emoji_in(message.text):
        if len(added) >= room:
            break
        document_id = pack.get(emoji_key(e.emoji))
        if document_id is None:
            continue
        if any(e.offset < end and start < e.offset + e.length for start, end in blocked):
            continue
        added.append(TextEntity("custom_emoji", e.offset, e.length, document_id=document_id))
    if not added:
        return message
    return MessageText(text=message.text, entities=_ordered([*message.entities, *added]))


def operator_message(html_text: str, pack: Mapping[str, int]) -> MessageText:
    """What the operator account sends for a post rendered as ``html_text``."""
    return with_custom_emoji(parse_html(html_text), pack)


def emoji_report(html_text: str, pack: Mapping[str, int], limit: int = 40) -> list[tuple[str, bool]]:
    """The different emoji of a post (in order) and whether the operator's message shows each one
    animated (at least once; an emoji inside code or a link stays plain)."""
    message = operator_message(html_text, pack)
    covered = {e.offset for e in message.entities if e.kind == "custom_emoji"}
    seen: dict[str, tuple[str, bool]] = {}
    for e in emoji_in(message.text):
        key = emoji_key(e.emoji)
        if key not in seen and len(seen) >= limit:
            break
        first, was = seen.get(key, (e.emoji, False))
        seen[key] = (first, was or e.offset in covered)
    return list(seen.values())
