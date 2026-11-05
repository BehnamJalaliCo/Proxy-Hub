"""The operator account may post and edit in OUR channels only, never delete (decision D38, rule 6).

Everything here uses REAL Telethon request, type and error objects and a real, unconnected Telethon
client (a local port nobody listens on): the allowlist refuses before a byte leaves, and an allowed
request goes on to a real connection refusal. Telegram's answers are real TL objects built as data,
read by the same functions that read Telegram's real answers (decision D10). No network.

Each guard of :func:`proxyhub.mtproxy.userclient.check_request` that this slice adds has its own
refusal below, so removing any one of them fails a test.
"""

from __future__ import annotations

import contextlib
import secrets
from datetime import UTC, datetime
from typing import Any

import pytest
from telethon import functions, types
from telethon.extensions import BinaryReader
from test_mtproxy_units import closed_port

from proxyhub.mtproxy import userclient
from proxyhub.mtproxy.userclient import (
    MAX_POST_ENTITIES,
    MAX_POST_TEXT,
    POST_ENTITY_TYPES,
    ApiCredentials,
    ChannelRef,
    ChannelScope,
    ClientOptions,
    Endpoint,
    MtpError,
    OperatorSession,
    RequestNotAllowed,
    channel_post_id,
    channel_rights,
    check_request,
    emoji_pack_of,
    tl_entities,
    tl_entity,
)
from proxyhub.posts.entities import TextEntity
from proxyhub.vault import SecretValue

OURS = 1_987_654_321  # the configured channel's bare id (Bot API -1001987654321)
OTHER = 1_987_654_322
EMOJI_SET = "proxyhubanimated"
PEER = types.InputPeerChannel(channel_id=OURS, access_hash=7_001)
CHANNEL = types.InputChannel(channel_id=OURS, access_hash=7_001)
STATS_ONLY = ChannelScope(channel_ids=frozenset({OURS}), usernames=frozenset({"ipoxxo"}))
POSTING = ChannelScope(
    channel_ids=frozenset({OURS}), usernames=frozenset({"ipoxxo"}), post_ids=frozenset({OURS})
)
READING = ChannelScope(channel_ids=frozenset({OURS}), usernames=frozenset({"ipoxxo"}), emoji_set=EMOJI_SET)
TEXT = "📡 پروکسی تلگرام — روی هر کدام بزنید"


def _send(peer: object = PEER, message: str = TEXT, **kw: object) -> Any:
    return functions.messages.SendMessageRequest(peer=peer, message=message, **kw)


def _edit(peer: object = PEER, msg_id: object = 77, message: str | None = TEXT, **kw: object) -> Any:
    return functions.messages.EditMessageRequest(peer=peer, id=msg_id, message=message, **kw)


def _refused(request: object, scope: ChannelScope | None = POSTING, **kw: Any) -> None:
    with pytest.raises(RequestNotAllowed):
        check_request(request, scope, **kw)


# --- posts and edits -------------------------------------------------------------------------------------


ALL_ENTITIES = [
    types.MessageEntityBold(offset=0, length=2),
    types.MessageEntityItalic(offset=3, length=6),
    types.MessageEntityUnderline(offset=3, length=6),
    types.MessageEntityStrike(offset=3, length=6),
    types.MessageEntitySpoiler(offset=3, length=6),
    types.MessageEntityCode(offset=3, length=6),
    types.MessageEntityPre(offset=3, length=6, language=""),
    types.MessageEntityTextUrl(offset=3, length=6, url="https://t.me/proxy?server=a&port=1&secret=b"),
    types.MessageEntityBlockquote(offset=0, length=10, collapsed=True),
    types.MessageEntityCustomEmoji(offset=0, length=2, document_id=5_368_324_170_671_202_286),
]


def test_a_post_and_an_edit_pass_only_in_a_post_channel() -> None:
    assert {type(e) for e in ALL_ENTITIES} == POST_ENTITY_TYPES
    for request in (
        _send(),
        _send(no_webpage=True, entities=ALL_ENTITIES),
        _edit(),
        _edit(no_webpage=True, entities=ALL_ENTITIES),
    ):
        check_request(request, POSTING)
        _refused(request, None)  # no scope at all
        _refused(request, ChannelScope())
        _refused(request, STATS_ONLY)  # the statistics may READ this channel; they may not post
        _refused(request, READING)
    # The channel's statistics scope with the bot chat open: still no post (it is not the bot).
    _refused(_send(), STATS_ONLY, bot_id=OURS)


@pytest.mark.parametrize(
    "peer",
    [
        types.InputPeerChannel(channel_id=OTHER, access_hash=7_001),  # another channel
        types.InputPeerChannelFromMessage(peer=PEER, msg_id=1, channel_id=OURS),  # same id, other type
        types.InputPeerChat(chat_id=OURS),  # a basic group with the same number
        types.InputPeerUser(user_id=OURS, access_hash=7_001),  # a user with the same number
        types.InputPeerSelf(),  # Saved Messages
        types.InputPeerEmpty(),
        "ipoxxo",  # a username the library would resolve later
        OURS,
        -1_000_000_000_000 - OURS,  # the Bot API id
    ],
    ids=lambda p: type(p).__name__,
)
def test_no_other_peer_takes_a_post_or_an_edit(peer: object) -> None:
    _refused(_send(peer))
    _refused(_edit(peer))


@pytest.mark.parametrize(
    "extra",
    [
        {"reply_markup": types.ReplyInlineMarkup(rows=[])},
        {"schedule_date": datetime(2030, 1, 1, tzinfo=UTC)},
        {"schedule_repeat_period": 86_400},
        {"send_as": types.InputPeerSelf()},
        {"silent": True},
        {"background": True},
        {"clear_draft": True},
        {"noforwards": True},
        {"invert_media": True},
        {"update_stickersets_order": True},
        {"allow_paid_floodskip": True},
        {"allow_paid_stars": 1},
        {"effect": 5_104_841_245_755_180_586},
        {"reply_to": types.InputReplyToMessage(reply_to_msg_id=1)},
        {"quick_reply_shortcut": types.InputQuickReplyShortcutId(shortcut_id=1)},
    ],
    ids=lambda kw: next(iter(kw)),
)
def test_a_post_carries_nothing_else_along(extra: dict[str, Any]) -> None:
    _refused(_send(**extra))


@pytest.mark.parametrize(
    "extra",
    [
        {"media": types.InputMediaEmpty()},
        {"reply_markup": types.ReplyInlineMarkup(rows=[])},
        {"schedule_date": datetime(2030, 1, 1, tzinfo=UTC)},
        {"invert_media": True},
        {"quick_reply_shortcut_id": 1},
    ],
    ids=lambda kw: next(iter(kw)),
)
def test_an_edit_changes_the_text_and_nothing_else(extra: dict[str, Any]) -> None:
    _refused(_edit(**extra))


@pytest.mark.parametrize(
    "request_",
    [
        _send(message=""),
        _send(message="   \n "),
        _send(message="x" * (MAX_POST_TEXT + 1)),
        _send(message="📡" * (MAX_POST_TEXT // 2 + 1)),  # 2050 characters, 4100 UTF-16 code units
        _edit(message=None),  # an edit that would only touch the media or the markup
        _edit(message=""),
        _edit(msg_id=0),
        _edit(msg_id=-5),
        _edit(msg_id=True),
    ],
)
def test_the_text_and_the_post_id_are_checked(request_: object) -> None:
    _refused(request_)


def test_the_text_limit_counts_utf16_code_units() -> None:
    check_request(_send(message="📡" * (MAX_POST_TEXT // 2)), POSTING)  # exactly 4096 code units
    check_request(_send(message="ا" * MAX_POST_TEXT), POSTING)  # Persian letters: one unit each


@pytest.mark.parametrize(
    "entities",
    [
        [types.MessageEntityMentionName(offset=0, length=2, user_id=777_000)],
        [types.InputMessageEntityMentionName(offset=0, length=2, user_id=types.InputUserSelf())],
        [types.MessageEntityMention(offset=0, length=2)],
        [types.MessageEntityUrl(offset=0, length=2)],
        [types.MessageEntityHashtag(offset=0, length=2)],
        [types.MessageEntityBotCommand(offset=0, length=2)],
        [types.MessageEntityBold(offset=-1, length=2)],
        [types.MessageEntityBold(offset=0, length=0)],
        [types.MessageEntityBold(offset=0, length=len(TEXT.encode("utf-16-le")) // 2 + 1)],
        [types.MessageEntityTextUrl(offset=0, length=2, url="javascript:alert(1)")],
        [types.MessageEntityTextUrl(offset=0, length=2, url="")],
        [types.MessageEntityCustomEmoji(offset=0, length=2, document_id=True)],
        [types.MessageEntityBold(offset=0, length=1)] * (MAX_POST_ENTITIES + 1),
    ],
    ids=lambda e: f"{type(e[0]).__name__}x{len(e)}",
)
def test_only_our_formatting_rides_along(entities: list[Any]) -> None:
    _refused(_send(entities=entities))
    _refused(_edit(entities=entities))


def test_the_entity_bounds_count_utf16_code_units() -> None:
    units = len(TEXT.encode("utf-16-le")) // 2
    assert units == len(TEXT) + 1  # the one emoji is two code units
    check_request(_send(entities=[types.MessageEntityBold(offset=0, length=units)]), POSTING)
    _refused(_send(entities=[types.MessageEntityBold(offset=1, length=units)]))


# --- the two reads ---------------------------------------------------------------------------------------


def _pack(stickerset: object | None = None, hash_: int = 0) -> Any:
    return functions.messages.GetStickerSetRequest(
        stickerset=stickerset or types.InputStickerSetShortName(short_name=EMOJI_SET), hash=hash_
    )


def test_only_the_owners_pack_is_read() -> None:
    check_request(_pack(), READING)
    check_request(_pack(types.InputStickerSetShortName(short_name=EMOJI_SET.upper())), READING)
    _refused(_pack(), None)  # no pack configured on this connection
    _refused(_pack(), POSTING)
    _refused(_pack(types.InputStickerSetShortName(short_name="someone_elses_pack")), READING)
    _refused(_pack(types.InputStickerSetID(id=1, access_hash=1)), READING)
    _refused(_pack(types.InputStickerSetEmojiDefaultStatuses()), READING)
    _refused(_pack(types.InputStickerSetAnimatedEmoji()), READING)
    _refused(_pack(hash_=12_345), READING)


def _me(channel: object = CHANNEL, participant: object | None = None) -> Any:
    return functions.channels.GetParticipantRequest(
        channel=channel, participant=participant or types.InputPeerSelf()
    )


def test_only_the_accounts_own_rights_in_our_channels_are_read() -> None:
    check_request(_me(), STATS_ONLY)
    _refused(_me(), None)
    _refused(_me(types.InputChannel(channel_id=OTHER, access_hash=1)), STATS_ONLY)
    _refused(_me(types.InputChannelEmpty()), STATS_ONLY)
    _refused(_me(participant=types.InputPeerUser(user_id=777_000, access_hash=1)), STATS_ONLY)
    _refused(_me(participant=types.InputPeerChannel(channel_id=OURS, access_hash=1)), STATS_ONLY)
    _refused(_me(participant=types.InputPeerUserFromMessage(peer=PEER, msg_id=1, user_id=1)), STATS_ONLY)


# --- the same on a real Telethon client ---------------------------------------------------------------------


def _client(scope: ChannelScope | None) -> userclient._OperatorClient:
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    return userclient._new_client(
        creds, None, ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port())), scope
    )


async def test_the_client_refuses_before_connecting_and_lets_the_allowed_through() -> None:
    for scope in (None, STATS_ONLY):
        client = _client(scope)
        for request in (_send(), _edit(), _pack(), _me(types.InputChannel(channel_id=OTHER, access_hash=1))):
            with pytest.raises(RequestNotAllowed):
                await client(request)
    client = _client(POSTING)
    with pytest.raises(RequestNotAllowed):
        await client([_send(), functions.messages.DeleteMessagesRequest(id=[1], revoke=True)])
    for allowed in (_send(entities=ALL_ENTITIES), _edit()):
        with pytest.raises(ConnectionError):  # passed the allowlist: a real refusal by the network
            await client(allowed)


async def test_the_session_methods_build_allowed_requests_only() -> None:
    """The real methods, on a real client that never connected: with the post scope the request
    passes the allowlist (a real connection failure), without it nothing is even tried."""
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)
    ref = ChannelRef(channel_id=OURS, access_hash=7_001)
    emoji = [TextEntity("custom_emoji", 0, 2, document_id=5_368_324_170_671_202_286)]
    for scope, expected in ((POSTING, MtpError), (STATS_ONLY, RequestNotAllowed)):
        with contextlib.ExitStack() as hold:
            op = OperatorSession(userclient._new_client(creds, None, options, scope), creds, options, hold)
            with pytest.raises(expected):
                await op.post_in_channel(ref, TEXT, emoji)
            with pytest.raises(expected):
                await op.edit_channel_post(ref, 77, TEXT, emoji)
    with contextlib.ExitStack() as hold:
        op = OperatorSession(userclient._new_client(creds, None, options, READING), creds, options, hold)
        with pytest.raises(MtpError):
            await op.emoji_pack(EMOJI_SET)
        with pytest.raises(MtpError):
            await op.my_rights(ref)
        with pytest.raises(RequestNotAllowed):
            await op.emoji_pack("someone_elses_pack")
    with contextlib.ExitStack() as hold:
        op = OperatorSession(userclient._new_client(creds, None, options, POSTING), creds, options, hold)
        with pytest.raises(RequestNotAllowed):
            await op.emoji_pack(EMOJI_SET)
    with contextlib.ExitStack() as hold:
        op = OperatorSession(userclient._new_client(creds, None, options, None), creds, options, hold)
        with pytest.raises(RequestNotAllowed):
            await op.my_rights(ref)


# --- Telegram's answers, as real TL data --------------------------------------------------------------------


def _wire(obj: Any) -> Any:
    """The object as Telegram sends it: serialised and read back by Telethon."""
    return BinaryReader(bytes(obj)).tgread_object()


NOW = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)


def test_the_new_post_id_comes_from_our_random_id_or_our_channel() -> None:
    message = types.Message(
        id=901, peer_id=types.PeerChannel(channel_id=OURS), date=NOW, message=TEXT, out=True
    )
    answer = _wire(
        types.Updates(
            updates=[
                types.UpdateMessageID(id=901, random_id=42),
                types.UpdateNewChannelMessage(message=message, pts=10, pts_count=1),
            ],
            users=[],
            chats=[],
            date=NOW,
            seq=0,
        )
    )
    assert channel_post_id(answer, random_id=42, channel_id=OURS) == 901
    assert channel_post_id(answer, random_id=43, channel_id=OURS) == 901  # the channel's new message
    assert channel_post_id(answer, random_id=43, channel_id=OTHER) is None
    assert channel_post_id(_wire(types.UpdatesTooLong()), random_id=42, channel_id=OURS) is None


def test_the_accounts_rights_become_plain_data() -> None:
    def admin(**rights: bool) -> Any:
        return _wire(
            types.ChannelParticipantAdmin(
                user_id=5_000_001,
                promoted_by=1,
                date=NOW,
                admin_rights=types.ChatAdminRights(**rights),
                is_self=True,
                inviter_id=1,
            )
        )

    good = channel_rights(admin(post_messages=True, edit_messages=True))
    assert (good.status, good.can_post, good.can_edit, good.can_delete) == ("admin", True, True, False)
    wrong = channel_rights(admin(post_messages=True, edit_messages=True, delete_messages=True))
    assert wrong.can_delete is True
    assert channel_rights(admin(edit_messages=True)).can_post is False
    creator = channel_rights(
        _wire(
            types.ChannelParticipantCreator(user_id=1, admin_rights=types.ChatAdminRights(post_messages=True))
        )
    )
    assert (creator.status, creator.can_post) == ("creator", True)
    member = channel_rights(_wire(types.ChannelParticipantSelf(user_id=1, inviter_id=2, date=NOW)))
    assert (member.status, member.can_post, member.can_edit, member.can_delete) == (
        "member",
        False,
        False,
        False,
    )
    left = channel_rights(_wire(types.ChannelParticipantLeft(peer=types.PeerUser(user_id=1))))
    assert left.status == "left"


def _document(doc_id: int, alt: str | None) -> types.Document:
    attributes: list[Any] = [types.DocumentAttributeFilename(file_name="sticker.webm")]
    if alt is not None:
        attributes.append(
            types.DocumentAttributeCustomEmoji(
                alt=alt, stickerset=types.InputStickerSetShortName(short_name=EMOJI_SET)
            )
        )
    return types.Document(
        id=doc_id,
        access_hash=1,
        file_reference=b"",
        date=NOW,
        mime_type="video/webm",
        size=1024,
        dc_id=2,
        attributes=attributes,
    )


def sticker_set(*, emojis: bool = True) -> types.messages.StickerSet:
    """messages.StickerSet the way Telegram answers getStickerSet for a custom-emoji pack."""
    return types.messages.StickerSet(
        set=types.StickerSet(
            id=1, access_hash=2, title="Proxy-Hub ✨", short_name=EMOJI_SET, count=4, hash=0, emojis=emojis
        ),
        packs=[
            types.StickerPack(emoticon="📡", documents=[11]),
            types.StickerPack(emoticon="⌛️", documents=[12]),
            types.StickerPack(emoticon="🔗", documents=[13]),
        ],
        keywords=[],
        documents=[_document(11, "📡"), _document(12, "⌛️"), _document(13, None), _document(14, "📡")],
    )


def test_the_emoji_pack_becomes_plain_data() -> None:
    pack = emoji_pack_of(_wire(sticker_set()))
    assert (pack.short_name, pack.title, pack.is_emoji) == (EMOJI_SET, "Proxy-Hub ✨", True)
    # The alt comes from each emoji's own attribute, else from the pack's emoticon list; a document
    # with neither is skipped.
    assert pack.entries == ((11, "📡"), (12, "⌛️"), (13, "🔗"), (14, "📡"))
    assert emoji_pack_of(_wire(sticker_set(emojis=False))).is_emoji is False
    with pytest.raises(MtpError):
        emoji_pack_of(_wire(types.messages.StickerSetNotModified()))


def test_text_entities_become_real_tl_entities() -> None:
    plain = [
        TextEntity("bold", 0, 2),
        TextEntity("italic", 0, 2),
        TextEntity("underline", 0, 2),
        TextEntity("strike", 0, 2),
        TextEntity("spoiler", 0, 2),
        TextEntity("code", 0, 2),
        TextEntity("pre", 0, 2, language="python"),
        TextEntity("text_url", 0, 2, url="https://t.me/ipoxxo"),
        TextEntity("blockquote", 0, 2, collapsed=True),
        TextEntity("blockquote", 0, 2),
        TextEntity("custom_emoji", 0, 2, document_id=5_368_324_170_671_202_286),
    ]
    built = tl_entities(plain)
    assert {type(e) for e in built} == POST_ENTITY_TYPES
    for entity in built:
        assert bytes(_wire(entity)) == bytes(entity)  # Telegram's own wire format, both ways
    assert tl_entity(plain[6]).language == "python"
    assert tl_entity(plain[8]).collapsed is True and tl_entity(plain[9]).collapsed is None
    assert tl_entity(plain[10]).document_id == 5_368_324_170_671_202_286
    check_request(_send(entities=built), POSTING)  # every kind we build passes the allowlist


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("MESSAGE_NOT_MODIFIED", "telegram_refused"),
        ("MESSAGE_ID_INVALID", "telegram_refused"),
        ("CHAT_ADMIN_REQUIRED", "telegram_refused"),
        ("USER_NOT_PARTICIPANT", "telegram_refused"),
        ("STICKERSET_INVALID", "telegram_refused"),
        ("BOT_RESPONSE_TIMEOUT", "telegram_refused"),
        ("SOMETHING_NEW_FROM_TELEGRAM", "telegram_refused"),
    ],
)
def test_telegrams_error_name_survives_for_known_errors(name: str, code: str) -> None:
    """Telethon builds a known error from Telegram's wire answer with only a generic ``message``
    (BAD_REQUEST); the poster decides by the real name (not modified, not a member, ...)."""
    from telethon import errors

    from proxyhub.mtproxy.userclient import classify

    exc = errors.rpc_message_to_error(types.RpcError(error_code=400, error_message=name), None)
    mapped = classify(exc)
    assert mapped is not None and (mapped.code, mapped.rpc) == (code, name)


@pytest.mark.parametrize(
    ("link", "short_name"),
    [
        ("https://t.me/addemoji/ProxyHub_Anim", "ProxyHub_Anim"),
        ("t.me/addemoji/abc", "abc"),
        ("https://www.t.me/addemoji/abc/", "abc"),
        ("https://telegram.me/addemoji/abc", "abc"),
        ("tg://addemoji?set=abc", "abc"),
        (" https://T.ME/ADDEMOJI/abc ", "abc"),
    ],
)
def test_pack_links(link: str, short_name: str) -> None:
    from proxyhub.posts.operator import parse_pack_link

    assert parse_pack_link(link) == short_name


@pytest.mark.parametrize(
    ("link", "code"),
    [
        ("https://t.me/addstickers/abc", "pack_link_stickers"),
        ("tg://addstickers?set=abc", "pack_link_stickers"),
        ("https://t.me/addemoji/", "pack_link_invalid"),
        ("https://t.me/addemoji/a.b", "pack_link_invalid"),
        ("https://t.me/addemoji/abc/def", "pack_link_invalid"),
        ("https://t.me.evil.example/addemoji/abc", "pack_link_invalid"),
        ("https://evil.example/t.me/addemoji/abc", "pack_link_invalid"),
        ("javascript:alert(1)", "pack_link_invalid"),
        ("abc", "pack_link_invalid"),
        ("https://t.me/addemoji/" + "a" * 65, "pack_link_invalid"),
    ],
)
def test_bad_pack_links(link: str, code: str) -> None:
    from proxyhub.posts.operator import PackLinkInvalid, parse_pack_link

    with pytest.raises(PackLinkInvalid) as caught:
        parse_pack_link(link)
    assert caught.value.code == code
