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
import math
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
PACK = f"ipoxxo_v{emoji_art.ART_VERSION}_by_proxyhub_test_bot"
PACK_V1 = "ipoxxo_by_proxyhub_test_bot"  # the first pack (2026-11-05), still on the owner's account


# --- the drawings ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("art", emoji_art.ART, ids=lambda a: a.name)
def test_every_drawing_keeps_telegrams_rules(art: emoji_art.EmojiArt) -> None:
    data = emoji_art.tgs(art)
    doc = json.loads(gzip.decompress(data))
    assert emoji_art.problems(doc, data) == []
    assert doc["w"] == doc["h"] == 512 and doc["fr"] == 60 and doc["op"] - doc["ip"] <= 180
    assert len(data) <= 64 * 1024
    assert doc["ip"] == 0 and doc["op"] == emoji_art.FRAMES <= emoji_art.MAX_FRAMES == 3 * 60
    for layer in doc["layers"]:  # one plain shape layer over the whole animation
        assert layer["ty"] == 4 and layer["sr"] == 1 and not layer["ddd"] and not layer["ao"]
        assert layer["ip"] <= doc["ip"] and layer["op"] >= doc["op"]


# The pack's drawings as of each ART_VERSION. Telegram never replaces an emoji of a pack (D43), so a
# changed drawing must go into a NEW pack: bump emoji_art.ART_VERSION and add the new digest here.
ART_DIGESTS = {2: "a16b7c51d4b319e7f18a1b7e1e96bf6ce8e674b1098ab1755a33d662f611bfd7"}


def test_a_changed_drawing_gets_a_new_pack_version() -> None:
    assert emoji_art.digest() == ART_DIGESTS.get(emoji_art.ART_VERSION), (
        "the drawings changed: bump emoji_art.ART_VERSION (the panel then builds a new pack beside the "
        "old one and moves the posts to it once complete), then add the new digest to ART_DIGESTS"
    )
    assert len(set(ART_DIGESTS.values())) == len(ART_DIGESTS) and emoji_art.ART_VERSION == max(ART_DIGESTS)


@pytest.mark.parametrize("art", emoji_art.ART, ids=lambda a: a.name)
def test_every_frame_stays_inside_the_canvas_with_a_margin(art: emoji_art.EmojiArt) -> None:
    """Every frame (both ends of the loop too), with its strokes and every scale, turn and move."""
    boxes = emoji_art.frame_boxes(emoji_art.lottie(art))
    assert len(boxes) == emoji_art.FRAMES + 1
    for frame, box in enumerate(boxes):
        assert box is not None, (art.name, frame)
        x0, y0, x1, y1 = box
        assert min(x0, y0) >= emoji_art.SAFE_MIN >= 24, (art.name, frame, box)
        assert max(x1, y1) <= emoji_art.SAFE_MAX <= 512 - 24, (art.name, frame, box)


@pytest.mark.parametrize("art", emoji_art.ART, ids=lambda a: a.name)
def test_every_drawing_fills_its_square_like_a_font_emoji(art: emoji_art.EmojiArt) -> None:
    """The first pack spanned 55 to 84% of the canvas (most about 75%), some off centre, and looked
    small or "half" beside a normal emoji in a post. Now: 90% over the loop, centred, every frame
    at least 70%."""
    doc = emoji_art.lottie(art)
    boxes = [b for b in emoji_art.frame_boxes(doc) if b is not None]
    x0, y0, x1, y1 = emoji_art.union(boxes) or (0, 0, 0, 0)
    assert 0.88 * 512 <= max(x1 - x0, y1 - y0) <= 512 - 2 * 24, art.name
    assert abs((x0 + x1) / 2 - 256) <= 2 and abs((y0 + y1) / 2 - 256) <= 2, art.name
    for frame, (a, b, c, d) in enumerate(boxes):
        assert max(c - a, d - b) >= 0.7 * 512, (art.name, frame)
    assert emoji_art.look_problems(doc) == []


def _values(p: dict[str, Any]) -> list[float]:
    """Every value an animatable number takes at a key (eased keys never overshoot them)."""
    return [p["k"]] if not p["a"] else [k["s"][0] for k in p["k"]]


@pytest.mark.parametrize("art", emoji_art.ART, ids=lambda a: a.name)
def test_no_part_of_a_drawing_fades_out(art: emoji_art.EmojiArt) -> None:
    """Every fill and stroke, times its groups' and its layer's opacity, at every key: at least 60%
    (the first pack's 📡 waves went to 25% and its 📶 bars, ⚡ glow and 🔋 cells to 0%). Only a
    "shine" (a still, light fill over the glyph) may be lighter. No trim path (a half-drawn line)."""
    doc = emoji_art.lottie(art)
    low: list[str] = []

    def walk(items: list[dict[str, Any]], floor: float, shine: bool) -> None:
        tr = [it for it in items if it.get("ty") == "tr"]
        here = floor * (min(_values(tr[0]["o"])) / 100 if tr else 1)
        for it in items:
            if it.get("ty") == "gr":
                walk(it["it"], here, shine or it.get("nm") == emoji_art.SHINE)
            elif it.get("ty") == "tm":
                low.append("trim path")
            elif it.get("ty") in ("fl", "st") and shine:
                assert it["ty"] == "fl" and it["o"]["a"] == 0 and not tr[0]["o"]["a"], art.name
            elif it.get("ty") in ("fl", "st") and here * min(_values(it["o"])) < 60:
                low.append(f"{it['ty']} {here * min(_values(it['o'])):.0f}%")

    for layer in doc["layers"]:
        walk(layer["shapes"], min(_values(layer["ks"]["o"])) / 100, False)
    assert low == [], art.name
    assert emoji_art.faint_parts(doc) == []


# --- what a frame draws, on known shapes --------------------------------------------------------------


def _doc(*shapes: dict[str, Any], op: int = 20) -> dict[str, Any]:
    """A bare one-layer animation around ``shapes`` (not fitted, unlike emoji_art.emoji)."""
    ks = {
        "o": emoji_art.prop(100),
        "r": emoji_art.prop(0),
        "p": emoji_art.prop([0, 0, 0]),
        "a": emoji_art.prop([0, 0, 0]),
        "s": emoji_art.prop([100, 100, 100]),
    }
    layer = {"ty": 4, "nm": "t", "ks": ks, "shapes": list(shapes), "ip": 0, "op": op, "sr": 1}
    return {"tgs": 1, "fr": 60, "ip": 0, "op": op, "w": 512, "h": 512, "layers": [layer]}


def _box(doc: dict[str, Any], frame: float) -> tuple[float, ...]:
    box = emoji_art.frame_box(doc, frame)
    assert box is not None
    return tuple(round(v, 6) for v in box)


def test_the_frame_box_follows_every_transform_and_stroke() -> None:
    e = emoji_art
    # A 100x40 bar with a 20 px stroke, turned 90 degrees: 60 wide, 120 tall.
    bar = e.group(e.rect(256, 256, 100, 40), e.stroke(e.NAVY, 20), t=e.transform(rot=90))
    assert _box(_doc(bar), 0) == (226, 196, 286, 316)
    # A circle stretched twice as wide: its stroke stretches with it (5 px -> 10 across, 5 down).
    oval = e.group(e.ellipse(256, 256, 100), e.stroke(e.NAVY, 10), t=e.transform(scale=[200, 100]))
    assert _box(_doc(oval), 0) == (146, 201, 366, 311)
    # A square turned 45 degrees reaches its corners' distance; a fill adds nothing.
    r = 50 * math.sqrt(2)
    diamond = e.group(e.rect(256, 256, 100, 100), e.fill(e.GOLD), t=e.transform(rot=45))
    assert _box(_doc(diamond), 0) == pytest.approx((256 - r, 256 - r, 256 + r, 256 + r), abs=1e-6)
    # Nested groups: an inner 50% scale around (300, 256), then the outer group moved 40 px right.
    inner = e.group(e.rect(300, 256, 40, 40), e.fill(e.GOLD), t=e.transform((300, 256), scale=50))
    assert _box(_doc(e.group(inner, t=e.transform(pos=[296, 256]))), 0) == (330, 246, 350, 266)
    # A curve's extreme between its vertices: half a circle bulging down to y = 356.
    bowl = e.group(e.shape(e.arc(256, 256, 100, 0, 180)), e.stroke(e.NAVY, 2))
    assert _box(_doc(bowl), 0) == (155, 255, 357, 357)
    # A stroke in an outer group grows the inner group's shapes, at the outer group's scale.
    outer = e.group(e.group(e.rect(256, 256, 100, 100)), e.stroke(e.NAVY, 20), t=e.transform(scale=50))
    assert _box(_doc(outer), 0) == (226, 226, 286, 286)


def test_the_frame_box_follows_keyframes() -> None:
    e = emoji_art

    def square(pos: Any) -> dict[str, Any]:
        return e.group(e.rect(256, 256, 20, 20), e.fill(e.GOLD), t=e.transform(pos=pos))

    eased = _doc(square(e.anim((0, [256, 256]), (10, [356, 256]))))
    assert _box(eased, 0) == (246, 246, 266, 266)
    assert _box(eased, 5) == (296, 246, 316, 266)  # the ease curve is symmetric: halfway at half time
    assert _box(eased, 2)[0] < 246 + 100 * 0.2  # slow at the start
    assert _box(eased, 10) == _box(eased, 15) == (346, 246, 366, 266)  # the last key holds
    held = _doc(square(e.anim((0, [256, 256], "hold"), (10, [356, 256]))))
    assert _box(held, 9.9) == (246, 246, 266, 266) and _box(held, 10) == (346, 246, 366, 266)
    linear = _doc(square(e.anim((0, [256, 256]), (10, [356, 256]), ease="linear")))
    assert _box(linear, 2) == (266, 246, 286, 266)
    grow = e.group(
        e.ellipse(256, 256, e.anim((0, [100, 100]), (10, [300, 100]), ease="linear")), e.fill(e.RED)
    )
    assert _box(_doc(grow), 5) == (156, 206, 356, 306)
    path = e.anim((0, e.poly((200, 200), (220, 200))), (10, e.poly((200, 200), (400, 300))), ease="linear")
    assert _box(_doc(e.group(e.shape(path), e.stroke(e.NAVY, 2))), 5) == (199, 199, 311, 251)


def test_a_drawing_is_scaled_and_centred_to_fill_its_square() -> None:
    e = emoji_art
    tiny = e.emoji("tiny", e.group(e.ellipse(100, 120, 50), e.fill(e.RED)))
    side = e.FIT_SIZE
    assert _box(tiny, 0) == (256 - side / 2, 256 - side / 2, 256 + side / 2, 256 + side / 2)
    assert e.look_problems(tiny) == [] and e.problems(tiny, b"x") == []


def test_the_look_check_finds_small_off_centre_shrinking_faint_and_half_drawn_emoji() -> None:
    e = emoji_art
    small = _doc(e.group(e.ellipse(256, 256, 200), e.fill(e.RED)))
    assert e.look_problems(small) == ["small: 200 of 512 px", "frame 0 shrinks to 200 px"]
    off = _doc(e.group(e.ellipse(240, 256, 460), e.fill(e.RED)))
    assert e.look_problems(off) == ["off centre"]
    shrink = e.loop((0, [100, 100]), (5, [100, 100]), (10, [60, 60]))
    shrinking = e.emoji("s", e.group(e.ellipse(256, 256, 300), e.fill(e.RED), t=e.transform(scale=shrink)))
    assert e.look_problems(shrinking) == ["frame 10 shrinks to 276 px"]
    fading = e.emoji("f", e.group(e.ellipse(256, 256, 300), e.fill(e.RED, e.loop((0, 100), (30, 25)))))
    assert e.look_problems(fading) == ["f/fit/g: fill fades to 25%"]
    dimmed = e.transform(opacity=e.loop((0, 100), (30, 70)))
    group_fade = e.emoji("g", e.group(e.ellipse(256, 256, 300), e.stroke(e.RED, 9, 80), t=dimmed))
    assert e.look_problems(group_fade) == ["g/fit/g: stroke fades to 56%"]
    shine = e.emoji("h", e.group(e.ellipse(256, 256, 300), e.fill(e.RED)), e.highlight(220, 200, 90, 50, 30))
    assert e.look_problems(shine) == []
    trimmed = e.group(
        e.shape(e.poly((100, 100), (400, 400), closed=False)), {"ty": "tm"}, e.stroke(e.RED, 30)
    )
    assert e.look_problems(e.emoji("t", trimmed)) == ["t/fit/g: trim path (a half-drawn line)"]


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
    assert own_pack.pack_name("@ipoxxo", "ProxyHub_Bot", 1) == "ipoxxo_by_proxyhub_bot"
    assert own_pack.pack_name("ipoxxo_", "x_bot", 1) == "ipoxxo_by_x_bot"  # no "__"
    assert own_pack.pack_name("ipoxxo_", "x_bot", 2) == "ipoxxo_v2_by_x_bot"
    v = emoji_art.ART_VERSION
    assert own_pack.pack_name("@ipoxxo", "ProxyHub_Bot") == f"ipoxxo_v{v}_by_proxyhub_bot"
    assert own_pack.pack_name("a__b", "x_bot") is None
    assert own_pack.pack_name("1abc", "x_bot") is None  # must start with a letter
    assert own_pack.pack_name("a" * 55, "x_bot", 1) is not None
    assert own_pack.pack_name("a" * 55, "x_bot", 2) is None  # longer than 64
    assert own_pack.pack_name("", "x_bot") is None
    assert own_pack.pack_title("@ipoxxo") == own_pack.pack_title("ipoxxo") == "@ipoxxo"
    # The versions before this one, newest first: the posts may still use one of them.
    assert own_pack.older_names("ipoxxo", "x_bot", 1) == ()
    assert own_pack.older_names("ipoxxo", "x_bot", 3) == ("ipoxxo_v2_by_x_bot", "ipoxxo_by_x_bot")
    assert own_pack.older_names("ipoxxo", "proxyhub_test_bot")[-1] == PACK_V1


def test_the_emoji_ids_count_only_when_they_cover_every_drawing() -> None:
    full = [(str(9_000 + n), a.alt) for n, a in enumerate(emoji_art.ART)]
    state = own_pack.PackState(name=PACK, entries=tuple(full))
    assert own_pack.emoji_ids(state) == tuple((9_000 + n, a.alt) for n, a in enumerate(emoji_art.ART))
    assert own_pack.emoji_ids(own_pack.PackState(name=PACK, entries=tuple(full[:-1]))) is None
    assert own_pack.emoji_ids(own_pack.PackState(name=PACK)) is None


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
            "stickers": [
                {"emoji": "📡", "custom_emoji_id": "5368324170671202286"},
                {"custom_emoji_id": "2"},
                {"emoji": "⚡", "custom_emoji_id": "12/../x"},
                {"emoji": "🔗", "custom_emoji_id": 7},
                "junk",
            ],
        }
    )
    assert info == StickerSetInfo(
        name=PACK,
        title="@ipoxxo",
        is_custom_emoji=True,
        alts=("📡", "⚡", "🔗"),
        entries=(("5368324170671202286", "📡"),),  # only a well-formed id goes into a post
    )
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
    assert await _target(engine) == own_pack.Target(
        name=PACK, title="@ipoxxo", owner_id=OWNER, older=own_pack.older_names("ipoxxo", "proxyhub_test_bot")
    )
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


async def _emoji_map(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        return await operator.emoji_map(conn)


@pytest.mark.db
async def test_new_drawings_are_a_new_pack_built_beside_the_old_one_and_taken_over_once_complete(
    engine: AsyncEngine, vault: Vault
) -> None:
    """The server's state when the art changed: the first pack (version 1, 28 emoji) is complete,
    chosen by the panel, and its emoji ids were read by the operator account."""
    await fleet_kill_switch(engine, on=False)
    await setup_telegram(engine, vault, test=True, main=True, owner=True)
    full = tuple(a.alt for a in emoji_art.ART)
    old_ids = [(1_000 + n, alt) for n, alt in enumerate(full)]
    async with engine.begin() as conn:
        # the stored JSON as version 1 wrote it (no "entries")
        await store.put_setting(
            conn,
            own_pack.KEY,
            {"name": PACK_V1, "title": "@ipoxxo", "owner_id": OWNER, "alts": list(full),
             "created_at": NOW.isoformat(), "checked_at": NOW.isoformat(), "attempted_at": NOW.isoformat(),
             "retry_at": None, "error": None},
        )  # fmt: skip
        await operator.save_config(conn, operator.OperatorConfig(poster="operator", emoji_pack=PACK_V1))
        await operator.save_pack(
            conn,
            operator.StoredPack(short_name=PACK_V1, entries=tuple(old_ids), fetched_at=NOW, attempted_at=NOW),
        )
    old_map = await _emoji_map(engine)
    assert old_map["📡"] == 1_000 and len(old_map) == len(full)

    # The next step goes for the new version's name at once (here Telegram is not reachable).
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "failed"
    state = await _state(engine)
    assert state is not None and state.name == PACK and state.alts == () and state.error
    assert (await _config(engine)).emoji_pack == PACK_V1 and await _emoji_map(engine) == old_map
    async with engine.connect() as conn:
        shown = await own_pack.status(conn)
    assert (shown["name"], shown["replaces"], shown["version"]) == (PACK, PACK_V1, emoji_art.ART_VERSION)

    # Partly built (the bot adds 3 a minute): the posts keep the old pack.
    new_ids = [(str(9_000 + n), alt) for n, alt in enumerate(full)]
    partial = own_pack.PackState(
        name=PACK, title="@ipoxxo", owner_id=OWNER, alts=full[:4], entries=tuple(new_ids[:4]),
        created_at=NOW, checked_at=NOW, attempted_at=NOW,
    )  # fmt: skip
    async with engine.begin() as conn:
        await own_pack.save_state(conn, partial)
    assert own_pack.due(partial, PACK, NOW + timedelta(minutes=1))
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "failed"
    assert (await _config(engine)).emoji_pack == PACK_V1 and await _emoji_map(engine) == old_map
    state = await _state(engine)
    assert state is not None and state.entries == tuple(new_ids[:4])  # kept through the failure

    # Complete, but without the ids (never written so by this code): the old pack stays.
    async with engine.begin() as conn:
        await own_pack.save_state(
            conn, own_pack.PackState(name=PACK, title="@ipoxxo", owner_id=OWNER, alts=full, checked_at=NOW)
        )
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "not_due"
    assert (await _config(engine)).emoji_pack == PACK_V1 and await _emoji_map(engine) == old_map

    # Complete with every id: the posts move to it in one transaction, ids included.
    async with engine.begin() as conn:
        await own_pack.save_state(
            conn,
            own_pack.PackState(
                name=PACK, title="@ipoxxo", owner_id=OWNER, alts=full, entries=tuple(new_ids), checked_at=NOW
            ),
        )
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "not_due"
    assert await _config(engine) == operator.OperatorConfig(poster="operator", emoji_pack=PACK)
    new_map = await _emoji_map(engine)
    assert new_map["📡"] == 9_000 and new_map[emoji_key("⏸️")] == 9_000 + full.index("⏸️")
    assert len(new_map) == len(full)
    async with engine.connect() as conn:
        pack = await operator.load_pack(conn)
        rows = (
            await conn.execute(
                text(
                    "SELECT target, reasons->'context'->>'replaces' FROM actions_log"
                    " WHERE action = 'posts.own_pack_chosen'"
                )
            )
        ).all()
        shown = await own_pack.status(conn)
    assert pack is not None and pack.fetched_at == NOW and pack.attempted_at is None  # read again soon
    assert [(r[0], r[1]) for r in rows] == [(PACK, PACK_V1)]
    assert shown["replaces"] is None and shown["name"] == PACK and shown["emoji"] == len(full)
    # Once moved, nothing changes again; a pack the owner pastes later is never replaced.
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "not_due"
    async with engine.begin() as conn:
        await operator.save_config(conn, operator.OperatorConfig(poster="operator", emoji_pack=EMOJI_SET))
    assert await own_pack.ensure(engine, vault, now=NOW, bot_factory=closed_port_bot()) == "not_due"
    assert (await _config(engine)).emoji_pack == EMOJI_SET


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
