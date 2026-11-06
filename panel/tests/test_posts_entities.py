"""A post's HTML as text + entities, and the animated emoji, on real Persian posts (decision D38).

Offsets and lengths are UTF-16 code units (core.telegram.org/api/entities). Every expectation is
checked against the UTF-16 encoding of the text itself, and the HTML side against Telethon's own
HTML parser (an independent implementation of the same rules) wherever both understand the tags.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from telethon.extensions import html as telethon_html

from proxyhub.posts import entities
from proxyhub.posts.entities import (
    MAX_CUSTOM_EMOJI,
    MessageText,
    TextEntity,
    emoji_in,
    emoji_key,
    emoji_report,
    operator_message,
    pack_map,
    parse_html,
    utf16_len,
    utf16_slice,
    with_custom_emoji,
)
from proxyhub.telegram.templates import (
    NBSP,
    REALITY_MARK,
    TITLE_MARK,
    PostLink,
    StatusTexts,
    compose_content,
    render_post,
)

PACK = pack_map(
    [
        (5_001, "📡"),
        (5_002, "⌛"),  # the pack's alt has no variation selector; posts write "⌛️"
        (5_003, "🔗"),
        (5_004, "🇮🇷"),
        (5_005, "👨‍👩‍👧"),
        (5_006, "1️⃣"),
        (5_007, "🔐"),
        (5_008, "👍🏽"),
        (5_009, "♻️"),
        (5_010, "📡"),  # a second animation of the same emoji: the first one wins
        (5_011, "🔑"),
        (5_012, "⚡"),
        (5_013, "🔒"),
    ]
)


def covered(message: MessageText, entity: TextEntity) -> str:
    return utf16_slice(message.text, entity.offset, entity.length)


def customs(message: MessageText) -> list[tuple[str, int]]:
    return [(covered(message, e), e.document_id or 0) for e in message.entities if e.kind == "custom_emoji"]


# --- UTF-16 ----------------------------------------------------------------------------------------------


def test_utf16_lengths_of_persian_and_emoji() -> None:
    assert utf16_len("سلام") == 4  # Arabic-script letters are one code unit each
    assert utf16_len("پروکسی‌ها") == 9  # the zero-width non-joiner is one unit too
    assert utf16_len("📡") == 2  # above U+FFFF: a surrogate pair
    assert utf16_len("⌛️") == 2  # U+231B + U+FE0F, both in the BMP
    assert utf16_len("🇮🇷") == 4
    assert utf16_len("👨‍👩‍👧") == 8  # three pictographs, two zero-width joiners
    assert utf16_len("۱۲۳") == 3
    for text in ("سلام 📡", "🇮🇷 ایران", "a⌛️b"):
        assert utf16_len(text) == len(text.encode("utf-16-le")) // 2


def test_utf16_slice_reads_back_exactly_one_emoji() -> None:
    text = "۱ 📡 پروکسی ⌛️"
    assert utf16_slice(text, 2, 2) == "📡"
    assert utf16_slice(text, 12, 2) == "⌛️"
    assert utf16_slice(text, 5, 6) == "پروکسی"


# --- HTML -> text + entities -----------------------------------------------------------------------------


def test_a_persian_post_parses_to_the_bot_apis_text_and_entities() -> None:
    m = parse_html(
        "<b>📡 پروکسی تازه</b>\n\n<blockquote>سلام <i>دوستان</i> 🇮🇷</blockquote>\n"
        '<a href="https://t.me/proxy?server=a.b&amp;port=443&amp;secret=x">پروکسی ۱</a> • <s>پروکسی ۲</s> ⌛️'
    )
    assert m.text == "📡 پروکسی تازه\n\nسلام دوستان 🇮🇷\nپروکسی ۱ • پروکسی ۲ ⌛️"
    got = [(e.kind, e.offset, e.length, covered(m, e)) for e in m.entities]
    assert got == [
        ("bold", 0, 14, "📡 پروکسی تازه"),  # 📡 is two units: 2 + 1 + 6 + 1 + 4
        ("blockquote", 16, 16, "سلام دوستان 🇮🇷"),  # 4 + 1 + 6 + 1 + 4
        ("italic", 21, 6, "دوستان"),
        ("text_url", 33, 8, "پروکسی ۱"),  # after the line break
        ("strike", 44, 8, "پروکسی ۲"),  # " • " is three units
    ]
    assert m.entities[3].url == "https://t.me/proxy?server=a.b&port=443&secret=x"  # references decoded


def test_every_tag_of_the_template_and_the_owners_body() -> None:
    m = parse_html(
        "<strong>a</strong><em>b</em><u>c</u><ins>d</ins><strike>e</strike><del>f</del>"
        '<tg-spoiler>g</tg-spoiler><span class="tg-spoiler">h</span><span class="x">i</span>'
        '<code>j</code><pre><code class="language-python">k</code></pre>'
        '<blockquote expandable>l</blockquote><tg-emoji emoji-id="42">⭐</tg-emoji><unknown>m</unknown>'
    )
    assert m.text == "abcdefghijkl⭐m"
    kinds = [(e.kind, covered(m, e)) for e in m.entities]
    assert kinds == [
        ("bold", "a"),
        ("italic", "b"),
        ("underline", "c"),
        ("underline", "d"),
        ("strike", "e"),
        ("strike", "f"),
        ("spoiler", "g"),
        ("spoiler", "h"),
        ("code", "j"),
        ("pre", "k"),
        ("blockquote", "l"),
        ("custom_emoji", "⭐"),
    ]
    pre = next(e for e in m.entities if e.kind == "pre")
    quote = next(e for e in m.entities if e.kind == "blockquote")
    emoji = next(e for e in m.entities if e.kind == "custom_emoji")
    assert (pre.language, quote.collapsed, emoji.document_id) == ("python", True, 42)


def test_unclosed_stray_and_empty_tags() -> None:
    m = parse_html("  <b>بدون بستن</i> <i></i>\n")
    assert m.text == "بدون بستن"  # outer white space trimmed, as Telegram does
    assert [(e.kind, e.offset, e.length) for e in m.entities] == [("bold", 0, 9)]
    assert parse_html('<a href="">x</a><tg-emoji emoji-id="abc">⭐</tg-emoji>').entities == ()


def _post_html() -> str:
    content = compose_content("💡 ترفند امروز 🇮🇷", "سلام <b>دوستان</b> 👨‍👩‍👧 <code>vless 🔑</code> ♻️")
    until = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    links = [
        PostLink(
            kind="mtproto",
            status="active",
            url="https://t.me/proxy?server=203.0.113.10&port=443&secret=" + "a1" * 16,
        ),
        PostLink(kind="mtproto", status="expired"),
        PostLink(kind="mtproto", status="paused"),
        PostLink(
            kind="mtproto",
            status="active",
            url="https://t.me/proxy?server=203.0.113.11&port=443&secret=" + "b2" * 16,
        ),
        PostLink(
            kind="reality",
            status="active",
            url="vless://11111111-2222-3333-4444-555555555555@203.0.113.10:443?security=reality#%40ipoxxo",
            expires_at=until,
            variant="TCP",
        ),
        PostLink(
            kind="reality",
            status="active",
            url="vless://11111111-2222-3333-4444-555555555555@203.0.113.10:2083?type=xhttp#%40ipoxxo%20XHTTP",
            expires_at=until,
            variant="XHTTP",
            companion=True,
        ),
        PostLink(
            kind="reality",
            status="active",
            url="vless://66666666-2222-3333-4444-555555555555@203.0.113.12:443?type=ws#%40ipoxxo",
            expires_at=until,
        ),
        PostLink(kind="reality", status="full"),
    ]
    return render_post(content, links, "@ipoxxo", StatusTexts()).text


def test_the_real_template_matches_telethons_own_html_parser() -> None:
    """Telethon's parser is an independent implementation; it knows every tag our template uses."""
    post = _post_html()
    ours = parse_html(post)
    text, theirs = telethon_html.parse(post)
    assert ours.text == text
    assert [(e.kind, e.offset, e.length, e.url, e.language or None) for e in ours.entities] == [
        (
            _KIND[type(t).__name__],
            t.offset,
            t.length,
            getattr(t, "url", None),
            getattr(t, "language", None) or None,  # Telethon writes "" for a pre without a language
        )
        for t in sorted(theirs, key=lambda t: (t.offset, -t.length))
    ]
    for e in ours.entities:  # every entity covers whole characters
        assert covered(ours, e).encode("utf-16-le").decode("utf-16-le")
    assert [e.collapsed for e in ours.entities if e.kind == "blockquote"] == [
        bool(getattr(t, "collapsed", False))
        for t in sorted(theirs, key=lambda t: (t.offset, -t.length))
        if type(t).__name__ == "MessageEntityBlockquote"
    ]
    assert [e.collapsed for e in ours.entities if e.kind == "blockquote"] == [False, True, True, True]
    # The body's quote, then one collapsed quote per config LINK (TCP, XHTTP, the WS config): each
    # quote covers exactly its one inline-code config and nothing else, so copying it gives one
    # config; no pre anywhere (Telegram nests none in a quote).
    quotes = [e for e in ours.entities if e.kind == "blockquote" and e.collapsed]
    configs = [e for e in ours.entities if e.kind == "code" and covered(ours, e).startswith("vless://")]
    assert len(configs) == 3 and not any(e.kind == "pre" for e in ours.entities)
    assert all("\n" not in covered(ours, e) for e in configs)
    assert [(q.offset, q.length) for q in quotes] == [(c.offset, c.length) for c in configs]
    assert "\nTCP\nvless://" in ours.text and "\n\nXHTTP\nvless://" in ours.text  # labels outside
    # The proxy words are the links' whole text, with no mark (D48).
    words = [covered(ours, e) for e in ours.entities if e.kind == "text_url"]
    assert words == [f"پروکسی{NBSP}۱", f"پروکسی{NBSP}۴"]


_KIND = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityStrike": "strike",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityTextUrl": "text_url",
    "MessageEntityBlockquote": "blockquote",
}


# --- emoji -------------------------------------------------------------------------------------------------


def test_emoji_are_found_whole() -> None:
    text = "۱ 📡 ⌛️⌛ 🇮🇷 👨‍👩‍👧 1️⃣ 👍🏽 ها‍ی — • − @ipoxxo ♻️"
    found = [(e.emoji, e.offset, e.length) for e in emoji_in(text)]
    assert [f[0] for f in found] == ["📡", "⌛️", "⌛", "🇮🇷", "👨‍👩‍👧", "1️⃣", "👍🏽", "♻️"]
    for emoji, offset, length in found:
        assert utf16_slice(text, offset, length) == emoji
    # A Persian zero-width joiner between letters, dashes, bullets, math signs, plain arrows, shapes
    # and digits: no emoji.
    assert not [e for e in emoji_in("ها‍ی — • − ← → ■ ▲ ۱۲ 12 abc") if e.emoji]
    assert [e.emoji for e in emoji_in("↔️ ↩ ▶️ ⬅️ ⭐")] == ["↔️", "↩", "▶️", "⬅️", "⭐"]


def test_variation_selectors_do_not_matter_for_the_pack() -> None:
    assert emoji_key("⌛️") == emoji_key("⌛") == "⌛"
    assert PACK["⌛"] == 5_002 and PACK["📡"] == 5_001  # the first 📡 of the pack


def test_the_owners_emoji_become_custom_emoji_over_exactly_their_units() -> None:
    m = operator_message(_post_html(), PACK)
    assert m.text == parse_html(_post_html()).text  # the text itself never changes
    got = customs(m)
    assert ("💡", 0) not in got  # not in the pack: stays a plain emoji
    assert got[:3] == [("🇮🇷", 5_004), ("👨‍👩‍👧", 5_005), ("♻️", 5_009)]
    assert ("⌛️", 5_002) in got and ("🔗", 5_003) in got  # an ended proxy's mark, the signature
    assert got.count(("🔒", 5_013)) == 2  # the line above each config's quote
    assert all(doc != 5_011 for _, doc in got)  # 🔑 is inside <code>: Telegram draws code plain
    assert "⚡" not in m.text and "🔐" not in m.text  # no mark on a proxy word, no Reality header
    # No custom emoji inside inline code (a config or the body's) or a link (Telegram's nesting rules).
    closed = [(e.offset, e.end) for e in m.entities if e.kind in ("code", "pre", "text_url")]
    assert len(closed) == 6  # the body's code, three configs, two proxy words
    for e in m.entities:
        if e.kind == "custom_emoji":
            assert not any(e.offset < end and start < e.end for start, end in closed)
    for e in m.entities:
        if e.kind == "custom_emoji":
            emoji = covered(m, e)
            assert emoji_key(emoji) in PACK and utf16_len(emoji) == e.length
            assert m.text.encode("utf-16-le")[2 * e.offset : 2 * e.end].decode("utf-16-le") == emoji
    # The other entities are untouched, and every list is in offset order.
    plain = parse_html(_post_html())
    assert [e for e in m.entities if e.kind != "custom_emoji"] == list(plain.entities)
    assert [e.offset for e in m.entities] == sorted(e.offset for e in m.entities)


def test_offsets_after_persian_text_and_surrogate_pairs() -> None:
    m = with_custom_emoji(MessageText("سلام 📡 دوستان 🇮🇷 خوش آمدید ⌛️"), PACK)
    assert [(e.offset, e.length, covered(m, e)) for e in m.entities] == [
        (5, 2, "📡"),  # after 4 letters and a space
        (15, 4, "🇮🇷"),  # 5 + 2 + 1 + 6 + 1
        (30, 2, "⌛️"),  # 15 + 4 + 1 + 9 (خوش آمدید) + 1
    ]


def test_explicit_custom_emoji_and_code_are_left_alone() -> None:
    m = operator_message('<tg-emoji emoji-id="9">📡</tg-emoji> <code>📡</code> <pre>⌛</pre> 📡', PACK)
    assert customs(m) == [("📡", 9), ("📡", 5_001)]


def test_an_emoji_inside_a_link_or_a_config_block_stays_plain() -> None:
    """A link holds no entity but bold, italic, underline, strike and spoiler, and pre or code
    none at all (core.telegram.org/bots/api#formatting-options)."""
    m = operator_message(
        'x ⚡ <a href="https://t.me/proxy?server=a.b&amp;port=1&amp;secret=x">⚡ اتصال <b>📡</b></a> '
        '<pre><code class="language-XHTTP">vless://a@b:1#⚡</code></pre> '
        "<blockquote expandable><code>vless://a@b:1#⚡</code></blockquote> ⚡",
        PACK,
    )
    assert customs(m) == [("⚡", 5_012), ("⚡", 5_012)]  # the first and the last one only
    assert [e.offset for e in m.entities if e.kind == "custom_emoji"] == [2, utf16_len(m.text) - 1]


def test_a_configs_quote_holds_only_its_code_and_a_status_line_gets_its_custom_emoji() -> None:
    """A config sits alone as inline code in its own collapsed quote (the code stays plain); the
    status line of the pair's ended link is outside any quote and gets its animated twin."""
    pair = [
        PostLink(
            kind="reality",
            status="active",
            url="vless://11111111-2222-3333-4444-555555555555@203.0.113.10:443?type=tcp#%40ipoxxo%20⌛",
            expires_at=datetime(2026, 11, 6, 6, 0, tzinfo=UTC),
            variant="TCP",
        ),
        PostLink(kind="reality", status="expired", variant="XHTTP", companion=True),
    ]
    m = operator_message(render_post(None, pair, "@ipoxxo").text, PACK)
    [quote] = [e for e in m.entities if e.kind == "blockquote"]
    [code] = [e for e in m.entities if e.kind == "code"]
    assert quote.collapsed and (quote.offset, quote.end) == (code.offset, code.end)
    got = [(covered(m, e), e.offset) for e in m.entities if e.kind == "custom_emoji"]
    # The "⌛" inside the config stays plain, and so does the 🔒: without content it is the
    # message's first character (see the next test).
    assert [emoji for emoji, _ in got] == ["⌛️", "🔗"] and m.text.startswith("🔒")
    status = m.text.index("XHTTP\n⌛️ زمان این کانفیگ تمام شد") + len("XHTTP\n")
    assert got[0][1] == utf16_len(m.text[:status]) and got[0][1] > quote.end


def test_a_quote_and_its_only_code_are_ordered_outer_first() -> None:
    """A config alone in its quote: both entities cover the same units. The quote comes first, as
    the Bot API and Telethon order them (the outer one opens first)."""
    m = parse_html("🔒 x\n<blockquote expandable><code>vless://a@b:1</code></blockquote>")
    assert [(e.kind, e.offset, e.length) for e in m.entities] == [("blockquote", 5, 13), ("code", 5, 13)]
    nested = parse_html("<b><i><u>x</u></i></b>")
    assert [e.kind for e in nested.entities] == ["bold", "italic", "underline"]


def test_no_pack_or_no_match_leaves_the_message_as_it_is() -> None:
    m = parse_html("<b>سلام</b> 💡")
    assert with_custom_emoji(m, {}) is m
    assert with_custom_emoji(m, PACK) == m


def test_at_most_max_custom_emoji() -> None:
    m = with_custom_emoji(MessageText("📡" * (MAX_CUSTOM_EMOJI + 5)), PACK)
    assert m.custom_emoji_count() == MAX_CUSTOM_EMOJI
    assert m.entities[0].offset == 2  # the message's first emoji stays plain
    assert m.entities[-1].offset == 2 * MAX_CUSTOM_EMOJI


@pytest.mark.parametrize(
    "title", ["🔄 به‌روزرسانی‌ها را عقب نیندازید", "🔄\ufe0f به‌روزرسانی‌ها را عقب نیندازید"]
)
def test_the_messages_first_character_stays_a_plain_emoji(title: str) -> None:
    """iOS draws no custom emoji at the very start of a message: the bold title's 🔄 (in our pack)
    showed nothing on the owner's iPhone, while the same pack's 🔒 and 🔗 at later line starts did.
    The first character keeps its font emoji; every other emoji of the pack still gets its twin."""
    config = PostLink(kind="reality", status="active", url="vless://a@203.0.113.9:443#x")
    post = render_post(compose_content(title, "متن"), [config], "@ipoxxo").text
    pack = {**PACK, emoji_key("🔄"): 5_020}
    plain = parse_html(post)
    m = with_custom_emoji(plain, pack)
    assert m.text.startswith(title) and m.text == plain.text
    first = next(emoji_in(m.text))
    assert (first.offset, first.length) == (0, 3 if "\ufe0f" in title else 2)  # what the entity would be
    assert not any(e.kind == "custom_emoji" and e.offset == 0 for e in m.entities)
    assert [(e.kind, e.offset) for e in m.entities][:1] == [("bold", 0)]  # the bold line is untouched
    assert [covered(m, e) for e in m.entities if e.kind == "custom_emoji"] == ["🔒", "🔗"]
    assert dict(emoji_report(post, pack))["🔒"] is True
    assert dict(emoji_report(post, pack))[first.emoji] is False  # the dashboard says it stays plain
    # Not only the title: whatever stands first in a message stays plain; a later twin still counts.
    again = with_custom_emoji(MessageText("🔄 x 🔄"), pack)
    assert [(e.offset, e.document_id) for e in again.entities] == [(5, 5_020)]


def test_the_bot_path_keeps_plain_emoji() -> None:
    """The bot sends the HTML itself; nothing in it changes for the operator's emoji."""
    post = _post_html()
    assert "tg-emoji" not in post
    assert operator_message(post, PACK).text == parse_html(post).text


def test_the_preview_report() -> None:
    report = dict(emoji_report(_post_html(), PACK))
    assert report["🔒"] is True and report["⌛️"] is True and report["🔗"] is True
    assert report["💡"] is False  # not in the pack
    assert report["🔑"] is False  # only inside code
    assert "🔐" not in report and "⚡" not in report and "📡" not in report  # gone with D48
    assert emoji_report(_post_html(), {}) == [(e, False) for e, _ in emoji_report(_post_html(), PACK)]


# --- properties --------------------------------------------------------------------------------------------

PIECES = st.sampled_from(
    [
        "سلام",
        "پروکسی‌ها",
        " ",
        "\n",
        "۱۲",
        "abc",
        "📡",
        "⌛️",
        "⌛",
        "🇮🇷",
        "👨‍👩‍👧",
        "1️⃣",
        "👍🏽",
        "🔐",
        "💡",
        "—",
        "&",
        "<",
    ]
)
TAGS = st.sampled_from(["b", "i", "u", "s", "code", "tg-spoiler", "blockquote"])


@settings(max_examples=300, deadline=None)
@given(st.lists(st.tuples(PIECES, st.one_of(st.none(), TAGS)), min_size=1, max_size=25))
def test_any_mix_of_persian_emoji_and_tags(parts: list[tuple[str, str | None]]) -> None:
    source = "".join(
        f"<{tag}>{html.escape(piece)}</{tag}>" if tag else html.escape(piece) for piece, tag in parts
    )
    plain = parse_html(source)
    message = with_custom_emoji(plain, PACK)
    assert message.text == plain.text == "".join(piece for piece, _ in parts).strip()
    units = utf16_len(message.text)
    code = [(e.offset, e.end) for e in message.entities if e.kind == "code"]
    for e in message.entities:
        assert 0 <= e.offset < e.end <= units
        piece = covered(message, e)
        assert "\ud800" > piece[:1] or piece[:1] > "\udfff"  # never starts inside a surrogate pair
        if e.kind == "custom_emoji":
            assert emoji_key(piece) in PACK
            assert not any(e.offset < end and start < e.end for start, end in code)
    # The same text through Telethon's own parser (it knows all these tags but tg-spoiler).
    if "tg-spoiler" not in source:
        text, _ = telethon_html.parse(source)
        assert text == message.text


@pytest.mark.parametrize("emoji", ["📡", "⌛️", "🇮🇷", "👨‍👩‍👧", "1️⃣", "👍🏽"])
def test_each_emoji_kind_after_mixed_text(emoji: str) -> None:
    prefix = "پروکسی ۱ • 🔗 ab "
    m = with_custom_emoji(MessageText(prefix + emoji), {emoji_key(emoji): 77})
    [e] = m.entities
    assert (e.offset, e.length) == (utf16_len(prefix), utf16_len(emoji))
    assert covered(m, e) == emoji


def test_the_compact_layouts_emoji_sit_on_exact_utf16_offsets() -> None:
    """D48: the title line's emoji (the panel's own when a title has none; it stays a plain emoji as
    the message's first character), the lock above a config and the signature; each custom emoji
    covers exactly its two units after Persian text, no-break spaces and bullets."""
    six = [
        PostLink(
            kind="mtproto",
            status="active",
            url=f"https://t.me/proxy?server=203.0.113.{i}&port=443&secret=" + "c3" * 16,
        )
        for i in range(1, 7)
    ]
    config = PostLink(kind="reality", status="active", url="vless://a@203.0.113.9:443#x", expires_at=None)
    post = render_post(compose_content("عنوان خبر", "متن خبر"), [*six, config], "@ipoxxo").text
    m = operator_message(post, PACK)
    title = f"{TITLE_MARK} عنوان خبر"
    grid = "\n".join(f"پروکسی{NBSP}{a} • پروکسی{NBSP}{b}" for a, b in (("۱", "۲"), ("۳", "۴"), ("۵", "۶")))
    lock = f"{REALITY_MARK} کانفیگ V2Ray"
    tag = "🔗 \u200e@ipoxxo"
    assert m.text == f"{title}\n\nمتن خبر\n\n{grid}\n\n{lock}\nvless://a@203.0.113.9:443#x\n\n{tag}"
    got = [(covered(m, e), e.offset, e.length, e.document_id) for e in m.entities if e.kind == "custom_emoji"]
    assert m.text.startswith(TITLE_MARK)  # a plain emoji: no entity at offset 0 (iOS)
    assert got == [
        ("🔒", utf16_len(f"{title}\n\nمتن خبر\n\n{grid}\n\n"), 2, 5_013),
        ("🔗", utf16_len(m.text) - utf16_len(tag), 2, 5_003),
    ]
    links = [(covered(m, e), e.offset) for e in m.entities if e.kind == "text_url"]
    assert [w for w, _ in links] == [f"پروکسی{NBSP}{d}" for d in "۱۲۳۴۵۶"]
    assert links[1][1] - links[0][1] == utf16_len(f"پروکسی{NBSP}۱ • ")  # the bullet and its spaces


def test_module_has_no_telethon_import() -> None:
    assert "telethon" not in entities.__dict__
