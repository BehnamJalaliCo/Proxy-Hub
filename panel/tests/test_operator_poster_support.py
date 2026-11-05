"""Shared helpers for the operator poster's tests (decision D38; decision D10: nothing imitated).

* The database, the vault, the advisory locks, the outbox and the publisher are the real ones.
* Telegram is never reached. A connection either goes to a local port nobody listens on (a real
  refusal), or, where a test needs Telegram's ANSWER, :func:`telegram_answers` replaces only the
  library's transport step (``TelegramClient._call`` and ``connect``): the real operator client,
  the real ``operator_session``, the real request objects and the real allowlist all run, every
  request is recorded, and the answer is a REAL Telethon TL object (or a real Telethon RPC error)
  built as data, the way Telegram's wire answer is read. Nothing leaves the process.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from telethon import TelegramClient, functions, types
from test_mtproxy_support import seed_connected
from test_mtproxy_units import closed_port
from test_posts_support import MAIN_CHAT, TEST_CHAT, channel, fleet_kill_switch, setup_telegram

from proxyhub.mtproxy.channel_stats import bare_channel_id
from proxyhub.mtproxy.userclient import check_request
from proxyhub.posts import operator
from proxyhub.telegram import store
from proxyhub.vault import Vault

TEST_NAME = "proxyhub_test_ch"
MAIN_NAME = "ipoxxo"
TEST_ID = bare_channel_id(TEST_CHAT)
MAIN_ID = bare_channel_id(MAIN_CHAT)
EMOJI_SET = "ProxyHubAnimated"
NOW = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)

OPERATOR_SQL = (
    "DELETE FROM settings WHERE key LIKE 'posts.operator%' OR key = 'mtproxy.account'",
    "DELETE FROM secrets WHERE name LIKE 'mtproxy:%'",
)


async def clean_operator(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for sql in OPERATOR_SQL:
            await conn.execute(text(sql))


async def setup_operator(
    engine: AsyncEngine,
    vault: Vault,
    *,
    connected: bool = True,
    kill_switch: bool = False,
    poster: operator.PosterName = "operator",
    emoji_pack: str | None = EMOJI_SET,
    public_test: bool = True,
    main: bool = False,
    port: int | None = None,
) -> None:
    """The bot, the owner's chat, the channels (the test channel public as @proxyhub_test_ch), the
    operator account (a real session string pointing at a closed local port) and the owner's choice."""
    await setup_telegram(engine, vault, test=True, main=main)
    async with engine.begin() as conn:
        if public_test:
            await store.save_channel(conn, "test", channel(TEST_CHAT, f"@{TEST_NAME}"))
        await operator.save_config(conn, operator.OperatorConfig(poster=poster, emoji_pack=emoji_pack))
    if connected:
        await seed_connected(engine, vault, port=port or closed_port())
    await fleet_kill_switch(engine, on=kill_switch)


async def store_pack(
    engine: AsyncEngine, entries: list[tuple[int, str]], short_name: str = EMOJI_SET
) -> None:
    async with engine.begin() as conn:
        await operator.save_pack(
            conn,
            operator.StoredPack(
                short_name=short_name,
                title="Animated",
                entries=tuple(entries),
                fetched_at=NOW,
                attempted_at=NOW,
            ),
        )


async def fallbacks(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        return await operator.recent_fallbacks(conn)


# --- Telegram's answers as real TL data ---------------------------------------------------------------------


def resolved(
    channel_id: int | None, username: str, *, access_hash: int = 7_001
) -> types.contacts.ResolvedPeer:
    """contacts.resolveUsername's answer for a public broadcast channel."""
    assert channel_id is not None
    return types.contacts.ResolvedPeer(
        peer=types.PeerChannel(channel_id=channel_id),
        chats=[
            types.Channel(
                id=channel_id,
                title="کانال",
                photo=types.ChatPhotoEmpty(),
                date=NOW,
                broadcast=True,
                access_hash=access_hash,
                username=username,
            )
        ],
        users=[],
    )


def sent(request: Any, message_id: int, channel_id: int | None) -> types.Updates:
    """messages.sendMessage's answer in a channel: our random id -> the new message id."""
    assert channel_id is not None
    message = types.Message(
        id=message_id,
        peer_id=types.PeerChannel(channel_id=channel_id),
        date=NOW,
        message=request.message,
        out=True,
    )
    return types.Updates(
        updates=[
            types.UpdateMessageID(id=message_id, random_id=request.random_id),
            types.UpdateNewChannelMessage(message=message, pts=100, pts_count=1),
        ],
        users=[],
        chats=[],
        date=NOW,
        seq=0,
    )


def edited() -> types.Updates:
    return types.Updates(updates=[], users=[], chats=[], date=NOW, seq=0)


def participant(
    *, post: bool = True, edit: bool = True, delete: bool = False
) -> types.channels.ChannelParticipant:
    rights = types.ChatAdminRights(post_messages=post, edit_messages=edit, delete_messages=delete)
    return types.channels.ChannelParticipant(
        participant=types.ChannelParticipantAdmin(
            user_id=5_000_001, promoted_by=1, date=NOW, admin_rights=rights, is_self=True, inviter_id=1
        ),
        chats=[],
        users=[],
    )


Answer = Callable[[Any], Any]


@contextlib.contextmanager
def telegram_answers(monkeypatch: pytest.MonkeyPatch, answer: Answer) -> Iterator[list[Any]]:
    """Replace the library's transport only (see the module doc). Yields the requests, in order,
    that passed the REAL allowlist of the operator client (a refused one raises as in production)."""
    requests: list[Any] = []

    async def no_connect(self: TelegramClient) -> None:
        return None

    async def transport(
        self: Any, sender: Any, request: Any, ordered: bool = False, flood_sleep_threshold: Any = None
    ) -> Any:
        for item in request if isinstance(request, list) else [request]:
            # The client's own allowlist let it through already; checked again so that no test can
            # pass with a request the allowlist would refuse.
            check_request(item, self.scope, bot_id=self._mtproxybot_id)
            requests.append(item)
        result = answer(request)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(TelegramClient, "connect", no_connect)
    monkeypatch.setattr(TelegramClient, "_call", transport)
    yield requests


def channel_answers(
    *, message_id: int = 901, send_error: BaseException | None = None, edit_error: BaseException | None = None
) -> Answer:
    def answer(request: Any) -> Any:
        if isinstance(request, functions.contacts.ResolveUsernameRequest):
            name = request.username.lower()
            return resolved(TEST_ID if name == TEST_NAME else MAIN_ID, name)
        if isinstance(request, functions.messages.SendMessageRequest):
            return send_error or sent(request, message_id, request.peer.channel_id)
        if isinstance(request, functions.messages.EditMessageRequest):
            return edit_error or edited()
        raise AssertionError(f"unexpected request {type(request).__name__}")

    return answer


def _emoji_document(doc_id: int, alt: str) -> types.Document:
    return types.Document(
        id=doc_id,
        access_hash=1,
        file_reference=b"",
        date=NOW,
        mime_type="video/webm",
        size=2048,
        dc_id=2,
        attributes=[
            types.DocumentAttributeCustomEmoji(
                alt=alt, stickerset=types.InputStickerSetShortName(short_name=EMOJI_SET)
            ),
            types.DocumentAttributeFilename(file_name="emoji.webm"),
        ],
    )


def pack_answer(entries: list[tuple[int, str]], *, emojis: bool = True, title: str = "متحرک") -> Any:
    """messages.getStickerSet's answer for the owner's custom-emoji pack."""
    return types.messages.StickerSet(
        set=types.StickerSet(
            id=1, access_hash=2, title=title, short_name=EMOJI_SET, count=len(entries), hash=0, emojis=emojis
        ),
        packs=[types.StickerPack(emoticon=alt, documents=[doc]) for doc, alt in entries],
        keywords=[],
        documents=[_emoji_document(doc, alt) for doc, alt in entries],
    )


def checks_answers(
    entries: list[tuple[int, str]],
    *,
    rights: Any = None,
    pack_error: BaseException | None = None,
    rights_error: BaseException | None = None,
    emojis: bool = True,
) -> Answer:
    """Answers for the daily reads: the pack, the channels' names and the account's own rights."""

    def answer(request: Any) -> Any:
        if isinstance(request, functions.messages.GetStickerSetRequest):
            return pack_error or pack_answer(entries, emojis=emojis)
        if isinstance(request, functions.contacts.ResolveUsernameRequest):
            name = request.username.lower()
            return resolved(TEST_ID if name == TEST_NAME else MAIN_ID, name)
        if isinstance(request, functions.channels.GetParticipantRequest):
            return rights_error or rights or participant()
        raise AssertionError(f"unexpected request {type(request).__name__}")

    return answer
