"""One round of the content engine (loop ``content``, proxyhub/reconcilers/content.py).

Every round (a minute), with no transaction open while the network answers:

1. **housekeeping**, in one short transaction: pending and approved candidates older than 48 hours
   expire; files nobody needs any more are removed; long-ended rows are pruned (hourly);
2. **sources**: up to :data:`SOURCES_PER_ROUND` enabled sources whose next read is due (every 5
   minutes; after an error the source's own back-off, proxyhub/content/sources.py) are read through
   their plugin (proxyhub/content/plugins.py); each source's result is stored in its own
   transaction (proxyhub/content/candidates.py :func:`store_items`), so one bad source never holds
   up the others. A Telegram source is read with the operator account (decision D55,
   proxyhub/content/telegram_account.py) on one connection for the round, under the account's
   lock, every 10 minutes; while the account is not usable (not connected, kill switch, vault)
   its web preview is read instead, and while the account is busy or Telegram asked it to wait,
   the source waits for a later round;
3. **media**: up to :data:`MEDIA_PER_ROUND` files are downloaded (approved videos first, then
   photos and video thumbnails of the queue), each checked by its real bytes
   (proxyhub/content/media.py), while the stored total stays under :data:`MEDIA_STORE_MAX`. A
   Telegram video's address is read again from its post right before (its token expires). A file
   of a post the account read (``tgmsg:``) is downloaded by the account on the round's connection,
   at most :data:`ACCOUNT_MEDIA_PER_ROUND`. A video the content supervisor already downloaded for
   its review (:func:`fetch_media`, D52) is ready and never fetched twice.

All fetching goes through :class:`~proxyhub.content.net.SafeHttp` (https only, no private address on
any hop, bounded). A failure is recorded as a short code (the dashboard shows it in Persian); a
failure of a kind the panel does not know is logged with its exception TYPE only (a message could
carry fetched text) and recorded as ``internal``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.content import candidates, media, sources
from proxyhub.content.items import FetchedItem, MediaRef
from proxyhub.content.net import MEDIA_DEADLINE_S, FetchError, SafeHttp
from proxyhub.content.plugins import PLUGINS, SourcePlugin
from proxyhub.content.telegram_account import (
    ACCOUNT_FETCH_EVERY,
    USE_PREVIEW,
    AccountRead,
    AccountReader,
    is_account_media,
    parse_media_ref,
)
from proxyhub.content.telegram_preview import TelegramPreview

log = logging.getLogger(__name__)

SOURCES_PER_ROUND: Final = 4
MEDIA_PER_ROUND: Final = 6
ACCOUNT_MEDIA_PER_ROUND: Final = 4
SUPERVISOR_LOCK_WAIT_S: Final = 30.0
MEDIA_STORE_MAX: Final = 300 * 1024 * 1024
PRUNE_EVERY: Final = timedelta(hours=1)
# Download errors no retry can fix.
FINAL_ERRORS: Final = frozenset(
    {"too_big", "bad_url", "blocked_address", "bad_content_type", "http_404", "http_410", "media_gone"}
)
_ACCEPT: Final = {"photo": "image/jpeg, image/png", "thumb": "image/jpeg, image/png", "video": "video/mp4"}
_LIMIT: Final = {
    "photo": media.PHOTO_MAX_BYTES,
    "thumb": media.THUMB_MAX_BYTES,
    "video": media.VIDEO_MAX_BYTES,
}

HttpFactory = Callable[[], SafeHttp]
Clock = Callable[[], datetime]


async def fresh_video_url(
    http: SafeHttp, task: candidates.MediaTask, plugins: Mapping[str, SourcePlugin]
) -> str:
    """A Telegram video's current address (its token expires), read again from its post; else the
    stored one."""
    plugin = plugins.get("telegram")
    if task.source_kind != "telegram" or task.post_url is None or not isinstance(plugin, TelegramPreview):
        return task.url
    try:
        found: MediaRef | None = await plugin.fresh_media(http, task.post_url)
    except FetchError:
        return task.url
    return found.url if found is not None and found.kind == "video" and found.url else task.url


async def fetch_media(
    http: SafeHttp,
    task: candidates.MediaTask,
    plugins: Mapping[str, SourcePlugin],
    account: AccountReader | None = None,
) -> tuple[media.MediaInfo, bytes]:
    """Download one file of a candidate (a video from a fresh address) within its role's byte limit
    and check its real bytes. Raises FetchError or MediaRefused. Also the content supervisor's way
    to fetch a video before its review (proxyhub/content/supervisor.py); a file of a post the
    account read (D55) is downloaded on a connection of its own."""
    if is_account_media(task.url):
        found = parse_media_ref(task.url)
        if found is None:
            raise FetchError("bad_url")
        if account is None:
            raise FetchError("account_unavailable")
        async with account.connect({found[0]}, wait_s=SUPERVISOR_LOCK_WAIT_S) as read:
            if read is None:
                raise FetchError(account.last_unavailable or "account_unavailable")
            return await read.download(task.url, task.role)
    url = await fresh_video_url(http, task, plugins) if task.role == "video" else task.url
    fetched = await http.get(
        url, limit=_LIMIT[task.role], accept=_ACCEPT[task.role], deadline_s=MEDIA_DEADLINE_S
    )
    return media.check_download(task.role, fetched.content_type, fetched.body), fetched.body


@dataclass
class Round:
    expired: int = 0
    media_dropped: int = 0
    pruned: int = 0
    read: dict[int, str] = field(default_factory=dict)  # source id -> "ok:<new>" or the error code
    # source id -> why its read waits for a later round (the account busy, or asked to wait)
    waiting: dict[int, str] = field(default_factory=dict)
    media: dict[int, str] = field(default_factory=dict)  # candidate id -> "ok" or the error code


@dataclass
class Engine:
    engine: AsyncEngine
    clock: Clock
    http_factory: HttpFactory = SafeHttp
    plugins: dict[str, SourcePlugin] = field(default_factory=lambda: dict(PLUGINS))
    account: AccountReader | None = None  # D55; None: Telegram sources by their web preview only
    last_prune: datetime | None = None

    async def run_round(self) -> Round:
        result = Round()
        now = self.clock()
        async with self.engine.begin() as conn:
            result.expired = await candidates.expire(conn, now)
            result.media_dropped = await candidates.drop_media(conn, now)
            if self.last_prune is None or now - self.last_prune >= PRUNE_EVERY:
                self.last_prune = now
                result.pruned = await candidates.prune(conn, now)
            due = await sources.due(conn, now, SOURCES_PER_ROUND)
            tasks = await candidates.media_tasks(conn, MEDIA_PER_ROUND)
            stored_bytes = await candidates.media_bytes_total(conn)
        if not due and not tasks:
            return result
        by_account = [s for s in due if s.kind == "telegram"] if self.account is not None else []
        names = {s.ref for s in by_account} | {
            found[0] for t in tasks if (found := parse_media_ref(t.url)) is not None
        }
        others = [s for s in due if s not in by_account]
        if names and self.account is not None:
            async with self.account.connect(names) as read:
                if read is None:
                    reason = self.account.last_unavailable or "account_unavailable"
                    if reason in USE_PREVIEW:
                        others += by_account
                    else:
                        result.waiting.update({s.id: reason for s in by_account})
                else:
                    stored_bytes = await self._account_round(read, by_account, stored_bytes, result)
        async with self.http_factory() as http:
            for source in others:
                result.read[source.id] = await self._read(http, source)
            if due:  # the new candidates' photos in this same round
                async with self.engine.begin() as conn:
                    tasks = await candidates.media_tasks(conn, MEDIA_PER_ROUND)
            for task in tasks:
                if is_account_media(task.url) or task.candidate_id in result.media:
                    continue  # the account's files go on its own connection only
                if stored_bytes >= MEDIA_STORE_MAX:
                    log.warning("content: media store full (%d bytes); downloads wait", stored_bytes)
                    break
                outcome, size = await self._download(http, task)
                result.media[task.candidate_id] = outcome
                stored_bytes += size
        return result

    async def _account_round(
        self, read: AccountRead, due: list[sources.Source], stored_bytes: int, result: Round
    ) -> int:
        """The round's Telegram sources and the files of the posts the account read, on one
        connection. Returns the media store's new total."""
        for source in due:
            if read.stopped:
                result.waiting[source.id] = read.stopped
                continue
            result.read[source.id] = await self._read_with(source, read.read, ACCOUNT_FETCH_EVERY)
        async with self.engine.begin() as conn:
            tasks = await candidates.media_tasks(conn, MEDIA_PER_ROUND)
        done = 0
        for task in tasks:
            found = parse_media_ref(task.url)
            if found is None or found[0] not in read.names or read.stopped:
                continue
            if done >= ACCOUNT_MEDIA_PER_ROUND:
                break
            if stored_bytes >= MEDIA_STORE_MAX:
                log.warning("content: media store full (%d bytes); downloads wait", stored_bytes)
                break
            outcome, size = await self._save_download(task, read.download(task.url, task.role))
            result.media[task.candidate_id] = outcome
            stored_bytes += size
            done += 1
        return stored_bytes

    async def _read(self, http: SafeHttp, source: sources.Source) -> str:
        plugin = self.plugins.get(source.kind)
        if plugin is None:
            return await self._store(source, None, "internal", sources.FETCH_EVERY)

        async def fetch(ref: str) -> list[FetchedItem]:
            return await plugin.fetch(http, ref)

        return await self._read_with(source, fetch, sources.FETCH_EVERY)

    async def _read_with(
        self,
        source: sources.Source,
        fetch: Callable[[str], Awaitable[list[FetchedItem]]],
        every: timedelta,
    ) -> str:
        items: list[FetchedItem] | None = None
        code = "internal"
        try:
            items = await fetch(source.ref)
        except FetchError as exc:
            code = exc.code
        except Exception as exc:  # a parser bug must not stop the loop, nor print fetched text
            log.error("content: reading source %d failed (%s)", source.id, type(exc).__name__)
        return await self._store(source, items, code, every)

    async def _store(
        self, source: sources.Source, items: list[FetchedItem] | None, code: str, every: timedelta
    ) -> str:
        async with self.engine.begin() as conn:
            current = await sources.get(conn, source.id)
            if current is None or not current.enabled:  # removed or turned off meanwhile
                return "gone"
            now = self.clock()
            if items is None:
                await sources.record_error(conn, current, now, code)
                return code
            stored = await candidates.store_items(conn, current, items, now)
            await sources.record_ok(conn, current.id, now, len(items), every=every)
        return f"ok:{stored.new}"

    async def _download(self, http: SafeHttp, task: candidates.MediaTask) -> tuple[str, int]:
        return await self._save_download(task, fetch_media(http, task, self.plugins))

    async def _save_download(
        self, task: candidates.MediaTask, fetching: Awaitable[tuple[media.MediaInfo, bytes]]
    ) -> tuple[str, int]:
        try:
            info, body = await fetching
        except FetchError as exc:
            await self._failed(task, exc.code, final=exc.code in FINAL_ERRORS)
            return exc.code, 0
        except media.MediaRefused as exc:
            await self._failed(task, exc.code, final=True)
            return exc.code, 0
        async with self.engine.begin() as conn:
            if await candidates.get(conn, task.candidate_id) is None:
                return "gone", 0
            await candidates.save_media(conn, task, info, body, self.clock())
        return "ok", len(body)

    async def _failed(self, task: candidates.MediaTask, code: str, *, final: bool) -> None:
        async with self.engine.begin() as conn:
            await candidates.media_failed(conn, task, code, self.clock(), final=final)
