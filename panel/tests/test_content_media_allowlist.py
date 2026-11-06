"""A photo or video post (decision D51) through both locks: the operator account's request allowlist
(proxyhub/mtproxy/userclient.py) and the bot's method allowlist (proxyhub/telegram/client.py).

Real Telethon request and type objects and a real, unconnected client on a local port nobody
listens on (as tests/test_operator_poster_allowlist.py): a refused request never leaves, an allowed
one goes on to a real connection refusal. No network. Each new guard has its own refusal below.
"""

from __future__ import annotations

import contextlib
import secrets
from typing import Any

import pytest
from telethon import functions, types
from test_mtproxy_units import closed_port
from test_telegram_support import closed_port_bot, random_token

from proxyhub.mtproxy import userclient
from proxyhub.mtproxy.userclient import (
    MAX_UPLOAD_PARTS,
    UPLOAD_PART_BYTES,
    ApiCredentials,
    ChannelRef,
    ChannelScope,
    ClientOptions,
    Endpoint,
    MtpError,
    OperatorSession,
    RequestNotAllowed,
    UploadedMedia,
    UploadFile,
    check_request,
)
from proxyhub.posts.entities import TextEntity
from proxyhub.telegram.client import ALLOWED_METHODS, BotClient, NetworkError
from proxyhub.vault import SecretValue

OURS = 1_987_654_321
OTHER = 1_987_654_322
PEER = types.InputPeerChannel(channel_id=OURS, access_hash=7_001)
FILE_ID = 4_242
CAPTION = "🔻 خبر کوتاه\n\nپروکسی ۱ • پروکسی ۲"


def posting(*, uploads: set[int] | None = None) -> ChannelScope:
    return ChannelScope(
        channel_ids=frozenset({OURS}),
        usernames=frozenset({"ipoxxo"}),
        post_ids=frozenset({OURS}),
        upload_ids={FILE_ID} if uploads is None else uploads,
    )


def stats_only() -> ChannelScope:
    return ChannelScope(channel_ids=frozenset({OURS}), usernames=frozenset({"ipoxxo"}), upload_ids={FILE_ID})


def photo(file: Any = None, **kw: Any) -> Any:
    return types.InputMediaUploadedPhoto(file=file or types.InputFile(FILE_ID, 1, "photo.jpg", ""), **kw)


def video(file: Any = None, mime: str = "video/mp4", attributes: list[Any] | None = None, **kw: Any) -> Any:
    attrs = (
        attributes
        if attributes is not None
        else [
            types.DocumentAttributeVideo(duration=12.0, w=640, h=360, supports_streaming=True),
            types.DocumentAttributeFilename(file_name="video.mp4"),
        ]
    )
    return types.InputMediaUploadedDocument(
        file=file or types.InputFileBig(FILE_ID, 30, "video.mp4"), mime_type=mime, attributes=attrs, **kw
    )


def send(media: Any, peer: Any = PEER, message: str = CAPTION, **kw: Any) -> Any:
    return functions.messages.SendMediaRequest(peer=peer, media=media, message=message, **kw)


def part(
    file_id: int = FILE_ID, number: int = 0, data: bytes = b"\x00" * 10, total: int | None = None
) -> Any:
    if total is None:
        return functions.upload.SaveFilePartRequest(file_id=file_id, file_part=number, bytes=data)
    return functions.upload.SaveBigFilePartRequest(
        file_id=file_id, file_part=number, file_total_parts=total, bytes=data
    )


def refused(request: Any, scope: ChannelScope | None = None) -> None:
    with pytest.raises(RequestNotAllowed):
        check_request(request, posting() if scope is None else scope)


def test_a_photo_and_a_video_post_with_their_upload_pass_in_our_channel() -> None:
    scope = posting()
    for request in (
        part(),
        part(number=MAX_UPLOAD_PARTS - 1, data=b"\x01" * UPLOAD_PART_BYTES),
        part(number=3, total=30),
        send(photo()),
        send(video()),
        send(photo(), entities=[types.MessageEntityBold(offset=0, length=4)]),
    ):
        check_request(request, scope)


@pytest.mark.parametrize(
    "request_",
    [
        send(photo(), peer=types.InputPeerChannel(channel_id=OTHER, access_hash=1)),  # another channel
        send(photo(), peer=types.InputPeerSelf()),
        send(photo(types.InputFile(999, 1, "photo.jpg", ""))),  # a file this connection did not upload
        send(photo(types.InputFile(FILE_ID, MAX_UPLOAD_PARTS + 1, "photo.jpg", ""))),
        send(
            types.InputMediaPhoto(id=types.InputPhoto(id=1, access_hash=1, file_reference=b""))
        ),  # someone's
        send(types.InputMediaDocument(id=types.InputDocument(id=1, access_hash=1, file_reference=b""))),
        send(types.InputMediaPhotoExternal(url="https://evil.example/x.jpg")),  # Telegram fetching a URL
        send(types.InputMediaDocumentExternal(url="https://evil.example/x.mp4")),
        send(types.InputMediaEmpty()),
        send(photo(spoiler=True)),
        send(photo(ttl_seconds=10)),
        send(video(mime="application/x-msdownload")),
        send(video(attributes=[types.DocumentAttributeFilename(file_name="x.mp4")])),  # no video attribute
        send(video(attributes=[types.DocumentAttributeVideo(duration=1.0, w=1, h=1, round_message=True)])),
        send(video(attributes=[types.DocumentAttributeAudio(duration=1)])),
        send(video(thumb=types.InputFile(FILE_ID, 1, "t.jpg", ""))),
        send(video(force_file=True)),
        send(photo(), silent=True),
        send(photo(), schedule_date=1_900_000_000),
        send(photo(), reply_markup=types.ReplyInlineMarkup(rows=[])),
        send(photo(), send_as=types.InputPeerSelf()),
        send(photo(), noforwards=True),
        send(photo(), message="x" * 4097),
        send(photo(), entities=[types.MessageEntityMentionName(offset=0, length=2, user_id=1)]),
        send(photo(), entities=[types.MessageEntityBold(offset=0, length=999)]),
        part(file_id=999),  # not this connection's file
        part(data=b""),
        part(data=b"\x00" * (UPLOAD_PART_BYTES + 1)),
        part(number=MAX_UPLOAD_PARTS),
        part(number=-1),
        part(number=5, total=5),
        part(total=MAX_UPLOAD_PARTS + 1),
    ],
)
def test_everything_else_about_media_is_refused(request_: Any) -> None:
    refused(request_)


def test_without_a_post_channel_nothing_is_uploaded_or_sent() -> None:
    for scope in (stats_only(), ChannelScope()):
        refused(part(), scope)
        refused(send(photo()), scope)
    refused(part(), posting(uploads=set()))  # no file chosen on this connection


def _session(scope: ChannelScope | None) -> tuple[OperatorSession, contextlib.ExitStack]:
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)
    hold = contextlib.ExitStack()
    return OperatorSession(userclient._new_client(creds, None, options, scope), creds, options, hold), hold


async def test_the_session_uploads_only_with_a_post_scope_and_registers_its_file() -> None:
    ref = ChannelRef(channel_id=OURS, access_hash=7_001)
    file = UploadFile(
        kind="photo", mime="image/jpeg", data=b"\xff\xd8\xff" + b"\x00" * 100, width=10, height=10
    )
    scope = posting(uploads=set())
    op, hold = _session(scope)
    with hold, pytest.raises(MtpError):  # passed the allowlist: a real connection failure
        await op.upload_media(file)
    assert len(scope.upload_ids) == 1  # the method chose the file id and registered it
    op, hold = _session(stats_only())
    with hold, pytest.raises(RequestNotAllowed):
        await op.upload_media(file)
    op, hold = _session(posting())
    with hold, pytest.raises(MtpError):
        await op.post_media_in_channel(ref, UploadedMedia(photo()), CAPTION, [TextEntity("bold", 0, 2)])
    op, hold = _session(stats_only())
    with hold, pytest.raises(RequestNotAllowed):
        await op.post_media_in_channel(ref, UploadedMedia(photo()), CAPTION, [])
    op, hold = _session(posting())
    with hold:
        with pytest.raises(ValueError):  # a video that is not MP4, or a file over 50 MB: never sent
            await op.upload_media(UploadFile(kind="video", mime="video/quicktime", data=b"x"))
        with pytest.raises(ValueError):
            await op.upload_media(UploadFile(kind="photo", mime="image/png", data=b""))


async def test_a_caption_edit_passes_like_a_text_edit() -> None:
    op, hold = _session(posting())
    ref = ChannelRef(channel_id=OURS, access_hash=7_001)
    with hold, pytest.raises(MtpError):
        await op.edit_channel_post(ref, 77, CAPTION, [], caption=True)
    check_request(functions.messages.EditMessageRequest(peer=PEER, id=77, message=CAPTION), posting())
    refused(functions.messages.EditMessageRequest(peer=PEER, id=77, message=CAPTION, media=photo()))


# --- the bot --------------------------------------------------------------------------------------


def test_the_bot_may_send_a_photo_a_video_and_edit_a_caption_and_nothing_more() -> None:
    assert {"sendPhoto", "sendVideo", "editMessageCaption"} <= ALLOWED_METHODS
    for name in ("sendDocument", "sendMediaGroup", "editMessageMedia", "copyMessage", "forwardMessage"):
        assert name not in ALLOWED_METHODS


async def test_the_bot_uploads_only_checked_files() -> None:
    async with closed_port_bot()(SecretValue(random_token())) as bot:
        assert isinstance(bot, BotClient)
        for bad in (
            bot.send_photo(-100123, b"", mime="image/jpeg", caption="x"),
            bot.send_photo(-100123, b"x", mime="image/gif", caption="x"),
            bot.send_photo(-100123, b"x" * (10 * 1024 * 1024 + 1), mime="image/jpeg", caption="x"),
            bot.send_video(-100123, b"", caption="x"),
            bot.send_video(-100123, b"x" * (50 * 1024 * 1024 + 1), caption="x"),
        ):
            with pytest.raises(ValueError):  # refused before any request (else NetworkError)
                await bad
        for allowed, method in (
            (bot.send_photo(-100123, b"\xff\xd8\xff", mime="image/jpeg", caption="<b>x</b>"), "sendPhoto"),
            (
                bot.send_video(-100123, b"\x00" * 16, caption="x", width=640, height=360, duration=3),
                "sendVideo",
            ),
            (bot.edit_message_caption(-100123, 7, "<b>x</b>"), "editMessageCaption"),
        ):
            with pytest.raises(NetworkError) as caught:  # passed the allowlist: a real refusal
                await allowed
            assert caught.value.method == method
