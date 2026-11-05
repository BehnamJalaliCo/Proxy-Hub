"""The channel's own animated emoji pack (D38): the drawings (proxyhub/posts/emoji_art.py), the bot's
sticker-set methods, and the loop that makes the pack (proxyhub/posts/own_pack.py).

Telegram is never imitated (decision D10): the drawings are checked against Telegram's published
rules, the bot points at a closed local port (a real connection refusal) or, when reachable, at the
real api.telegram.org with a token-shaped value that Telegram refuses with its real 401.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_operator_poster_support import EMOJI_SET, clean_operator
from test_posts_support import (
    MAIN_CHAT,
    OWNER,
    channel,
    clean_posts_tables,
    fleet_kill_switch,
    setup_telegram,
    vault_at,
)
from test_telegram_support import all_logs, closed_port_bot, random_token, real_bot, telegram_reachable

from proxyhub.posts import emoji_art, operator, own_pack
from proxyhub.posts.entities import emoji_in, emoji_key
from proxyhub.telegram import store, templates
from proxyhub.telegram.client import (
    AnimatedEmoji,
    BotClient,
    NetworkError,
    StickerSetInfo,
    TelegramError,
    Unauthorized,
)
from proxyhub.vault import SecretValue, Vault

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
PACK = "ipoxxo_by_proxyhub_test_bot"


# --- the drawings ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("art", emoji_art.ART, ids=lambda a: a.name)
def test_every_drawing_keeps_telegrams_rules(art: emoji_art.EmojiArt) -> None:
    data = emoji_art.tgs(art)
    doc = json.loads(gzip.decompress(data))
    assert emoji_art.problems(doc, data) == []
    assert doc["w"] == doc["h"] == 512 and doc["fr"] == 60 and doc["op"] - doc["ip"] <= 180
    assert len(data) <= 64 * 1024


@pytest.mark.parametrize("art", emoji_art.ART, ids=lambda a: a.name)
def test_keyframes_run_forward_and_end_inside_the_loop(art: emoji_art.EmojiArt) -> None:
    for node in emoji_art._walk(emoji_art.lottie(art)):
        if node.get("a") == 1:
            times = [key["t"] for key in node["k"]]
            assert times == sorted(times) and len(set(times)) == len(times), (art.name, times)
            assert times[0] >= 0 and times[-1] <= emoji_art.FRAMES


def test_the_files_are_the_same_bytes_every_time() -> None:
    for art in emoji_art.ART:
        first = emoji_art.tgs(art)
        assert first == emoji_art.tgs(art)
        assert first[4:8] == b"\0\0\0\0"  # gzip without a timestamp


def test_every_alt_is_one_distinct_emoji() -> None:
    keys = [emoji_key(a.alt) for a in emoji_art.ART]
    assert len(keys) == len(set(keys))
    for art in emoji_art.ART:
        found = list(emoji_in(art.alt))
        assert len(found) == 1 and found[0].emoji == art.alt, art.name
    assert len({a.name for a in emoji_art.ART}) == len(emoji_art.ART)
    assert len(emoji_art.ART) <= 50  # one createNewStickerSet request


def test_the_pack_draws_every_emoji_of_the_post_templates() -> None:
    texts = [
        templates.PROXY_HEADER,
        templates.TAG_PREFIX,
        templates.MTPROTO_HEADER,
        templates.REALITY_HEADER,
        templates.ALL_DEAD_LINE,
        templates.PAUSED_LINE,
        templates.PAUSED_MARK,
        templates.LIVE_MARK,
        "⏳",  # a config's end time
        *templates.STATUS_MARKS.values(),
    ]
    used = {emoji_key(e.emoji) for t in texts for e in emoji_in(t)}
    drawn = {emoji_key(a.alt) for a in emoji_art.ART}
    assert used and used <= drawn, used - drawn


def test_the_rules_check_finds_what_telegram_refuses() -> None:
    art = emoji_art.ART[0]
    doc = emoji_art.lottie(art)
    data = emoji_art.tgs(art)
    bad = json.loads(json.dumps(doc))
    bad["w"] = 100
    bad["fr"] = 30
    bad["op"] = 240
    bad["layers"][0]["ty"] = 5
    bad["layers"][0]["hasMask"] = True
    bad["layers"][0]["ef"] = [{}]
    bad["layers"][0]["ddd"] = 1
    bad["layers"][0]["shapes"].append({"ty": "rp"})
    bad["layers"][0]["shapes"].append(
        {"ty": "sh", "ks": {"a": 0, "k": {"i": [[0, 0]], "o": [[0, 0]], "v": [[600, 5]], "c": False}}}
    )
    found = emoji_art.problems(bad, data + bytes(64 * 1024))
    for problem in (
        "canvas is not 512x512",
        "not 60 fps",
        "longer than 3 seconds",
        "text layer",
        "mask",
        "layer effect",
        "3D layer",
        "repeater",
        "drawn outside the canvas",
    ):
        assert problem in found
    assert any("bytes > 64 KB" in p for p in found)


# --- pure parts of the loop ------------------------------------------------------------------------------


def test_the_pack_name_follows_telegrams_rules() -> None:
    assert own_pack.pack_name("@ipoxxo", "ProxyHub_Bot") == "ipoxxo_by_proxyhub_bot"
    assert own_pack.pack_name("ipoxxo_", "x_bot") == "ipoxxo_by_x_bot"  # no "__"
    assert own_pack.pack_name("a__b", "x_bot") is None
    assert own_pack.pack_name("1abc", "x_bot") is None  # must start with a letter
    assert own_pack.pack_name("a" * 60, "x_bot") is None  # longer than 64
    assert own_pack.pack_name("", "x_bot") is None
    assert own_pack.pack_title("@ipoxxo") == own_pack.pack_title("ipoxxo") == "@ipoxxo"


def test_missing_ignores_variation_selectors() -> None:
    all_alts = [a.alt for a in emoji_art.ART]
    assert own_pack.missing(all_alts) == []
    plain = [emoji_key(a) for a in all_alts]  # "⏸" for "⏸️": the same emoji
    assert own_pack.missing(plain) == []
    lacking = own_pack.missing(all_alts[:-2])
    assert lacking == list(emoji_art.ART[-2:])


def test_when_a_check_is_due() -> None:
    full = tuple(a.alt for a in emoji_art.ART)
    ok = own_pack.PackState(name=PACK, alts=full, checked_at=NOW, attempted_at=NOW)
    assert own_pack.due(None, PACK, NOW)
    assert own_pack.due(ok, "other_by_proxyhub_test_bot", NOW)  # the channel or bot changed
    assert not own_pack.due(ok, PACK, NOW + timedelta(hours=23))
    assert own_pack.due(ok, PACK, NOW + timedelta(hours=24))
    grown = own_pack.PackState(name=PACK, alts=full[:-1], checked_at=NOW, attempted_at=NOW)
    assert own_pack.due(grown, PACK, NOW)  # a drawing was added to the repository
    failed = own_pack.PackState(name=PACK, attempted_at=NOW, error="x")
    assert not own_pack.due(failed, PACK, NOW + timedelta(minutes=59))
    assert own_pack.due(failed, PACK, NOW + timedelta(hours=1))


def test_a_429_waits_exactly_what_telegram_asked() -> None:
    """P17: createNewStickerSet got "429 retry after 301" twice; the next try waited a fixed hour."""
    found = own_pack.Target(name=PACK, title="@ipoxxo", owner_id=OWNER)
    talk = own_pack.Talk(
        "rate_limited", error="createNewStickerSet: 429 Too Many Requests", retry_after_s=301
    )
    st = own_pack.failed_state(None, found, talk, NOW)
    assert st.retry_at == NOW + timedelta(seconds=301) + own_pack.RETRY_MARGIN and st.error
    assert not own_pack.due(st, PACK, NOW + timedelta(seconds=300))
    assert own_pack.due(st, PACK, NOW + timedelta(seconds=307))  # not an hour
    # A partly filled pack keeps what it had through a failure.
    partial = own_pack.PackState(name=PACK, title="@ipoxxo", alts=("📡", "📶"), checked_at=NOW)
    kept = own_pack.failed_state(partial, found, own_pack.Talk("failed", error="x"), NOW)
    assert kept.alts == ("📡", "📶") and kept.retry_at is None and kept.title == "@ipoxxo"
    assert own_pack.due(kept, PACK, NOW + own_pack.AFTER_FAILURE)
    assert not own_pack.due(kept, PACK, NOW + own_pack.AFTER_FAILURE - timedelta(seconds=1))
    # The next minute's step fills it a few at a time, until it is complete.
    filling = own_pack.PackState(name=PACK, alts=("📡",), checked_at=NOW, attempted_at=NOW)
    assert own_pack.due(filling, PACK, NOW + timedelta(minutes=1)) and not own_pack.complete(filling)
    full = own_pack.PackState(name=PACK, alts=tuple(a.alt for a in emoji_art.ART), checked_at=NOW)
    assert own_pack.complete(full) and own_pack.ADD_PER_RUN <= 5


# --- the bot's sticker-set methods -------------------------------------------------------------------------


def _one() -> AnimatedEmoji:
    return own_pack.upload(emoji_art.ART[0])


async def test_sticker_set_calls_check_their_input_before_sending() -> None:
    # A refused port: had anything been sent, this would be a NetworkError, not a ValueError.
    async with closed_port_bot()(SecretValue(random_token())) as bot:
        for name in ("", "1pack", "a__b_by_x", "a" * 65, "pack/../x", "pack name"):
            with pytest.raises(ValueError, match="sticker set name"):
                await bot.get_sticker_set(name)
            with pytest.raises(ValueError, match="sticker set name"):
                await bot.add_custom_emoji(user_id=OWNER, name=name, item=_one())
        with pytest.raises(ValueError, match="title"):
            await bot.create_custom_emoji_set(user_id=OWNER, name=PACK, title="", emoji=[_one()])
        with pytest.raises(ValueError, match="1 to 50"):
            await bot.create_custom_emoji_set(user_id=OWNER, name=PACK, title="@x", emoji=[])
        with pytest.raises(ValueError, match="1 to 50"):
            await bot.create_custom_emoji_set(user_id=OWNER, name=PACK, title="@x", emoji=[_one()] * 51)
        with pytest.raises(ValueError, match="64 KB"):
            await bot.add_custom_emoji(
                user_id=OWNER, name=PACK, item=AnimatedEmoji(tgs=bytes(64 * 1024 + 1), alt="📡")
            )
        with pytest.raises(ValueError, match="alt"):
            await bot.add_custom_emoji(user_id=OWNER, name=PACK, item=AnimatedEmoji(tgs=b"x", alt=""))
        with pytest.raises(NetworkError):  # well-formed: it is really sent (multipart) and refused
            await bot.create_custom_emoji_set(user_id=OWNER, name=PACK, title="@ipoxxo", emoji=[_one()])


def test_a_sticker_set_answer_is_read_without_trusting_it() -> None:
    info = StickerSetInfo.from_result(
        {
            "name": PACK,
            "title": "@ipoxxo",
            "sticker_type": "custom_emoji",
            "stickers": [{"emoji": "📡", "custom_emoji_id": "1"}, {"custom_emoji_id": "2"}, "junk"],
        }
    )
    assert info == StickerSetInfo(name=PACK, title="@ipoxxo", is_custom_emoji=True, alts=("📡",))
    regular = StickerSetInfo.from_result({"name": "x", "sticker_type": "regular", "stickers": []})
    assert not regular.is_custom_emoji
    for bad in ({}, {"name": "x"}, [], "x", {"name": "", "stickers": []}):
        with pytest.raises(TelegramError):
            StickerSetInfo.from_result(bad)


async def test_telegram_refuses_a_wrong_token_on_the_multipart_path() -> None:
    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    token = random_token()
    with all_logs(logging.DEBUG) as logs:
        async with real_bot(token) as bot:
            with pytest.raises(Unauthorized):
                await bot.create_custom_emoji_set(user_id=OWNER, name=PACK, title="@ipoxxo", emoji=[_one()])
    assert token not in logs.getvalue() and token.split(":")[1] not in logs.getvalue()


# --- the loop on the real PostgreSQL ----------------------------------------------------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)

    async def clean() -> None:
        await clean_posts_tables(eng)
        await clean_operator(eng)
        async with eng.begin() as conn:
            await conn.execute(text("DELETE FROM settings WHERE key = 'posts.own_pack'"))
            await conn.execute(text("DELETE FROM actions_log WHERE action LIKE 'posts.own_pack%'"))

    await clean()
    yield eng
    await clean()
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return vault_at(tmp_path)


async def _target(engine: AsyncEngine) -> Any:
    async with engine.connect() as conn:
        return await own_pack.target(conn)


async def _state(engine: AsyncEngine) -> own_pack.PackState | None:
    async with engine.connect() as conn:
        return await own_pack.load_state(conn)


async def _config(engine: AsyncEngine) -> operator.OperatorConfig:
    async with engine.connect() as conn:
        return await operator.load_config(conn)


@pytest.mark.db
async def test_the_pack_waits_for_the_bot_the_owner_and_a_public_main_channel(
    engine: AsyncEngine, vault: Vault
) -> None:
    assert await _target(engine) == "kill_switch"  # a fresh panel starts with it ON
    await fleet_kill_switch(engine, on=False)
    assert await _target(engine) == "no_bot"
    await setup_telegram(engine, vault, test=True, main=False, owner=False)
    assert await _target(engine) == "no_owner"
    await setup_telegram(engine, vault, test=True, main=False, owner=True)
    assert await _target(engine) == "no_public_channel"
    async with engine.begin() as conn:
        await store.save_channel(conn, "main", channel(MAIN_CHAT))  # private: no @username
    assert await _target(engine) == "no_public_channel"
    await setup_telegram(engine, vault, test=True, main=True, owner=True)
    assert await _target(engine) == own_pack.Target(name=PACK, title="@ipoxxo", owner_id=OWNER)
    await fleet_kill_switch(engine, on=True)
    assert await _target(engine) == "kill_switch"
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "kill_switch"
    await fleet_kill_switch(engine, on=False)
    async with engine.connect() as conn:
        assert (await own_pack.status(conn))["state"] == "pending"


@pytest.mark.db
async def test_a_failed_try_is_kept_and_retried_an_hour_later(engine: AsyncEngine, vault: Vault) -> None:
    await fleet_kill_switch(engine, on=False)
    await setup_telegram(engine, vault, test=True, main=True, owner=True)
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "failed"
    state = await _state(engine)
    assert state is not None and state.name == PACK and state.error and state.attempted_at == NOW
    assert state.alts == () and state.owner_id == OWNER
    async with engine.connect() as conn:
        creds = await store.load_bot(conn, vault)
    assert creds is not None and creds.token.reveal().split(":")[1] not in (state.error or "")
    assert (await _config(engine)).emoji_pack is None  # nothing usable yet: the owner's choice stays
    later = NOW + timedelta(minutes=30)
    assert await own_pack.ensure(engine, vault, now=later, bot_factory=closed_port_bot()) == "not_due"
    hour = NOW + timedelta(hours=1)
    assert await own_pack.ensure(engine, vault, now=hour, bot_factory=closed_port_bot()) == "failed"
    async with engine.connect() as conn:
        shown = await own_pack.status(conn)
    assert shown["state"] == "error" and shown["link"] is None and shown["total"] == len(emoji_art.ART)


@pytest.mark.db
async def test_a_ready_pack_becomes_the_posters_pack_only_when_the_owner_chose_none(
    engine: AsyncEngine, vault: Vault
) -> None:
    await fleet_kill_switch(engine, on=False)
    await setup_telegram(engine, vault, test=True, main=True, owner=True)
    full = tuple(a.alt for a in emoji_art.ART)
    async with engine.begin() as conn:
        await own_pack.save_state(
            conn,
            own_pack.PackState(
                name=PACK, title="@ipoxxo", owner_id=OWNER, alts=full, checked_at=NOW, attempted_at=NOW
            ),
        )
        await operator.save_config(conn, operator.OperatorConfig(poster="operator", emoji_pack=EMOJI_SET))
    # a partly filled pack is not chosen yet (the poster reads the pack once a day)
    async with engine.begin() as conn:
        await own_pack.save_state(
            conn,
            own_pack.PackState(
                name=PACK, title="@ipoxxo", owner_id=OWNER, alts=full[:3], checked_at=NOW, attempted_at=NOW
            ),
        )
        await operator.save_config(conn, operator.OperatorConfig(poster="operator", emoji_pack=None))
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "failed"
    assert (await _config(engine)).emoji_pack is None
    async with engine.begin() as conn:
        await own_pack.save_state(
            conn,
            own_pack.PackState(
                name=PACK, title="@ipoxxo", owner_id=OWNER, alts=full, checked_at=NOW, attempted_at=NOW
            ),
        )
        await operator.save_config(conn, operator.OperatorConfig(poster="operator", emoji_pack=EMOJI_SET))
    # the owner's own pack stays
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "not_due"
    assert (await _config(engine)).emoji_pack == EMOJI_SET
    # an empty choice takes the channel's pack (and keeps the poster)
    async with engine.begin() as conn:
        await operator.save_config(conn, operator.OperatorConfig(poster="operator", emoji_pack=None))
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "not_due"
    assert await _config(engine) == operator.OperatorConfig(poster="operator", emoji_pack=PACK)
    async with engine.connect() as conn:
        chosen = (
            await conn.execute(text("SELECT target FROM actions_log WHERE action = 'posts.own_pack_chosen'"))
        ).all()
        shown = await own_pack.status(conn)
    assert [r[0] for r in chosen] == [PACK]
    assert shown["state"] == "ready" and shown["link"] == f"https://t.me/addemoji/{PACK}"
    assert shown["emoji"] == shown["total"] == len(emoji_art.ART)


@pytest.mark.db
async def test_the_loop_is_registered_and_needs_the_vault(engine: AsyncEngine, vault: Vault) -> None:
    from proxyhub.reconcilers.operator_poster import OwnEmojiPackReconciler
    from proxyhub.vault import VaultKeyError
    from proxyhub.worker import reconcilers

    loops = {r.name: r for r in reconcilers()}
    assert isinstance(loops["own_emoji_pack"], OwnEmojiPackReconciler)
    assert loops["own_emoji_pack"].bot_factory is BotClient

    def no_vault() -> Vault:
        raise VaultKeyError("no master key")

    broken = OwnEmojiPackReconciler(vault_factory=no_vault)
    assert await broken.try_run_once(engine) and broken.last_outcome == "vault_unavailable"
    await fleet_kill_switch(engine, on=False)
    idle = OwnEmojiPackReconciler(vault_factory=lambda: vault, bot_factory=closed_port_bot())
    assert await idle.try_run_once(engine) and idle.last_outcome == "no_bot"
    with pytest.raises(NotImplementedError):
        async with engine.begin() as conn:
            await idle.run_once(conn)
