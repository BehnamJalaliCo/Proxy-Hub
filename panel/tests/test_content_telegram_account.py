"""Telegram content sources read with the operator account (decision D55).

No imitation of Telegram (D10): the allowlist is tested with Telethon's own request and type
objects, a session's requests go on to a real connection refusal on a local port nobody listens on,
and the reader runs on a real PostgreSQL with a real (refused) connection. The real read of a real
channel is the server's job (ops/prompts/P23.md).
"""

from __future__ import annotations

import contextlib
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from telethon import functions, types
from test_content_support import clean_content_tables
from test_fleet_support import make_vault
from test_mtproxy_support import clean as clean_account
from test_mtproxy_support import seed_connected
from test_mtproxy_units import closed_port

from proxyhub.content import candidates, sources
from proxyhub.content.engine import Engine
from proxyhub.content.items import FetchedItem
from proxyhub.content.net import FetchError, SafeHttp
from proxyhub.content.telegram_account import (
    AccountReader,
    items_from,
    media_ref,
    parse_media_ref,
)
from proxyhub.fleet.settings_store import save_fleet_settings
from proxyhub.mtproxy import store as mtp_store
from proxyhub.mtproxy import userclient
from proxyhub.mtproxy.userclient import (
    MAX_SOURCE_HISTORY,
    ApiCredentials,
    ChannelRef,
    ChannelScope,
    ClientOptions,
    Endpoint,
    MtpError,
    OperatorSession,
    RequestNotAllowed,
    SourceMedia,
    SourcePost,
    check_request,
)
from proxyhub.settings_schema import FleetSettings
from proxyhub.vault import SecretValue, Vault

SOURCE = 1_555_000_111
OTHER = 1_555_000_222
PHOTO_ID = 9_001
DOC_ID = 9_002
T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


def reading(*, files: set[int] | None = None) -> ChannelScope:
    return ChannelScope(
        source_names=frozenset({"imtproto"}),
        source_ids={SOURCE},
        source_files={PHOTO_ID} if files is None else files,
    )


def history(peer: Any, limit: int = 20, **kw: Any) -> Any:
    return functions.messages.GetHistoryRequest(
        peer=peer, offset_id=0, offset_date=None, add_offset=0, limit=limit, max_id=0, min_id=0, hash=0, **kw
    )


def peer(channel_id: int = SOURCE) -> Any:
    return types.InputPeerChannel(channel_id=channel_id, access_hash=5)


def channel(channel_id: int = SOURCE) -> Any:
    return types.InputChannel(channel_id=channel_id, access_hash=5)


def photo_part(file_id: int = PHOTO_ID, limit: int = 512 * 1024, **kw: Any) -> Any:
    location = types.InputPhotoFileLocation(id=file_id, access_hash=1, file_reference=b"r", thumb_size="y")
    return functions.upload.GetFileRequest(location=location, offset=0, limit=limit, **kw)


def refused(request: Any, scope: ChannelScope) -> None:
    with pytest.raises(RequestNotAllowed):
        check_request(request, scope)


# --- the allowlist ---------------------------------------------------------------------------------


def test_a_source_is_read_only_within_the_scope() -> None:
    scope = reading()
    check_request(functions.contacts.ResolveUsernameRequest(username="imtproto"), scope)
    check_request(history(peer()), scope)
    check_request(history(peer(), limit=MAX_SOURCE_HISTORY), scope)
    check_request(
        functions.channels.GetMessagesRequest(channel=channel(), id=[types.InputMessageID(id=7)]), scope
    )
    check_request(photo_part(), scope)
    check_request(functions.auth.ExportAuthorizationRequest(dc_id=4), scope)

    refused(functions.contacts.ResolveUsernameRequest(username="durov"), scope)  # not a source
    refused(history(peer(OTHER)), scope)  # a channel that is not a resolved source
    refused(history(peer(), limit=MAX_SOURCE_HISTORY + 1), scope)
    refused(history(peer(), limit=0), scope)
    refused(
        functions.channels.GetMessagesRequest(channel=channel(OTHER), id=[types.InputMessageID(id=7)]), scope
    )
    refused(
        functions.channels.GetMessagesRequest(
            channel=channel(), id=[types.InputMessageID(id=i) for i in range(1, 12)]
        ),
        scope,
    )
    refused(functions.channels.GetMessagesRequest(channel=channel(), id=[types.InputMessagePinned()]), scope)
    refused(photo_part(file_id=PHOTO_ID + 1), scope)  # a file this connection never read
    refused(photo_part(limit=2 * 1024 * 1024), scope)
    refused(photo_part(cdn_supported=True), scope)
    refused(functions.auth.ExportAuthorizationRequest(dc_id=9), scope)
    no_files = reading(files=set())
    refused(functions.auth.ExportAuthorizationRequest(dc_id=4), no_files)  # no download in progress
    for nothing in (
        ChannelScope(),
        ChannelScope(channel_ids=frozenset({SOURCE})),
    ):  # our channel is not a source
        refused(history(peer()), nothing)
        refused(photo_part(), nothing)
        refused(functions.auth.ExportAuthorizationRequest(dc_id=4), nothing)


def test_a_source_scope_still_writes_nowhere() -> None:
    scope = reading()
    for bad in (
        functions.messages.SendMessageRequest(peer=peer(), message="x"),
        functions.messages.ForwardMessagesRequest(from_peer=peer(), id=[7], to_peer=types.InputPeerSelf()),
        functions.channels.JoinChannelRequest(channel=channel()),
        functions.messages.ReadHistoryRequest(peer=peer(), max_id=7),
        functions.channels.ReadHistoryRequest(channel=channel(), max_id=7),
        functions.messages.GetMessagesViewsRequest(peer=peer(), id=[7], increment=True),
        functions.messages.GetDialogsRequest(
            offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=10, hash=0
        ),
        functions.auth.ImportAuthorizationRequest(id=1, bytes=b"x"),
        functions.messages.DeleteHistoryRequest(peer=peer(), max_id=0),
    ):
        refused(bad, scope)
    # The bot chat's own history read is unchanged: a user peer needs the open bot chat.
    with pytest.raises(RequestNotAllowed):
        check_request(history(types.InputPeerUser(user_id=93_372_553, access_hash=1), limit=5), scope)


# --- a post's file, as plain values --------------------------------------------------------------------


def tl_photo(*sizes: Any, dc_id: int = 4) -> Any:
    return types.Photo(
        id=PHOTO_ID, access_hash=11, file_reference=b"ref", date=T0, sizes=list(sizes), dc_id=dc_id
    )


def tl_video(
    size: int = 3_000_000, mime: str = "video/mp4", extra: list[Any] | None = None, thumbs: Any = None
) -> Any:
    attributes = [types.DocumentAttributeVideo(duration=12.0, w=640, h=360), *(extra or [])]
    return types.Document(
        id=DOC_ID,
        access_hash=12,
        file_reference=b"ref",
        date=T0,
        mime_type=mime,
        size=size,
        dc_id=5,
        attributes=attributes,
        thumbs=[types.PhotoSize(type="m", w=320, h=180, size=9_000)] if thumbs is None else thumbs,
    )


def test_photos_and_videos_become_plain_values_and_nothing_else_does() -> None:
    photo = userclient._source_file(
        types.MessageMediaPhoto(
            photo=tl_photo(
                types.PhotoStrippedSize(type="i", bytes=b"\x01"),
                types.PhotoSize(type="m", w=320, h=240, size=20_000),
                types.PhotoSizeProgressive(type="y", w=1280, h=960, sizes=[10_000, 90_000, 180_000]),
            )
        )
    )
    assert photo is not None and photo.media == SourceMedia(
        kind="photo", size=180_000, mime="image/jpeg", width=1280, height=960
    )
    assert photo.dc_id == 4 and photo.file_id == PHOTO_ID
    location, size = photo.location("photo", limit=100_000)  # the biggest size within the limit
    assert type(location) is types.InputPhotoFileLocation and location.thumb_size == "m" and size == 20_000
    with pytest.raises(MtpError, match="source_media_too_big"):
        photo.location("photo", limit=1_000)

    video = userclient._source_file(types.MessageMediaDocument(document=tl_video()))
    assert video is not None and video.media == SourceMedia(
        kind="video", size=3_000_000, mime="video/mp4", duration=12, width=640, height=360, has_thumb=True
    )
    location, size = video.location("video", limit=20 * 1024 * 1024)
    assert (
        type(location) is types.InputDocumentFileLocation and location.thumb_size == "" and size == 3_000_000
    )
    location, size = video.location("thumb", limit=1024 * 1024)
    assert location.thumb_size == "m" and size == 9_000
    with pytest.raises(MtpError, match="source_media_too_big"):  # refused before a byte is fetched
        video.location("video", limit=1_000_000)
    with pytest.raises(MtpError, match="source_media_gone"):
        video.location("photo", limit=1_000_000)

    for nothing in (
        types.MessageMediaDocument(document=tl_video(extra=[types.DocumentAttributeAnimated()])),  # a GIF
        types.MessageMediaDocument(document=tl_video(), round=True),
        types.MessageMediaDocument(document=tl_video(), ttl_seconds=10),
        types.MessageMediaPhoto(photo=tl_photo(types.PhotoSize(type="m", w=1, h=1, size=10)), ttl_seconds=5),
        types.MessageMediaPhoto(photo=tl_photo(types.PhotoStrippedSize(type="i", bytes=b"\x01"))),
        types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1)),
        None,
    ):
        assert userclient._source_file(nothing) is None


def post(i: int, text: str = "", media: SourceMedia | None = None, group: int | None = None) -> SourcePost:
    return SourcePost(
        id=i, date=T0 + timedelta(minutes=i), text=text, views=100 * i, grouped_id=group, media=media
    )


def test_items_keep_the_previews_ids_and_one_item_per_album() -> None:
    pic = SourceMedia(kind="photo", size=1000)
    clip = SourceMedia(kind="video", size=10_000, mime="video/mp4", duration=30, has_thumb=True)
    found = items_from(
        "imtproto",
        [
            post(30, "  خبر   تازه\n\n\n\nخط دوم  "),
            post(29, media=pic, group=77),
            post(28, "متن آلبوم", media=pic, group=77),
            post(27, media=pic, group=77),
            post(26, "ویدیو", media=clip),
            post(25),  # nothing to show: left out
        ],
    )
    assert [i.external_id for i in found] == ["30", "28", "26"]
    first, album, video = found
    assert first == FetchedItem(
        external_id="30",
        url="https://t.me/imtproto/30",
        published_at=T0 + timedelta(minutes=30),
        text="خبر تازه\n\nخط دوم",
        views=3000,
        media=None,
    )
    assert album.text == "متن آلبوم" and album.media is not None and album.media.url == "tgmsg:imtproto/28"
    assert video.media is not None and video.media.kind == "video" and video.media.duration == 30
    assert video.media.url == video.media.thumb_url == media_ref("imtproto", 26)
    assert parse_media_ref("tgmsg:imtproto/26") == ("imtproto", 26)
    for bad in ("tgmsg:imtproto/0", "tgmsg:IMT/1", "https://t.me/imtproto/26", "tgmsg:a/1", None):
        assert parse_media_ref(bad) is None


# --- a session: the methods go through the allowlist, then to a real refusal -------------------------


def _session(scope: ChannelScope) -> tuple[OperatorSession, contextlib.ExitStack]:
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)
    hold = contextlib.ExitStack()
    return OperatorSession(userclient._new_client(creds, None, options, scope), creds, options, hold), hold


async def test_the_session_reads_only_resolved_sources() -> None:
    ours = ChannelRef(channel_id=SOURCE, access_hash=5)
    other = ChannelRef(channel_id=OTHER, access_hash=5)
    op, hold = _session(reading())
    with hold:
        with pytest.raises(MtpError):  # allowed: a real connection refusal
            await op.read_source(ours)
        with pytest.raises(MtpError):
            await op.resolve_source("@iMTProto")
        with pytest.raises(MtpError):
            await op.source_message(ours, 7)
        with pytest.raises(RequestNotAllowed):
            await op.read_source(other)
        with pytest.raises(RequestNotAllowed):
            await op.resolve_source("durov")
        with pytest.raises(MtpError, match="source_media_gone"):  # never read on this connection
            await op.download_source_media(ours, 7, "photo", 1_000_000)
        message = types.Message(
            id=8,
            peer_id=types.PeerChannel(channel_id=SOURCE),
            date=T0,
            message="خبر",
            media=types.MessageMediaDocument(document=tl_video(size=30_000_000)),
            views=12,
        )
        found = op._source_post(ours, message)
        assert found.media is not None and found.media.size == 30_000_000 and found.views == 12
        assert DOC_ID in op._client.scope.source_files
        with pytest.raises(MtpError, match="source_media_too_big"):  # bigger than the panel takes
            await op.download_source_media(ours, 8, "video", 20 * 1024 * 1024)


# --- the reader and the engine on a real database ----------------------------------------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_content_tables(eng)
    await clean_account(eng)
    yield eng
    await clean_content_tables(eng)
    await clean_account(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


async def kill_switch(engine: AsyncEngine, on: bool) -> None:
    async with engine.begin() as conn:
        await save_fleet_settings(conn, FleetSettings(kill_switch=on))


def reader(engine: AsyncEngine, vault: Vault | None, port: int | None = None) -> AccountReader:
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", port or closed_port()), deadline_s=2)
    return AccountReader(
        engine=engine, vault=lambda: vault, clock=lambda: T0, options=options, lock_wait_s=0.2
    )


async def opened(r: AccountReader, names: set[str]) -> str | None:
    async with r.connect(names) as read:
        assert read is None
    return r.last_unavailable


@pytest.mark.db
async def test_the_reader_says_why_the_account_cannot_read(engine: AsyncEngine, vault: Vault) -> None:
    assert await opened(reader(engine, None), {"imtproto"}) == "vault_unavailable"
    assert await opened(reader(engine, vault), {"imtproto"}) == "kill_switch"  # no fleet row: ON
    await kill_switch(engine, False)
    assert await opened(reader(engine, vault), {"imtproto"}) == "not_connected"
    port = closed_port()
    await seed_connected(engine, vault, port=port)
    assert await opened(reader(engine, vault, port), {"imtproto"}) == "telegram_unavailable"
    async with mtp_store.account_lock(engine, wait_s=1):  # the poster or the sponsor code is working
        assert await opened(reader(engine, vault, port), {"imtproto"}) == "account_busy"
    held = reader(engine, vault, port)
    held.hold(30)  # a flood wait: at least a minute, nothing is even tried
    assert held.hold_until == T0 + timedelta(minutes=1)
    assert await opened(held, {"imtproto"}) == "account_flood"


async def add(engine: AsyncEngine, ref: str, kind: str = "telegram") -> sources.Source:
    async with engine.begin() as conn:
        return await sources.create(conn, kind=kind, ref=ref, label=None, now=T0)


async def source_row(engine: AsyncEngine, source_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            (await conn.execute(text("SELECT * FROM content_sources WHERE id = :i"), {"i": source_id}))
            .mappings()
            .one()
        )
    return dict(row)


class _Preview:
    """The preview plugin's place: records that the engine fell back to it (no network)."""

    kind = "telegram"

    def __init__(self) -> None:
        self.read: list[str] = []

    async def fetch(self, http: SafeHttp, ref: str) -> list[FetchedItem]:
        self.read.append(ref)
        raise FetchError("not_public")


@pytest.mark.db
async def test_the_engine_falls_back_to_the_preview_or_waits(engine: AsyncEngine, vault: Vault) -> None:
    source = await add(engine, "@imtproto")
    preview = _Preview()

    def make(r: AccountReader) -> Engine:
        return Engine(engine=engine, clock=lambda: T0, plugins={"telegram": preview}, account=r)  # type: ignore[dict-item]

    # The account is not usable (kill switch on): the web preview is read, as before D55.
    result = await make(reader(engine, vault)).run_round()
    assert preview.read == ["imtproto"] and result.read == {source.id: "not_public"}
    assert result.waiting == {}

    # The account is usable but busy: the source waits for a later round, with no error recorded.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE content_sources SET next_fetch_at = NULL, last_error = NULL, failures = 0")
        )
    await kill_switch(engine, False)
    port = closed_port()
    await seed_connected(engine, vault, port=port)
    async with mtp_store.account_lock(engine, wait_s=1):
        result = await make(reader(engine, vault, port)).run_round()
    assert result.waiting == {source.id: "account_busy"} and result.read == {}
    row = await source_row(engine, source.id)
    assert row["last_error"] is None and row["failures"] == 0 and row["next_fetch_at"] is None
    assert preview.read == ["imtproto"]  # not read again by its preview

    # A photo of a post the account read waits for the account; it is never fetched over https.
    pic = SourceMedia(kind="photo", size=1000)
    async with engine.begin() as conn:
        current = await sources.get(conn, source.id)
        assert current is not None
        news = "خبر تازه درباره‌ی اینترنت و فیلترینگ که متنی به اندازه‌ی کافی بلند دارد."
        await candidates.store_items(conn, current, items_from("imtproto", [post(5, news, media=pic)]), T0)
    await kill_switch(engine, True)
    result = await make(reader(engine, vault, port)).run_round()
    assert result.media == {}
    async with engine.connect() as conn:
        state = (await conn.execute(text("SELECT media_state, media_tries FROM content_candidates"))).one()
    assert tuple(state) == ("waiting", 0)
