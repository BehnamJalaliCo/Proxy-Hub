"""Telegram content sources read with the operator account (decision D55).

Big channels turn their web preview (``t.me/s/<name>``, proxyhub/content/telegram_preview.py) off,
so the panel reads them the way the Telegram app does: with the second account (D37), the same one
that posts with Premium emoji (D38). The owner asked for it (2026-11-06): «با همون حساب دوم داریم
پستارو میزاریم؛ از همون برداریم.»

What the account may do here is pinned in proxyhub/mtproxy/userclient.py (:class:`ChannelScope`,
``source_names`` / ``source_ids`` / ``source_files``): resolve the owner's source names, read the
last :data:`READ_LIMIT` posts of those channels, read one post again, and download the photo, video
or video thumbnail of a post it read on the same connection. It never joins a channel, marks
anything read, forwards, or writes anywhere.

* **One operation at a time on the account**: everything runs under the account's advisory lock
  (proxyhub.mtproxy.store.account_lock); when the poster or the sponsor code holds it, this round's
  Telegram sources simply wait for the next round.
* **Kept low**: a source is read every :data:`ACCOUNT_FETCH_EVERY` (not every 5 minutes as a web
  page), a channel's id is resolved once and kept (:data:`CACHE_KEY`, again after
  :data:`REFRESH_EVERY`), and a flood wait from Telegram stops every read and download of this
  process until it is over (:attr:`AccountReader.hold_until`).
* **Not usable** (not connected, kill switch on, vault unreadable): the engine reads the source's
  web preview as before (:data:`USE_PREVIEW`).
* **Media**: a post's file is stored by reference (``tgmsg:<name>/<id>``, :func:`media_ref`), never
  as a URL, and downloaded later by reading that post again (its file reference is fresh). The
  size Telegram declares is checked before a byte is fetched; the bytes are checked as every
  other download (proxyhub/content/media.py).
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.content import media
from proxyhub.content.clean import tidy
from proxyhub.content.items import MAX_TEXT_CHARS, FetchedItem, MediaRef
from proxyhub.content.net import FetchError
from proxyhub.fleet.actions import iso
from proxyhub.mtproxy import state as mtp_state
from proxyhub.mtproxy import store as mtp_store
from proxyhub.mtproxy.userclient import (
    ChannelRef,
    ChannelScope,
    ClientOptions,
    MtpError,
    SourcePost,
    operator_session,
)
from proxyhub.telegram import store as tg_store
from proxyhub.vault import Vault, VaultError

log = logging.getLogger(__name__)

TG_MEDIA_PREFIX: Final = "tgmsg:"
_MEDIA_REF = re.compile(r"^tgmsg:([a-z][a-z0-9_]{3,30}[a-z0-9])/([1-9][0-9]{0,14})$")
CACHE_KEY: Final = "content.tg_channels"
READ_LIMIT: Final = 20
ACCOUNT_FETCH_EVERY: Final = timedelta(minutes=10)
REFRESH_EVERY: Final = timedelta(days=7)
LOCK_WAIT_S: Final = 2.0
MIN_HOLD: Final = timedelta(minutes=1)
MAX_HOLD: Final = timedelta(hours=24)
# Why the account cannot read now, when the web preview should be read instead (it may work for a
# channel whose preview is on). Busy and flood wait are short: the source waits for a later round.
USE_PREVIEW: Final = frozenset({"not_connected", "kill_switch", "vault_unavailable"})
_LIMIT: Final = {
    "photo": media.PHOTO_MAX_BYTES,
    "thumb": media.THUMB_MAX_BYTES,
    "video": media.VIDEO_MAX_BYTES,
}
_DECLARED: Final = {"photo": "image/jpeg", "thumb": "image/jpeg", "video": "video/mp4"}
# MtpError codes (proxyhub/mtproxy/userclient.py) -> the content engine's error codes
# (proxyhub/content/messages.py).
_CODES: Final = {
    "flood_wait": "account_flood",
    "session_terminated": "account_unavailable",
    "source_not_found": "channel_not_found",
    "source_not_channel": "not_a_channel",
    "source_media_gone": "media_gone",
    "source_media_too_big": "too_big",
    "telegram_unavailable": "telegram_unavailable",
}
_PRIVATE_RPCS: Final = frozenset({"CHANNEL_INVALID", "CHANNEL_PRIVATE", "CHANNEL_PUBLIC_GROUP_NA"})


def media_ref(name: str, message_id: int) -> str:
    """The stored reference of a source post's file (``content_candidates.media_url``)."""
    return f"{TG_MEDIA_PREFIX}{name}/{message_id}"


def parse_media_ref(value: str | None) -> tuple[str, int] | None:
    m = _MEDIA_REF.fullmatch(value or "")
    return (m.group(1), int(m.group(2))) if m else None


def is_account_media(value: str | None) -> bool:
    return bool(value) and str(value).startswith(TG_MEDIA_PREFIX)


def _pick(group: list[SourcePost]) -> SourcePost:
    """An album is one post in Telegram's eyes: its captioned message (else its first one)."""
    with_text = [p for p in group if p.text.strip()]
    chosen = min(with_text or group, key=lambda p: p.id)
    if chosen.media is None:
        with_media = [p for p in group if p.media is not None]
        if with_media:
            first = min(with_media, key=lambda p: p.id)
            return SourcePost(
                id=first.id,
                date=chosen.date,
                text=chosen.text,
                views=chosen.views,
                grouped_id=chosen.grouped_id,
                media=first.media,
            )
    return chosen


def items_from(name: str, posts: list[SourcePost]) -> list[FetchedItem]:
    """The posts of a source as the engine's items (the same ids and links as its web preview, so
    a source read either way never gives the same post twice)."""
    singles: list[SourcePost] = []
    albums: dict[int, list[SourcePost]] = {}
    for post in posts:
        if post.grouped_id is None:
            singles.append(post)
        else:
            albums.setdefault(post.grouped_id, []).append(post)
    chosen = singles + [_pick(group) for group in albums.values()]
    items: list[FetchedItem] = []
    for post in sorted(chosen, key=lambda p: p.id, reverse=True):
        ref: MediaRef | None = None
        if post.media is not None:
            stored = media_ref(name, post.id)
            if post.media.kind == "photo":
                ref = MediaRef(kind="photo", url=stored)
            else:
                ref = MediaRef(
                    kind="video",
                    url=stored,
                    thumb_url=stored if post.media.has_thumb else None,
                    duration=post.media.duration,
                )
        text = tidy(post.text)[:MAX_TEXT_CHARS]
        if not text and ref is None:
            continue
        items.append(
            FetchedItem(
                external_id=str(post.id),
                url=f"https://t.me/{name}/{post.id}",
                published_at=post.date,
                text=text,
                views=post.views,
                media=ref,
            )
        )
    return items


def _cache_entry(value: Any) -> tuple[int, int, datetime | None] | None:
    if not isinstance(value, dict):
        return None
    channel_id, access_hash = value.get("id"), value.get("hash")
    if not isinstance(channel_id, int) or not isinstance(access_hash, int) or channel_id <= 0:
        return None
    at = value.get("at")
    try:
        when = datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        when = None
    return channel_id, access_hash, when


@dataclass
class AccountRead:
    """One connection of the operator account, for this round's sources and files."""

    reader: AccountReader
    op: Any  # proxyhub.mtproxy.userclient.OperatorSession
    names: frozenset[str]
    cache: dict[str, Any]
    changed: bool = False
    stopped: str | None = None  # a flood wait: nothing more on this connection
    _refs: dict[str, ChannelRef] = field(default_factory=dict)

    def _code(self, exc: MtpError) -> str:
        if exc.code == "flood_wait":
            self.reader.hold(exc.seconds)
            self.stopped = "account_flood"
        if exc.code == "telegram_refused" and exc.rpc in _PRIVATE_RPCS:
            return "channel_private"
        return _CODES.get(exc.code, "telegram_refused")

    async def _ref(self, name: str) -> ChannelRef:
        known = self._refs.get(name)
        if known is not None:
            return known
        entry = _cache_entry(self.cache.get(name))
        now = self.reader.clock()
        if entry is not None and entry[2] is not None and now - entry[2] < REFRESH_EVERY:
            ref = ChannelRef(channel_id=entry[0], access_hash=entry[1])
        else:
            ref = await self.op.resolve_source(name)
            self.cache[name] = {"id": ref.channel_id, "hash": ref.access_hash, "at": iso(now)}
            self.changed = True
        self._refs[name] = ref
        return ref

    def _forget(self, name: str) -> None:
        self._refs.pop(name, None)
        if self.cache.pop(name, None) is not None:
            self.changed = True

    async def read(self, name: str) -> list[FetchedItem]:
        """A source's recent posts. Raises FetchError."""
        if self.stopped:
            raise FetchError(self.stopped)
        if name not in self.names:
            raise FetchError("account_unavailable")
        try:
            ref = await self._ref(name)
            posts = await self.op.read_source(ref, READ_LIMIT)
        except MtpError as exc:
            code = self._code(exc)
            if code in ("channel_private", "channel_not_found", "not_a_channel"):
                self._forget(name)  # resolved again next time: the name may point elsewhere now
            raise FetchError(code) from None
        return items_from(name, posts)

    async def download(
        self, url: str, role: Literal["photo", "thumb", "video"]
    ) -> tuple[media.MediaInfo, bytes]:
        """A source post's file, read again first (fresh file reference). Raises FetchError or
        MediaRefused."""
        if self.stopped:
            raise FetchError(self.stopped)
        found = parse_media_ref(url)
        if found is None:
            raise FetchError("bad_url")
        name, message_id = found
        if name not in self.names:
            raise FetchError("account_unavailable")
        try:
            ref = await self._ref(name)
            post = await self.op.source_message(ref, message_id)
            if post is None or post.media is None:
                raise FetchError("media_gone")
            if role == "video" and post.media.mime != "video/mp4":
                raise media.MediaRefused("video_not_mp4")
            data = await self.op.download_source_media(ref, message_id, role, _LIMIT[role])
        except MtpError as exc:
            raise FetchError(self._code(exc)) from None
        return media.check_download(role, _DECLARED[role], data), data


@dataclass
class AccountReader:
    """Opens :class:`AccountRead` connections (one process: the worker's content loops)."""

    engine: AsyncEngine
    vault: Callable[[], Vault | None]
    clock: Callable[[], datetime]
    options: ClientOptions | None = None
    lock_wait_s: float = LOCK_WAIT_S
    hold_until: datetime | None = None
    last_unavailable: str | None = None

    def hold(self, seconds: int | None) -> None:
        wait = timedelta(seconds=seconds or 0)
        until = self.clock() + min(max(wait, MIN_HOLD), MAX_HOLD)
        if self.hold_until is None or until > self.hold_until:
            self.hold_until = until
        log.warning("content: Telegram asked the account to wait; sources read again after %s", iso(until))

    @contextlib.asynccontextmanager
    async def connect(
        self, names: Collection[str], *, wait_s: float | None = None
    ) -> AsyncIterator[AccountRead | None]:
        """A connection allowed to read ``names`` (the owner's source usernames), or None when the
        account cannot be used now (:attr:`last_unavailable` says why)."""
        async with contextlib.AsyncExitStack() as stack:
            read = await self._open(stack, frozenset(names), wait_s)
            try:
                yield read
            finally:
                if read is not None and read.changed:
                    await self._save_cache(read.cache)

    async def _open(
        self, stack: contextlib.AsyncExitStack, names: frozenset[str], wait_s: float | None
    ) -> AccountRead | None:
        self.last_unavailable = None
        if self.hold_until is not None and self.clock() < self.hold_until:
            self.last_unavailable = "account_flood"
            return None
        vault = self.vault()
        if vault is None:
            self.last_unavailable = "vault_unavailable"
            return None
        try:
            await stack.enter_async_context(
                mtp_store.account_lock(self.engine, wait_s=self.lock_wait_s if wait_s is None else wait_s)
            )
        except mtp_store.LockBusy:
            self.last_unavailable = "account_busy"
            return None
        try:
            async with self.engine.connect() as conn:
                if await mtp_store.kill_switch_on(conn):
                    self.last_unavailable = "kill_switch"
                    return None
                row = await mtp_store.load_row(conn)
                if row.state != mtp_state.CONNECTED:
                    self.last_unavailable = "not_connected"
                    return None
                creds = await mtp_store.load_creds(conn, vault)
                session = await mtp_store.load_session(conn, vault)
                cache = await tg_store.get_setting(conn, CACHE_KEY) or {}
        except VaultError:
            self.last_unavailable = "vault_unavailable"
            return None
        if creds is None or session is None:
            self.last_unavailable = "not_connected"
            return None
        known = {n: _cache_entry(cache.get(n)) for n in names}
        scope = ChannelScope(source_names=names, source_ids={e[0] for e in known.values() if e is not None})
        try:
            op = await stack.enter_async_context(
                operator_session(creds, session, options=self.options, channels=scope)
            )
        except MtpError as exc:
            if exc.code == "flood_wait":
                self.hold(exc.seconds)
            self.last_unavailable = _CODES.get(exc.code, "telegram_unavailable")
            return None
        return AccountRead(reader=self, op=op, names=names, cache=dict(cache))

    async def _save_cache(self, cache: dict[str, Any]) -> None:
        async with self.engine.begin() as conn:
            await tg_store.put_setting(conn, CACHE_KEY, cache)
