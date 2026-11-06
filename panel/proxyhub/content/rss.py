"""Plugin ``rss``: an RSS 2.0 or Atom feed, by its https URL.

The feed is parsed with the standard library's expat parser, configured so that nothing outside
the document is ever read and no entity can blow up: a document type with an internal subset, any
entity declaration, and any external or parameter entity are refused before parsing goes on
(:class:`FeedRefused`, error ``feed_unsafe``). The tree is bounded too (:data:`MAX_ELEMENTS`,
:data:`MAX_DEPTH`, :data:`MAX_TEXT_CHARS` per element). Nothing is executed.

What an entry gives (RSS ``item`` / Atom ``entry``; namespaces are matched by their local name):

* id: ``guid`` / ``id``, else the entry's link (longer than 200 characters: its sha256);
* link: ``link`` (RSS text, Atom ``link rel=alternate href``), kept only if it passes
  :func:`~proxyhub.content.net.check_url`;
* time: ``pubDate`` (RFC 822), ``published`` / ``updated`` / ``dc:date`` (ISO 8601);
* text: ``title``, then ``description`` / ``summary`` / ``content`` / ``content:encoded`` as plain
  text (proxyhub/content/clean.py :func:`html_to_text`);
* media: an ``enclosure`` (or Atom ``link rel=enclosure``), ``media:content`` or
  ``media:thumbnail`` with an image or video type, else the first ``<img>`` of the description;
  only https URLs, checked again when downloaded.

Views: a feed has none.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final
from xml.parsers import expat

from proxyhub.content.clean import html_to_text, tidy
from proxyhub.content.items import MAX_EXTERNAL_ID, MAX_TEXT_CHARS, FetchedItem, MediaRef, SourceInvalid
from proxyhub.content.net import FetchError, SafeHttp, check_url

MAX_FEED_BYTES: Final = 3 * 1024 * 1024
MAX_ELEMENTS: Final = 60_000
MAX_DEPTH: Final = 40
MAX_ELEMENT_TEXT: Final = 100_000
MAX_ENTRIES: Final = 60
_ACCEPT: Final = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"
_FEED_TYPES: Final = frozenset(
    {
        "application/rss+xml",
        "application/atom+xml",
        "application/xml",
        "text/xml",
        "application/rdf+xml",
        "application/x-rss+xml",
        "text/plain",
        "",
    }
)
_IMAGE_TYPES: Final = ("image/jpeg", "image/jpg", "image/png")
_VIDEO_TYPES: Final = ("video/mp4",)


class FeedRefused(Exception):
    pass


@dataclass
class _Node:
    name: str  # local name, lower case
    attrs: dict[str, str]
    text: list[str] = field(default_factory=list)
    children: list[_Node] = field(default_factory=list)

    def first(self, *names: str) -> _Node | None:
        for child in self.children:
            if child.name in names:
                return child
        return None

    def all(self, *names: str) -> list[_Node]:
        return [c for c in self.children if c.name in names]

    @property
    def value(self) -> str:
        return "".join(self.text).strip()


def _local(name: str) -> str:
    """``{namespace}local`` or ``prefix:local`` -> ``local`` (lower case)."""
    return name.rsplit("}", 1)[-1].rsplit(" ", 1)[-1].rsplit(":", 1)[-1].lower()


def parse_xml(data: bytes) -> _Node:
    """A small tree of the document; FeedRefused for anything unsafe or too big."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    root = _Node("#root", {})
    stack = [root]
    count = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal count
        count += 1
        if count > MAX_ELEMENTS or len(stack) > MAX_DEPTH:
            raise FeedRefused("too_big")
        node = _Node(_local(name), {_local(k): v for k, v in attrs.items()})
        stack[-1].children.append(node)
        stack.append(node)

    def end(name: str) -> None:
        stack.pop()

    def chars(data: str) -> None:
        node = stack[-1]
        if sum(len(t) for t in node.text) + len(data) > MAX_ELEMENT_TEXT:
            raise FeedRefused("too_big")
        node.text.append(data)

    def doctype(name: str, sysid: str | None, pubid: str | None, has_internal_subset: int) -> None:
        if has_internal_subset or sysid or pubid:
            raise FeedRefused("dtd")

    def refuse(*_: object) -> None:
        raise FeedRefused("entity")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse  # type: ignore[assignment]
    parser.SkippedEntityHandler = refuse
    try:
        parser.Parse(data, True)
    except expat.ExpatError:
        raise FeedRefused("syntax") from None
    return root


def _time(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _https(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return check_url(value.strip())
    except FetchError:
        return None


def _media(entry: _Node, description_img: str | None) -> MediaRef | None:
    candidates: list[tuple[str, str]] = []  # (type, url)
    for node in entry.children:
        if node.name == "enclosure" or (node.name == "link" and node.attrs.get("rel") == "enclosure"):
            candidates.append(
                (node.attrs.get("type", "").lower(), node.attrs.get("url") or node.attrs.get("href") or "")
            )
        elif node.name in ("content", "group") and node.attrs.get("url") is not None:
            kind = node.attrs.get("type", "").lower() or {"image": "image/jpeg", "video": "video/mp4"}.get(
                node.attrs.get("medium", ""), ""
            )
            candidates.append((kind, node.attrs.get("url", "")))
        elif node.name == "group":
            for inner in node.all("content"):
                candidates.append((inner.attrs.get("type", "").lower(), inner.attrs.get("url", "")))
    for kind, url in candidates:
        checked = _https(url)
        if checked and kind in _VIDEO_TYPES:
            thumb = entry.first("thumbnail")
            return MediaRef(
                kind="video", url=checked, thumb_url=_https(thumb.attrs.get("url")) if thumb else None
            )
    for kind, url in candidates:
        checked = _https(url)
        if checked and kind in _IMAGE_TYPES:
            return MediaRef(kind="photo", url=checked)
    thumb = entry.first("thumbnail")
    image = _https(thumb.attrs.get("url")) if thumb is not None else None
    image = image or _https(description_img)
    return MediaRef(kind="photo", url=image) if image else None


def _first_text(entry: _Node, *names: str) -> str:
    for name in names:
        for node in entry.all(name):
            if node.value and node.attrs.get("url") is None:  # media:content carries a url, no text
                return node.value
    return ""


def _entry(entry: _Node, atom: bool) -> FetchedItem | None:
    title = html_to_text(_first_text(entry, "title"))[0]
    body, first_img = html_to_text(_first_text(entry, "encoded", "description", "summary", "content"))
    link: str | None = None
    if atom:
        for node in entry.all("link"):
            if node.attrs.get("rel", "alternate") == "alternate" and node.attrs.get("href"):
                link = _https(node.attrs["href"])
                break
    else:
        plain = entry.first("link")
        link = _https(plain.value) if plain is not None else None
    ident = _first_text(entry, "guid", "id") or (link or "")
    if not ident:
        return None
    if len(ident) > MAX_EXTERNAL_ID:
        ident = "sha256:" + hashlib.sha256(ident.encode()).hexdigest()
    when = None
    for name in ("pubdate", "published", "updated", "date", "issued", "modified"):
        stamp = entry.first(name)
        if stamp is not None and (when := _time(stamp.value)) is not None:
            break
    parts = [body] if body and title and body.startswith(title) else [x for x in (title, body) if x]
    return FetchedItem(
        external_id=ident,
        url=link,
        published_at=when,
        text=tidy("\n".join(parts))[:MAX_TEXT_CHARS],
        views=None,
        media=_media(entry, first_img),
    )


def parse_feed(data: bytes) -> list[FetchedItem]:
    """The entries of an RSS 2.0, RSS 1.0 (RDF) or Atom feed. FeedRefused if it is none of them."""
    root = parse_xml(data)
    top = root.children[0] if root.children else None
    if top is None:
        raise FeedRefused("empty")
    if top.name == "feed":
        entries, atom = top.all("entry"), True
    elif top.name == "rss":
        channel = top.first("channel")
        entries, atom = (channel.all("item") if channel else []), False
    elif top.name == "rdf":
        entries, atom = top.all("item"), False
    else:
        raise FeedRefused("not_a_feed")
    out: list[FetchedItem] = []
    for entry in entries[:MAX_ENTRIES]:
        item = _entry(entry, atom)
        if item is not None:
            out.append(item)
    return out


_FEED_INPUT = re.compile(r"^https://\S+$")


class RssFeed:
    kind = "rss"

    def check_ref(self, raw: str) -> str:
        if not isinstance(raw, str) or not _FEED_INPUT.fullmatch(raw.strip()):
            raise SourceInvalid("feed_url_invalid")
        try:
            return check_url(raw.strip())
        except FetchError:
            raise SourceInvalid("feed_url_invalid") from None

    def link(self, ref: str) -> str:
        return ref

    def default_label(self, ref: str) -> str:
        from urllib.parse import urlsplit

        return (urlsplit(ref).hostname or ref)[:80]

    async def fetch(self, http: SafeHttp, ref: str) -> list[FetchedItem]:
        fetched = await http.get(self.check_ref(ref), limit=MAX_FEED_BYTES, accept=_ACCEPT)
        if fetched.content_type not in _FEED_TYPES:
            raise FetchError("bad_content_type")
        try:
            return parse_feed(fetched.body)
        except FeedRefused as exc:
            raise FetchError("feed_unsafe" if str(exc) in ("dtd", "entity") else "feed_invalid") from None
