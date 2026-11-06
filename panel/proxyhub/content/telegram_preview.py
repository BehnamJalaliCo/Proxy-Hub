"""Plugin ``telegram``: a public channel read from its public preview page, ``https://t.me/s/<name>``.

No Telegram account and no API: the preview page is what anyone's browser sees, and it gives the
last ~20 posts of the channel with their text, photo or video, time and view count (docs/10
section 3.12, checked on a real channel). The page is parsed with the standard library's HTML
tokenizer, by the classes Telegram's own widget uses:

* a post: ``div.tgme_widget_message`` with ``data-post="<name>/<id>"`` (service messages skipped);
* its text: the first ``div.tgme_widget_message_text`` that is not inside a reply's quote; ``<br>``
  is a line break, every other tag is dropped and only its text kept;
* its photo: the first ``a.tgme_widget_message_photo_wrap`` (the URL in its ``background-image``);
  a video: the first ``video.tgme_widget_message_video`` that is not the blurred background copy,
  its thumbnail ``i.tgme_widget_message_video_thumb`` and duration ``time.message_video_duration``
  (a link preview's image is not the post's media);
* views: ``span.tgme_widget_message_views`` (``6.79K`` -> 6790); time: the ``time[datetime]``.

A channel whose preview is off (or a name that is not a public channel) redirects to
``https://t.me/<name>``; that is the error ``not_public``. Every URL found is checked with
:func:`~proxyhub.content.net.check_url` and anything else is dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urlsplit

from proxyhub.content.clean import tidy
from proxyhub.content.items import MAX_TEXT_CHARS, FetchedItem, MediaRef, SourceInvalid
from proxyhub.content.net import FetchError, SafeHttp, check_url, decode_text

MAX_PAGE_BYTES: Final = 2 * 1024 * 1024
MAX_POSTS: Final = 50
_NAME = re.compile(r"^[a-z][a-z0-9_]{3,30}[a-z0-9]$")
_INPUT = re.compile(
    r"^(?:@|(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?)?([A-Za-z0-9_]{1,64})/?$"
)
# t.me paths that are not channels.
_RESERVED: Final = frozenset(
    {"joinchat", "addemoji", "addstickers", "proxy", "socks", "share", "iv", "login", "setlanguage", "s"}
)
_BG_URL = re.compile(r"background-image\s*:\s*url\(\s*['\"]?([^'\")\s]+)['\"]?\s*\)")
_VIEWS = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([KkMm]?)$")
_DURATION = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$")
_VOID: Final = frozenset(
    {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "col", "embed"}
)


def check_name(raw: str) -> str:
    """A public channel's username (lower case, no @) from ``@name``, ``name`` or a t.me link."""
    if not isinstance(raw, str):
        raise SourceInvalid("telegram_name_invalid")
    m = _INPUT.fullmatch(raw.strip())
    if m is None:
        raise SourceInvalid("telegram_name_invalid")
    name = m.group(1).lower()
    if name in _RESERVED or "__" in name or not _NAME.fullmatch(name):
        raise SourceInvalid("telegram_name_invalid")
    return name


def parse_views(text: str) -> int | None:
    m = _VIEWS.fullmatch(text.strip().replace(",", ""))
    if m is None:
        return None
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(float(m.group(1)) * scale)


def parse_duration(text: str) -> int | None:
    m = _DURATION.fullmatch(text.strip())
    if m is None:
        return None
    hours = int(m.group(1) or 0)
    return hours * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    try:
        return check_url(value)
    except FetchError:
        return None


def _bg_url(style: str | None) -> str | None:
    m = _BG_URL.search(style or "")
    return _url(m.group(1)) if m else None


@dataclass
class _Post:
    post_id: str
    depth: int
    text_parts: list[str] = field(default_factory=list)
    text_done: bool = False
    photo: str | None = None
    video: str | None = None
    video_seen: bool = False
    thumb: str | None = None
    duration: int | None = None
    views: int | None = None
    published_at: datetime | None = None
    service: bool = False


class _Page(HTMLParser):
    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.name = name
        self.posts: list[_Post] = []
        self.channel_seen = False
        self.stack: list[tuple[str, frozenset[str]]] = []
        self.post: _Post | None = None
        self.text_depth: int | None = None  # stack depth of the post's text div while inside it
        self.capture: str | None = None  # "views" | "duration": the element whose text we read
        self.captured: list[str] = []

    def _inside(self, cls: str) -> bool:
        return any(cls in classes for _, classes in self.stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {k.lower(): (v or "") for k, v in attrs}
        classes = frozenset(values.get("class", "").split())
        if tag == "br":
            if self.text_depth is not None and self.post is not None:
                self.post.text_parts.append("\n")
            return
        if "tgme_channel_info" in classes or "tgme_channel_info_header" in classes:
            self.channel_seen = True
        if tag not in _VOID:
            self.stack.append((tag, classes))
        if tag == "div" and "tgme_widget_message" in classes and "data-post" in values:
            self._start_post(values["data-post"], classes)
            return
        post = self.post
        if post is None:
            return
        if tag == "div" and "tgme_widget_message_text" in classes and not post.text_done:
            if not self._inside("tgme_widget_message_reply") and self.text_depth is None:
                self.text_depth = len(self.stack)
        elif tag == "a" and "tgme_widget_message_photo_wrap" in classes and post.photo is None:
            if not self._inside("tgme_widget_message_link_preview"):
                post.photo = _bg_url(values.get("style"))
        elif tag == "video" and "tgme_widget_message_video" in classes and "blured" not in classes:
            if not post.video_seen:
                post.video_seen = True
                post.video = _url(values.get("src"))
        elif tag == "i" and "tgme_widget_message_video_thumb" in classes and post.thumb is None:
            post.thumb = _bg_url(values.get("style"))
        elif tag == "span" and "tgme_widget_message_views" in classes:
            self._capture("views")
        elif tag == "time" and "message_video_duration" in classes:
            self._capture("duration")
        elif tag == "time" and values.get("datetime") and post.published_at is None:
            try:
                parsed = datetime.fromisoformat(values["datetime"])
                post.published_at = parsed if parsed.tzinfo is not None else None
            except ValueError:
                pass

    def _start_post(self, data_post: str, classes: frozenset[str]) -> None:
        self._finish_post()
        _, _, post_id = data_post.partition("/")
        if not post_id.isascii() or not post_id.isdigit() or len(post_id) > 15:
            return
        self.post = _Post(post_id=post_id, depth=len(self.stack), service="service_message" in classes)

    def _capture(self, what: str) -> None:
        self.capture = what
        self.captured = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br" and self.post is not None and self.text_depth is not None:
            self.post.text_parts.append("\n")
            return
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID or tag not in (name for name, _ in self.stack):
            return
        while self.stack:
            name, _ = self.stack.pop()
            depth = len(self.stack) + 1
            if self.capture is not None:
                self._end_capture()
            if self.text_depth is not None and depth == self.text_depth:
                self.text_depth = None
                if self.post is not None:
                    self.post.text_done = True
            if self.post is not None and depth == self.post.depth:
                self._finish_post()
            if name == tag:
                break

    def _end_capture(self) -> None:
        text = "".join(self.captured).strip()
        if self.post is not None:
            if self.capture == "views":
                self.post.views = parse_views(text)
            elif self.capture == "duration":
                self.post.duration = parse_duration(text)
        self.capture = None
        self.captured = []

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.captured.append(data)
        if self.post is not None and self.text_depth is not None:
            self.post.text_parts.append(data)

    def _finish_post(self) -> None:
        post = self.post
        self.post = None
        self.text_depth = None
        if post is not None and not post.service and len(self.posts) < MAX_POSTS:
            self.posts.append(post)

    def close(self) -> None:
        super().close()
        self._finish_post()


def parse_page(page: str, name: str) -> tuple[list[FetchedItem], bool]:
    """The posts of a preview page, and whether the page is a channel's preview at all."""
    parser = _Page(name)
    parser.feed(page)
    parser.close()
    items: list[FetchedItem] = []
    for post in parser.posts:
        media: MediaRef | None = None
        if post.photo:
            media = MediaRef(kind="photo", url=post.photo)
        elif post.video_seen or post.thumb:
            media = MediaRef(kind="video", url=post.video, thumb_url=post.thumb, duration=post.duration)
        items.append(
            FetchedItem(
                external_id=post.post_id,
                url=f"https://t.me/{name}/{post.post_id}",
                published_at=post.published_at,
                text=tidy("".join(post.text_parts))[:MAX_TEXT_CHARS],
                views=post.views,
                media=media,
            )
        )
    return items, parser.channel_seen or bool(parser.posts)


class TelegramPreview:
    kind = "telegram"

    def check_ref(self, raw: str) -> str:
        return check_name(raw)

    def link(self, ref: str) -> str:
        return f"https://t.me/{ref}"

    def default_label(self, ref: str) -> str:
        return f"@{ref}"

    def page_url(self, ref: str) -> str:
        return f"https://t.me/s/{ref}"

    async def fetch(self, http: SafeHttp, ref: str) -> list[FetchedItem]:
        name = check_name(ref)
        fetched = await http.get(self.page_url(name), limit=MAX_PAGE_BYTES, accept="text/html")
        if urlsplit(fetched.url).path.rstrip("/").lower() != f"/s/{name}":
            raise FetchError("not_public")  # Telegram sent us to t.me/<name>: no public preview
        if fetched.content_type and fetched.content_type != "text/html":
            raise FetchError("bad_content_type")
        items, is_channel = parse_page(decode_text(fetched), name)
        if not is_channel:
            raise FetchError("not_public")
        return items

    async def fresh_media(self, http: SafeHttp, post_url: str) -> MediaRef | None:
        """The media of ONE post, read again from its embed page (``https://t.me/<name>/<id>?embed=1``):
        Telegram's file addresses carry a token that expires, so a video is downloaded from a fresh
        address once the owner approved it."""
        m = re.fullmatch(r"https://t\.me/([a-z0-9_]{5,32})/([0-9]{1,15})", post_url or "")
        if m is None:
            return None
        name, post_id = check_name(m.group(1)), m.group(2)
        fetched = await http.get(
            f"https://t.me/{name}/{post_id}?embed=1&mode=tme", limit=MAX_PAGE_BYTES, accept="text/html"
        )
        items, _ = parse_page(decode_text(fetched), name)
        found = next((i for i in items if i.external_id == post_id), None)
        return found.media if found is not None else None
