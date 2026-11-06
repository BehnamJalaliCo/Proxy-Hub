"""Pure parts of the posts team: post layout, link checks, the day plan, settings and content checks."""

from __future__ import annotations

import html
import itertools
import re
import secrets
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from proxyhub.links.contract import ProxyLink
from proxyhub.posts import content, links, operators, plan
from proxyhub.posts import settings as posting
from proxyhub.posts.content import ContentInvalid
from proxyhub.posts.entities import parse_html
from proxyhub.posts.messages import FALLBACK_TITLE_FA
from proxyhub.posts.publisher import (
    MISSING_GRACE,
    _Carried,
    current_view,
    fit,
    views_for_new_post,
    with_marks,
)
from proxyhub.telegram.schedule import TEHRAN, QuietWindow
from proxyhub.telegram.templates import (
    ALL_DEAD_LINE,
    BODY_TAGS,
    MAX_MESSAGE_CHARS,
    NBSP,
    PAUSED_LINE,
    TITLE_MARK,
    WORD_SEPARATOR,
    PostLink,
    PostTooLong,
    StatusTexts,
    compose_content,
    config_quote,
    fa_digits,
    fits,
    mtproto_url,
    render_post,
    utf16_len,
    visible_text,
    vless_url,
)


def secret() -> str:
    return secrets.token_hex(16)


def mt(i: int, status: str = "active", host: str | None = None, sec: str | None = None) -> PostLink:
    url = mtproto_url(
        f"https://t.me/proxy?server={host or f'slot{i}.example.com'}&port=8443&secret={sec or secret()}"
    )
    return PostLink(kind="mtproto", status=status, url=url)  # type: ignore[arg-type]


def rl(
    i: int,
    status: str = "active",
    expires: datetime | None = None,
    variant: str | None = None,
    companion: bool = False,
) -> PostLink:
    url = vless_url(f"vless://{secrets.token_hex(4)}-aaaa-bbbb-cccc-{secrets.token_hex(6)}@slot{i}.example.com:443"
                    f"?security=reality&sni=www.example.com&fp=chrome#%40ipoxxo%20{i}")  # fmt: skip
    shown: Any = status
    return PostLink(
        kind="reality", status=shown, url=url, expires_at=expires, variant=variant, companion=companion
    )


def word(n: int) -> str:
    """The visible proxy word of the n-th MTProto link (no-break space before the number)."""
    return f"پروکسی{NBSP}{fa_digits(n)}"


def code(link: PostLink) -> str:
    return f"<code>{html.escape(link.url or '', quote=False)}</code>"


def quoted(link: PostLink) -> str:
    """A live config as a post shows it: its own collapsed quote with nothing but its code."""
    return f"<blockquote expandable>{code(link)}</blockquote>"


def one_config_per_quote(post: str) -> bool:
    """Every collapsed quote holds exactly one config as inline code and nothing else (owner,
    iPhone + V2Box: copying a quote must give exactly one config)."""
    quotes = re.findall(r"<blockquote expandable>(.*?)</blockquote>", post, flags=re.DOTALL)
    return all(re.fullmatch(r"<code>vless://[^<\s]+</code>", q) for q in quotes)


CONTENT = compose_content("💡 عنوان <آزمایشی>", "متن <b>پررنگ</b> و <i>کج</i>\nخط دوم")
EXP = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)  # Thursday 18:30 Tehran
LINE = "🔒 کانفیگ V2Ray — اعتبار تا پنجشنبه ساعت ۱۸:۳۰"
QUOTE = "<blockquote expandable>"


# --- layout (decision D48) -----------------------------------------------------------------------


def test_post_is_title_body_proxy_grid_config_and_tag_in_that_order() -> None:
    sec = secret()
    tcp, xhttp = rl(1, expires=EXP, variant="TCP"), rl(1, expires=EXP, variant="XHTTP", companion=True)
    post = render_post(CONTENT, [mt(1, sec=sec), mt(2), mt(3), tcp, xhttp], "@ipoxxo").text
    assert post.startswith("<b>💡 عنوان &lt;آزمایشی&gt;</b>\n\n<blockquote>متن <b>پررنگ</b>")
    # Right under the body: the proxy words, two to a row with a bullet between them; no header,
    # no mark, the whole word is the link.
    assert (
        '</blockquote>\n\n<a href="https://t.me/proxy?server=slot1.example.com'
        f'&amp;port=8443&amp;secret={sec}">{word(1)}</a>{WORD_SEPARATOR}<a href="' in post
    )
    assert f">{word(2)}</a>\n<a href=" in post and f">{word(3)}</a>\n\n🔒 " in post
    # The config: ONE line with its end time, then each link under a tiny label line in its OWN
    # collapsed quote that holds nothing but the config as inline code (one tap, one config).
    assert (f"\n\n{LINE}\nTCP\n{quoted(tcp)}\n\nXHTTP\n{quoted(xhttp)}\n\n—\n🔗 ‎@ipoxxo") in post
    assert one_config_per_quote(post)
    assert post.endswith("</blockquote>\n\n—\n🔗 ‎@ipoxxo")
    assert "#%40ipoxxo%201" in post  # the config's own name (D41) stays inside it
    # No section headers, no ⚡, no <pre> (Telegram nests no pre in a quote), no «کانفیگ ۱».
    for gone in ("<pre", "⚡", "📡", "🔐", "⏳", "پروکسی‌ها", "کانفیگ ۱", "اتصال"):
        assert gone not in post, gone
    assert post.count("<blockquote") == 3  # the body's, then one per config link
    assert visible_text(post).count("https://") == 0
    assert ALL_DEAD_LINE not in post
    assert fits(post)
    assert "·" not in post  # a middle dot next to Persian digits reads as a zero


def test_proxy_words_are_numbered_by_place_two_to_a_row() -> None:
    post = render_post(None, [mt(i) for i in range(1, 8)], "@ipoxxo").text
    rows = post.split("\n\n")[0].split("\n")
    assert [r.count("<a href=") for r in rows] == [2, 2, 2, 1]
    assert [r.count(WORD_SEPARATOR) for r in rows] == [1, 1, 1, 0]
    shown = re.findall(r"<a href=\"[^\"]+\">([^<]+)</a>", post)
    assert shown == [word(n) for n in range(1, 8)]  # ۱ to ۷, whatever slots the links come from
    assert word(7) == "پروکسی ۷"  # a no-break space: a word never wraps
    assert visible_text(rows[0]) == f"{word(1)} • {word(2)}"


def test_rendering_is_deterministic() -> None:
    items = [mt(1), mt(2), rl(1, expires=datetime(2026, 11, 5, tzinfo=UTC))]
    assert render_post(CONTENT, items, "@ipoxxo") == render_post(CONTENT, items, "@ipoxxo")


def test_dead_links_keep_their_place_lose_their_link_and_get_marks() -> None:
    sec = secret()
    live = mt(2, sec=sec)
    items = [
        PostLink(kind="mtproto", status="burned"),
        live,
        PostLink(kind="mtproto", status="expired"),
        PostLink(kind="mtproto", status="full"),
        PostLink(kind="reality", status="expired"),
        PostLink(kind="reality", status="full"),
        PostLink(kind="reality", status="burned"),
    ]
    post = render_post(CONTENT, items, "@ipoxxo").text
    assert f"🚫{NBSP}<s>{word(1)}</s>{WORD_SEPARATOR}<a href=" in post  # number 1 stays number 1
    assert f">{word(2)}</a>\n⌛️{NBSP}<s>{word(3)}</s>{WORD_SEPARATOR}🔴{NBSP}<s>{word(4)}</s>\n\n" in post
    assert post.count("<a href=") == 1 and sec in post
    # A config with nothing left is ONE status line in its place: no lock line, no quote.
    assert (
        f"<s>{word(4)}</s>\n\n⌛️ زمان این کانفیگ تمام شد\n\n"
        "🔴 ظرفیت این کانفیگ پر شد\n\n🚫 این کانفیگ از دسترس خارج شد\n\n—\n🔗" in post
    )
    assert "🔒" not in post and QUOTE not in post and "<code" not in post and "vless://" not in post
    assert ALL_DEAD_LINE not in post  # one link still works


def test_a_config_and_its_xhttp_link_share_one_line_each_in_its_own_quote() -> None:
    later = EXP + timedelta(hours=2)
    tcp, xhttp = rl(1, expires=EXP, variant="TCP"), rl(1, expires=EXP, variant="XHTTP", companion=True)
    alone = rl(2, expires=later)
    post = render_post(None, [tcp, xhttp, alone], "@ipoxxo").text
    assert post == (
        f"{LINE}\nTCP\n{quoted(tcp)}\n\nXHTTP\n{quoted(xhttp)}\n\n"
        f"🔒 کانفیگ V2Ray — اعتبار تا پنجشنبه ساعت ۲۰:۳۰\n{quoted(alone)}\n\n—\n🔗 ‎@ipoxxo"
    )  # a config without its XHTTP link: no label (an older post may carry several configs)
    assert one_config_per_quote(post) and post.count(QUOTE) == 3
    # Half of the pair is not live: its status line takes its place under its label, OUTSIDE any
    # quote, and the line shows the end of the link that still works.
    paused_xhttp = PostLink(kind="reality", status="paused", variant="XHTTP", companion=True)
    half = render_post(None, [tcp, paused_xhttp], "@ipoxxo").text
    assert half == f"{LINE}\nTCP\n{quoted(tcp)}\n\nXHTTP\n{PAUSED_LINE}\n\n—\n🔗 ‎@ipoxxo"
    full_tcp = PostLink(kind="reality", status="full", variant="TCP")
    other = render_post(
        None, [full_tcp, rl(1, expires=later, variant="XHTTP", companion=True)], "@ipoxxo"
    ).text
    assert f"۲۰:۳۰\nTCP\n🔴 ظرفیت این کانفیگ پر شد\n\nXHTTP\n{QUOTE}<code>" in other
    assert one_config_per_quote(other) and other.count(QUOTE) == 1
    # Both end: ONE status line for the pair, no quote; the pause line only when both are paused.
    for statuses, line in [
        (("full", "full"), "🔴 ظرفیت این کانفیگ پر شد"),
        (("paused", "expired"), "⌛️ زمان این کانفیگ تمام شد"),
        (("paused", "paused"), PAUSED_LINE),
    ]:
        pair = [
            PostLink(kind="reality", status=statuses[0], variant="TCP"),  # type: ignore[arg-type]
            PostLink(kind="reality", status=statuses[1], variant="XHTTP", companion=True),  # type: ignore[arg-type]
        ]
        ended = render_post(None, [*pair, alone], "@ipoxxo").text
        later_line = "🔒 کانفیگ V2Ray — اعتبار تا پنجشنبه ساعت ۲۰:۳۰"
        assert ended.startswith(f"{line}\n\n{later_line}\n{QUOTE}"), statuses
        assert ended.count(QUOTE) == 1 and " — TCP" not in ended and "XHTTP" not in ended
    # Two ends in one pair (never expected): the line names the earlier one.
    apart = render_post(None, [tcp, rl(1, expires=later, variant="XHTTP", companion=True)], "@ipoxxo").text
    assert apart.count("🔒") == 1 and apart.startswith(LINE + "\n")


def test_all_dead_gets_the_closing_line_and_owner_texts() -> None:
    texts = StatusTexts(expired="⌛️ تمام شد <b>", full="🔴 پر", burned="🚫 رفت")
    items = [PostLink(kind="mtproto", status="burned"),
             PostLink(kind="reality", status="expired", variant="XHTTP")]  # fmt: skip
    post = render_post(CONTENT, items, "@ipoxxo", texts).text
    assert ALL_DEAD_LINE in post
    assert f"⌛️ تمام شد &lt;b&gt;\n\n{ALL_DEAD_LINE}" in post  # owner text is escaped
    assert post.index(ALL_DEAD_LINE) < post.index("🔗 ‎@ipoxxo")


def test_expiry_names_the_tehran_weekday() -> None:
    # 23:00 UTC on Thursday is 02:30 on Friday in Tehran: the weekday follows Tehran, not UTC.
    late = render_post(None, [rl(1, expires=datetime(2026, 11, 5, 23, 0, tzinfo=UTC))], "@ipoxxo").text
    assert late.startswith("🔒 کانفیگ V2Ray — اعتبار تا جمعه ساعت ۰۲:۳۰\n")
    days = {
        render_post(None, [rl(1, expires=datetime(2026, 11, d, 9, 0, tzinfo=UTC))], "@ipoxxo")
        .text.split("اعتبار تا ", 1)[1]
        .split(" ساعت", 1)[0]
        for d in range(2, 9)
    }
    assert days == {"دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"}
    # A config that never ends: the line without an end time.
    assert render_post(None, [rl(1)], "@ipoxxo").text.startswith(f"🔒 کانفیگ V2Ray\n{QUOTE}<code>")


def test_paused_links_keep_their_place_without_a_link_and_are_not_dead() -> None:
    sec = secret()
    items = [
        PostLink(kind="mtproto", status="paused", url=mt(1, sec=sec).url),
        PostLink(kind="reality", status="paused", url=rl(1).url, variant="TCP"),
    ]
    post = render_post(None, items, "@ipoxxo").text
    # One proxy word in the post: no number (D50).
    assert post == f"⏸{NBSP}پروکسی\n\n{PAUSED_LINE}\n\n—\n🔗 ‎@ipoxxo"
    assert sec not in post and "vless://" not in post
    assert ALL_DEAD_LINE not in post  # paused is not dead: it may come back
    dead_and_paused = [PostLink(kind="mtproto", status="burned"), items[0]]
    assert ALL_DEAD_LINE not in render_post(None, dead_and_paused, "@ipoxxo").text


def test_the_title_line_has_one_emoji() -> None:
    assert compose_content("عنوان بی ایموجی", None) == f"<b>{TITLE_MARK} عنوان بی ایموجی</b>"
    assert compose_content("💡 ترفند امروز", None) == "<b>💡 ترفند امروز</b>"
    assert compose_content("خبر مهم 🔥", None) == "<b>خبر مهم 🔥</b>"  # its own emoji, wherever it is
    assert compose_content(FALLBACK_TITLE_FA, None) == f"<b>{FALLBACK_TITLE_FA}</b>"
    assert compose_content("  a <b>  ", None) == f"<b>{TITLE_MARK} a &lt;b&gt;</b>"
    assert compose_content("۱۲ نکته", "متن").startswith(f"<b>{TITLE_MARK} ۱۲ نکته</b>")  # digits: no emoji


def test_body_tags_have_no_custom_emoji_for_the_bot() -> None:
    assert "tg-emoji" not in BODY_TAGS
    out = compose_content("t", 'a <tg-emoji emoji-id="5368324170671202286">👍</tg-emoji> b')
    assert "tg-emoji" not in out and "👍" in out


def test_active_link_without_url_is_never_a_link() -> None:
    post = render_post(None, [PostLink(kind="mtproto", status="active")], "@ipoxxo").text
    assert "<a " not in post and f"🚫{NBSP}<s>پروکسی</s>" in post
    config = render_post(None, [PostLink(kind="reality", status="active")], "@ipoxxo").text
    assert "<code" not in config and QUOTE not in config and "🚫 این کانفیگ از دسترس خارج شد" in config


def test_a_dead_link_never_shows_its_url_even_if_it_still_has_one() -> None:
    sec = secret()
    dead = [
        PostLink(kind="mtproto", status="burned", url=mt(1, sec=sec).url),
        PostLink(kind="reality", status="expired", url=rl(1).url),
        PostLink(kind="reality", status="paused", url=rl(2).url, variant="TCP"),
        PostLink(kind="reality", status="full", url=rl(2).url, variant="XHTTP", companion=True),
    ]
    post = render_post(None, dead, "@ipoxxo").text
    assert "<a " not in post and "<code" not in post and sec not in post and "vless://" not in post


def test_a_marker_is_only_ever_a_plain_word() -> None:
    for bad in ('"><b>x</b>', "a b", "x" * 17, "", None):
        live = render_post(None, [rl(1, variant=bad)], "@ipoxxo").text
        assert live.startswith(f"🔒 کانفیگ V2Ray\n{QUOTE}<code>vless://") and "<b>x" not in live
        ended = render_post(None, [PostLink(kind="reality", status="full", variant=bad)], "@ipoxxo").text
        assert ended.split("\n")[0] == "🔴 ظرفیت این کانفیگ پر شد"
        half = render_post(None, [rl(1, variant="TCP"), PostLink(kind="reality", status="full", variant=bad,
                                                                companion=True)], "@ipoxxo").text  # fmt: skip
        assert "</code></blockquote>\n\n🔴 ظرفیت این کانفیگ پر شد\n\n—\n🔗" in half
    assert (
        config_quote("vless://a@b:1?x=1&y=<2>", "XHTTP")
        == "XHTTP\n<blockquote expandable><code>vless://a@b:1?x=1&amp;y=&lt;2&gt;</code></blockquote>"
    )
    assert (
        config_quote("vless://a@b:1", '"><b>')
        == "<blockquote expandable><code>vless://a@b:1</code></blockquote>"
    )


def test_content_is_shortened_but_links_and_tag_never() -> None:
    long_body = "کلمه " * 2000
    many = [mt(i) for i in range(1, 13)] + [rl(i) for i in range(1, 4)]
    post = render_post(compose_content("عنوان", long_body[:2900]), many, "@ipoxxo")
    assert fits(post.text)
    assert post.text.count("<a href=") == 12 and post.text.count("<code>") == 3
    assert post.text.count(QUOTE) == 3
    assert post.text.endswith("🔗 ‎@ipoxxo")
    assert post.content_html.endswith("…</blockquote>")
    assert len(post.text) <= MAX_MESSAGE_CHARS and utf16_len(visible_text(post.text)) <= MAX_MESSAGE_CHARS


def test_inline_code_counts_toward_both_limits() -> None:
    def config(n: int, **over: Any) -> PostLink:
        url = vless_url(f"vless://{'a' * n}@b.example.com:443")
        return PostLink(kind="reality", status="active", url=url, **over)

    pair = [config(1900, variant="TCP"), config(1900, variant="XHTTP", companion=True)]
    tail = render_post(None, pair, "@ipoxxo").text
    assert fits(tail) and utf16_len(visible_text(tail)) > 3800  # the configs are visible text
    with pytest.raises(PostTooLong):
        render_post(None, [*pair, config(300)], "@ipoxxo")
    # "&" is one character to read but five in the HTML: the HTML limit counts the escaped config.
    amps_url = vless_url("vless://a@b.example.com:443?" + "&x" * 900)
    amps = PostLink(kind="reality", status="active", url=amps_url)
    with pytest.raises(PostTooLong):
        render_post(None, [amps], "@ipoxxo")
    # With content, the content (and only the content) gives way to the configs.
    post = render_post(compose_content("عنوان", "کلمه " * 300), pair, "@ipoxxo")
    assert fits(post.text) and post.text.endswith(tail) and "…" in post.content_html


def test_the_link_blocks_use_few_entities() -> None:
    """Telegram keeps about 100 entities of a message; the most links a post may carry (20 MTProto,
    5 configs with their XHTTP links), in every state, stay far below that."""
    url = "vless://11111111-2222-3333-4444-555555555555@slot1.example.com:443?type=tcp#x"
    states = ["active", "paused", "expired", "full", "burned"]
    items = [mt(i, status=states[i % 5]) for i in range(20)] + [
        PostLink(
            kind="reality",
            status=states[i % 5],  # type: ignore[arg-type]
            url=url,
            expires_at=EXP,
            variant=v,
            companion=v == "XHTTP",
        )
        for i in range(5)
        for v in ("TCP", "XHTTP")
    ]
    tail = render_post(None, items, "@ipoxxo").text
    assert len(parse_html(tail).entities) <= 40
    assert fits(tail) and one_config_per_quote(tail)


def test_too_many_links_raise_and_fit_drops_from_the_end() -> None:
    huge = [rl(i) for i in range(30)]
    with pytest.raises(PostTooLong):
        render_post(None, [*huge, *huge], "@ipoxxo")
    proxies = [
        ProxyLink(
            kind="reality",
            key=f"reality:{i}",
            slot=i,
            label=f"ریلیتی {i}",
            url=x.url or "",
            expires_at=None,
            status="active",
        )
        for i, x in enumerate(huge * 2)
    ]
    rendered, kept = fit(CONTENT, proxies, "@ipoxxo", StatusTexts())
    assert 0 < len(kept) < len(proxies) and kept == proxies[: len(kept)]
    assert fits(rendered.text)


def test_body_keeps_only_body_tags_and_never_nests_blockquotes() -> None:
    html_in = (
        '<blockquote>نقل</blockquote><pre>کد</pre><script>x</script><a href="javascript:alert(1)">بد</a>'
    )
    out = compose_content("t", html_in)
    assert out.count("<blockquote>") == 1 and "<pre>" not in out and "<script>" not in out
    assert "javascript" not in out and "نقل" in out and "کد" in out and "بد" in out


@given(st.text(max_size=400), st.text(max_size=3000))
@settings(max_examples=150, deadline=None)
def test_any_content_renders_inside_the_limits(title: str, body: str) -> None:
    post = render_post(compose_content(title, body) if title.strip() or body.strip() else None,
                       [mt(1), rl(1)], "@ipoxxo").text  # fmt: skip
    assert fits(post) and post.endswith("🔗 ‎@ipoxxo")


# --- operator names (decision D50) -----------------------------------------------------------------


def named(i: int, operator: str | None, status: str = "active") -> PostLink:
    return replace(mt(i, status=status), operator=operator)


def grid(post: str) -> list[str]:
    """The visible rows of a post's proxy grid (the first block of a post without content)."""
    return visible_text(post.split("\n\n")[0]).split("\n")


def test_operator_names_come_first_and_a_repeated_word_is_numbered_by_its_place() -> None:
    links = [
        named(1, None),
        named(2, "mci"),
        named(3, None),
        named(4, "rightel"),
        named(5, "mci"),
        named(6, None),
    ]
    post = render_post(None, links, "@ipoxxo").text
    assert grid(post) == [
        f"همراه{NBSP}اول{NBSP}۱ • همراه{NBSP}اول{NBSP}۲",
        f"رایتل • پروکسی{NBSP}۱",  # a word that shows once has no number
        f"پروکسی{NBSP}۲ • پروکسی{NBSP}۳",
    ]
    # Within a word the posted order stays: the first «همراه اول» is link 2, the first «پروکسی» link 1.
    hrefs = re.findall(r'<a href="([^"]+)">', post)
    order = [links[i].url for i in (1, 4, 3, 0, 2, 5)]
    assert hrefs == [html.escape(u or "", quote=True) for u in order]
    assert not any(row.startswith("•") for row in grid(post))  # no leading bullet (see the report)
    assert render_post(None, links, "@ipoxxo") == render_post(None, links, "@ipoxxo")


def test_a_named_link_keeps_its_name_when_it_ends_or_pauses() -> None:
    links = [named(1, "mci", "burned"), named(2, "mci"), named(3, "rightel", "paused"), named(4, None)]
    post = render_post(None, links, "@ipoxxo").text
    assert f"🚫{NBSP}<s>همراه{NBSP}اول{NBSP}۱</s> • <a href=" in post
    assert f">همراه{NBSP}اول{NBSP}۲</a>\n⏸{NBSP}رایتل • <a href=" in post
    assert post.count("<a href=") == 2  # a dead or paused word has no link, named or not


def test_posts_without_names_keep_their_words_and_get_the_sign_off() -> None:
    """A post made before D50 has no stored names: the same «پروکسی N» words as before."""
    post = render_post(None, [mt(i) for i in range(1, 5)], "@ipoxxo").text
    assert grid(post) == [f"پروکسی{NBSP}۱ • پروکسی{NBSP}۲", f"پروکسی{NBSP}۳ • پروکسی{NBSP}۴"]
    assert post.endswith("\n\n—\n🔗 \u200e@ipoxxo")
    unknown = render_post(None, [named(1, "tci"), named(2, None)], "@ipoxxo").text
    assert grid(unknown) == [f"پروکسی{NBSP}۱ • پروکسی{NBSP}۲"]  # an unknown code is no name
    assert "همراه" not in unknown


def tls_round(cells: dict[str, dict[str, str]]) -> dict[str, Any]:
    """A probe round's observations as proxyhub/probes stores them (port -> network -> cell)."""
    return {"https": {"state": "reachable", "cells": cells}, "globalping": {"state": "invalid"}}


MCI, RIGHTEL, IRANCELL, DC = "asn:197207", "asn:57218", "asn:44244", "asn:205585"


@pytest.mark.parametrize(
    ("rounds", "seen"),
    [
        ([tls_round({"8443": {MCI: "up", RIGHTEL: "down"}})], ["mci"]),
        ([tls_round({"8443": {MCI: "down", RIGHTEL: "up"}})], ["rightel"]),
        ([tls_round({"8443": {MCI: "up", RIGHTEL: "up"}})], ["mci", "rightel"]),
        ([tls_round({"8443": {MCI: "up", RIGHTEL: "invalid"}})], ["mci"]),
        ([tls_round({"8443": {MCI: "down"}}), tls_round({"8443": {MCI: "up"}})], []),  # fresh down wins
        ([tls_round({"8443": {MCI: "invalid"}}), tls_round({"8443": {MCI: "up"}})], ["mci"]),  # older
        ([tls_round({"443": {MCI: "up"}, "8443": {MCI: "down"}})], []),  # the MTProto port decides
        # P21: the probes measure 443 only; the node's 443 result stands in for its MTProto port.
        ([tls_round({"443": {MCI: "up", RIGHTEL: "up"}})], ["mci", "rightel"]),
        (
            [tls_round({"443": {MCI: "invalid", RIGHTEL: "up"}}), tls_round({"443": {MCI: "up"}})],
            ["mci", "rightel"],
        ),
        ([tls_round({"443": {MCI: "down"}}), tls_round({"443": {MCI: "up"}})], []),  # fresh down wins
        ([tls_round({"8443": {DC: "up"}})], []),  # a data-centre probe never names a link
        ([tls_round({"8443": {IRANCELL: "up", MCI: "down"}})], []),  # Irancell is never named (owner)
        ([], []),
        ([{"https": "junk"}, {"https": {"cells": {"8443": ["up"]}}}, None], []),
    ],
)
def test_a_link_counts_as_open_only_from_the_operators_seen_open_on_its_port(
    rounds: list[Any], seen: list[str]
) -> None:
    assert operators.open_from(rounds, 8443) == seen


def test_each_operator_names_at_most_two_links_and_only_links_seen_open_from_it() -> None:
    """Owner, 2026-11-06: up to two per operator; Irancell's links stay «پروکسی» (no Irancell name)."""
    order = [f"k{i}" for i in range(1, 7)]
    everywhere = {k: ["mci", "rightel"] for k in order}
    assert operators.assign(everywhere, order) == {"k1": "mci", "k2": "mci", "k3": "rightel", "k4": "rightel"}
    assert "irancell" not in operators.OPERATOR_ASNS
    # A link is never named after an operator it was not seen open from.
    mixed = {"k1": ["rightel"], "k2": [], "k3": ["mci"], "k4": ["mci", "rightel"]}
    assert operators.assign(mixed, list(mixed)) == {"k1": "rightel", "k3": "mci", "k4": "mci"}
    assert operators.assign({}, []) == {}


# --- transport markers (proxyhub/posts/links.py) -------------------------------------------------


def test_transport_marks_come_from_the_keys_alone() -> None:
    keys = [
        ("mtproto", "mtproto:n1:u"),
        ("reality", "reality:12"),
        ("reality", "reality:12:xhttp"),
        ("reality", "reality:13"),
        ("reality", "reality:14:xhttp"),  # its config is not in this post
    ]
    assert links.transport_marks(keys) == [
        (None, False),
        ("TCP", False),
        ("XHTTP", True),
        (None, False),
        ("XHTTP", False),
    ]
    assert links.transport_marks([]) == []
    # The views of a new post and of an edit get the same markers: both come from the keys.
    chosen = [
        ProxyLink("reality", "reality:12", 1, "کانفیگ ۱۲ (TCP)", rl(1).url or "", EXP, "active"),
        ProxyLink(
            "reality",
            "reality:12:xhttp",
            1,
            "کانفیگ ۱۲ (XHTTP)",
            rl(1).url or "",
            EXP,
            "active",
            companion_of="reality:12",
        ),
    ]
    views = views_for_new_post(chosen)
    assert [(v.variant, v.companion) for v in views] == [("TCP", False), ("XHTTP", True)]
    ended = with_marks([PostLink(kind="reality", status="full")] * 2, [(x.kind, x.key) for x in chosen])
    assert [(v.variant, v.companion) for v in ended] == [("TCP", False), ("XHTTP", True)]


# --- link checks ---------------------------------------------------------------------------------


def test_mtproto_url_is_rebuilt_canonically() -> None:
    sec = secret()
    assert mtproto_url(f"https://t.me/proxy?secret={sec.upper()}&port=443&server=SLOT1.Example.com") == (
        f"https://t.me/proxy?server=slot1.example.com&port=443&secret={sec}"
    )
    assert mtproto_url(f"https://t.me/proxy?server=203.0.113.7&port=8443&secret=dd{sec}").endswith(f"dd{sec}")


@pytest.mark.parametrize(
    "url",
    [
        "http://t.me/proxy?server=a.com&port=1&secret={s}",
        "https://evil.example/proxy?server=a.com&port=1&secret={s}",
        "https://t.me/socks?server=a.com&port=1&secret={s}",
        "tg://proxy?server=a.com&port=1&secret={s}",
        "https://t.me/proxy?server=a.com&port=1&secret={s}&x=1",
        "https://t.me/proxy?server=a.com&server=b.com&port=1&secret={s}",
        "https://t.me/proxy?server=a.com&port=0&secret={s}",
        "https://t.me/proxy?server=a.com&port=99999&secret={s}",
        "https://t.me/proxy?server=a.com&port=۸۴۴۳&secret={s}",
        "https://t.me/proxy?server=a.com&port=1&secret=zz{s}",
        "https://t.me/proxy?server=slot۱.example.com&port=1&secret={s}",
        "https://t.me/proxy?server=a b.com&port=1&secret={s}",
        'https://t.me/proxy?server=a.com"><script>&port=1&secret={s}',
        "https://t.me/proxy?server=a.com&port=1&secret={s}#frag",
    ],
)
def test_bad_mtproto_urls_are_refused_without_echoing_the_secret(url: str) -> None:
    sec = secret()
    with pytest.raises(ValueError) as exc:
        mtproto_url(url.format(s=sec))
    assert sec not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "vmess://abc",
        "vless://",
        "vless://a b@c:1",
        "vless://a@c:1\n<b>",
        "vless://ä@c:1",
        "vless://" + "a" * 3000,
    ],
)
def test_bad_vless_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        vless_url(url)


def link(**over: Any) -> ProxyLink:
    base: dict[str, Any] = {
        "kind": "mtproto",
        "key": "mtproto:n1:slot1",
        "slot": 1,
        "label": "  پروکسی   ۱ ",
        "url": f"https://t.me/proxy?server=slot1.example.com&port=8443&secret={secret()}",
        "expires_at": None,
        "status": "active",
    }
    base.update(over)
    return ProxyLink(**base)


def test_checked_cleans_labels_and_urls() -> None:
    got = links.checked(link())
    assert got is not None and got.label == "پروکسی ۱" and got.url.startswith("https://t.me/proxy?server=")
    dead = links.checked(link(status="burned", url="garbage"))
    assert dead is not None and dead.status == "burned"  # a dead link needs no URL


@pytest.mark.parametrize(
    "over",
    [
        {"kind": "socks"},
        {"status": "unknown"},
        {"key": ""},
        {"key": "k" * 201},
        {"label": "   "},
        {"slot": True},
        {"url": "https://t.me/proxy?server=a.com&port=1"},
        {"kind": "reality"},  # an MTProto URL is not a vless link
        {"expires_at": datetime(2026, 1, 1)},  # naive
    ],
)
def test_checked_refuses_bad_links(over: dict[str, Any]) -> None:
    assert links.checked(link(**over)) is None


def test_checked_refuses_non_links() -> None:
    assert links.checked({"kind": "mtproto"}) is None


def test_selection_spreads_over_slots_turns_with_each_post_and_prefers_the_newest() -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)

    def cfg(key: str, slot: int | None, hours: float) -> ProxyLink:
        return link(kind="reality", key=key, slot=slot, url="vless://a@b:1", label="ریلیتی",
                    expires_at=now + timedelta(hours=hours))  # fmt: skip

    many = [cfg(f"s{i}", i, 10) for i in range(1, 10)]
    # Slot 2 has an older and a newer config ("ریلیتی ۱۰" would sort before "ریلیتی ۲" by label).
    older, newer = cfg("old2", 2, 3), cfg("new2", 2, 20)
    h = links.Harvest(links={x.key: x for x in [*many, older, newer]})
    first = links.for_new_post(h, now, max_mtproto=0, max_reality=3, rotate=0)
    assert [x.key for x in first] == ["s1", "new2", "s3"]
    second = links.for_new_post(h, now, max_mtproto=0, max_reality=3, rotate=1)
    assert [x.key for x in second] == ["new2", "s3", "s4"]
    wrapped = links.for_new_post(h, now, max_mtproto=0, max_reality=3, rotate=8)
    assert [x.key for x in wrapped] == ["s1", "new2", "s9"]  # slot order, whatever the start
    # Over 9 posts every slot is carried equally often.
    counts: dict[int | None, int] = {}
    for r in range(9):
        for x in links.for_new_post(h, now, max_mtproto=0, max_reality=3, rotate=r):
            counts[x.slot] = counts.get(x.slot, 0) + 1
    assert set(counts.values()) == {3} and len(counts) == 9
    # Fewer slots than places: a second config of a slot is taken only after one of every slot.
    two = links.Harvest(links={x.key: x for x in [cfg("a1", 1, 5), cfg("a2", 1, 9), older, newer]})
    got = links.for_new_post(two, now, max_mtproto=0, max_reality=3)
    assert [x.key for x in got] == ["a2", "a1", "new2"]
    assert links.spread([], 3) == [] and links.spread(many, 0) == []


def test_the_main_channel_never_gets_a_test_nodes_link() -> None:
    # The fleet gives each of its nodes a slot; a test node has none (proxyhub/nodes/service.py).
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    slot = link(kind="mtproto", key="mtproto:slot", slot=3)
    test_mt = link(kind="mtproto", key="mtproto:test", slot=None, label="پروکسی آزمایشی")
    test_re = link(kind="reality", key="reality:9", slot=None, url="vless://a@b:1", label="ریلیتی",
                   expires_at=now + timedelta(hours=20))  # fmt: skip
    h = links.Harvest(links={x.key: x for x in (slot, test_mt, test_re)})
    main = links.for_new_post(h, now, max_mtproto=12, max_reality=3, slots_only=True)
    assert [x.key for x in main] == ["mtproto:slot"]
    test = links.for_new_post(h, now, max_mtproto=12, max_reality=3)
    assert {x.key for x in test} == {"mtproto:slot", "mtproto:test", "reality:9"}


def test_a_post_takes_one_link_per_node_first_then_extras_round_robin() -> None:
    """D49: six links; with fewer nodes, extra links of the same nodes fill the post, one per node
    before a second of any node; with enough nodes no extra is taken at all."""
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

    def mtl(node: int, slot: int | None, extra: int = 0) -> ProxyLink:
        primary = f"mtproto:{uuid.UUID(int=node)}:mt_{node:016x}"
        key = primary if extra == 0 else f"{primary}_x{extra}"
        return link(key=key, slot=slot, extra_of=primary if extra else None)

    two = [mtl(n, n, x) for n in (1, 2) for x in range(3)]
    h = links.Harvest(links={x.key: x for x in two})
    got = links.for_new_post(h, now, max_mtproto=6, max_reality=0)
    assert [(x.slot, x.extra_of is not None) for x in got] == [
        (1, False), (2, False), (1, True), (1, True), (2, True), (2, True)
    ]  # fmt: skip
    four = links.for_new_post(h, now, max_mtproto=4, max_reality=0)
    assert [x.key for x in four] == [two[0].key, two[3].key, two[1].key, two[4].key]  # x1 of each node
    many = [mtl(n, n, x) for n in range(1, 8) for x in range(2)]
    seven = links.for_new_post(
        links.Harvest(links={x.key: x for x in many}), now, max_mtproto=6, max_reality=0
    )
    assert len(seven) == 6 and all(x.extra_of is None for x in seven)  # enough nodes: no extra
    # A test node (no slot): its extras group with it; never in the main channel.
    test_node = [mtl(9, None, x) for x in range(3)]
    mixed = links.Harvest(links={x.key: x for x in [mtl(1, 1), mtl(1, 1, 1), *test_node]})
    test_post = links.for_new_post(mixed, now, max_mtproto=4, max_reality=0)
    assert [x.key for x in test_post] == [mtl(1, 1).key, test_node[0].key, mtl(1, 1, 1).key, test_node[1].key]
    main_post = links.for_new_post(mixed, now, max_mtproto=4, max_reality=0, slots_only=True)
    assert [x.key for x in main_post] == [mtl(1, 1).key, mtl(1, 1, 1).key]


def test_checked_refuses_a_bad_extra_of() -> None:
    assert links.checked(link(extra_of="mtproto:x:mt_a")) is not None
    for bad in ("", "k" * 201):
        assert links.checked(link(extra_of=bad)) is None
    assert links.checked(link(key="mtproto:n1:slot1", extra_of="mtproto:n1:slot1")) is None
    assert links.checked(link(kind="reality", url="vless://a@b:1", extra_of="reality:1")) is None


def test_selection_orders_by_slot_caps_and_skips_ending_configs() -> None:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    h = links.Harvest(
        links={
            x.key: x
            for x in [
                link(key="m3", slot=3),
                link(key="m1", slot=1),
                link(key="m2", slot=2),
                link(key="mx", slot=None),
                link(key="mb", slot=4, status="burned"),
                link(
                    kind="reality", key="r1", slot=1, url="vless://a@b:1", expires_at=now + timedelta(hours=5)
                ),
                link(
                    kind="reality",
                    key="r2",
                    slot=2,
                    url="vless://a@b:1",
                    expires_at=now + timedelta(minutes=30),
                ),
                link(kind="reality", key="r3", slot=3, url="vless://a@b:1", expires_at=None),
            ]
        }
    )
    chosen = links.for_new_post(h, now, max_mtproto=3, max_reality=5)
    assert [x.key for x in chosen] == ["m1", "m2", "m3", "r1", "r3"]
    assert [x.key for x in links.for_new_post(h, now, max_mtproto=0, max_reality=1)] == ["r1"]


# --- how a carried link shows in an edit (proxyhub/posts/publisher.py) -------------------------

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def carried(status: str = "active", **over: Any) -> _Carried:
    base: dict[str, Any] = {"id": 1, "kind": "reality", "key": "reality:7", "label": "ریلیتی ۷",
                            "expires_at": NOW + timedelta(hours=5), "status": status}  # fmt: skip
    base.update(over)
    return _Carried(**base)


def harvest_of(*items: ProxyLink) -> links.Harvest:
    return links.Harvest(links={x.key: x for x in items}, providers=2)


def reality(status: str = "active", expires: datetime | None = None) -> ProxyLink:
    url = "vless://a@slot7.example.com:443"
    return link(kind="reality", key="reality:7", slot=7, url=url, status=status,
                expires_at=expires or NOW + timedelta(hours=5))  # fmt: skip


def test_a_missing_link_is_unsure_first_then_paused_never_dead() -> None:
    for since in (None, NOW, NOW - MISSING_GRACE + timedelta(seconds=1)):
        shown = current_view(carried(missing_since=since), harvest_of(), NOW)
        assert shown.unsure and shown.new_status is None and shown.view.status == "paused"
    for since in (NOW - MISSING_GRACE, NOW - timedelta(days=1)):
        shown = current_view(carried(missing_since=since), harvest_of(), NOW)
        assert not shown.unsure and shown.new_status is None and shown.view.status == "paused"
        assert shown.view.url is None


def test_a_missing_link_past_its_own_expiry_is_expired() -> None:
    for expires in (NOW, NOW - timedelta(minutes=1)):
        shown = current_view(carried(expires_at=expires), harvest_of(), NOW)
        assert (shown.new_status, shown.view.status, shown.unsure) == ("expired", "expired", False)


def test_only_a_reported_status_or_the_expiry_ends_a_link() -> None:
    live = current_view(carried(missing_since=NOW - timedelta(hours=2)), harvest_of(reality()), NOW)
    assert (live.view.status, live.new_status, live.unsure) == ("active", None, False)
    assert live.view.url == "vless://a@slot7.example.com:443"
    for status in ("expired", "full", "burned"):
        shown = current_view(carried(), harvest_of(reality(status=status)), NOW)
        assert (shown.view.status, shown.new_status) == (status, status)
    late = current_view(carried(), harvest_of(reality(expires=NOW - timedelta(seconds=1))), NOW)
    assert late.new_status == "expired"
    # Sticky: once ended in a post, a link never comes back, even if it is listed as active again.
    for status in ("expired", "full", "burned"):
        shown = current_view(carried(status=status), harvest_of(reality()), NOW)
        assert (shown.view.status, shown.new_status, shown.view.url) == (status, None, None)


# --- the day plan --------------------------------------------------------------------------------


DAY = date(2026, 11, 5)


def local(slot: plan.Slot) -> time:
    return slot.at.astimezone(TEHRAN).time()


def test_plan_spreads_posts_over_the_posting_hours() -> None:
    slots = plan.day_plan(DAY, count=10, start=time(8), end=time(0), quiet=[])
    assert len(slots) == 10 and [s.index for s in slots] == list(range(10))
    assert all(time(8) <= local(s) for s in slots) and all(
        s.at.astimezone(TEHRAN).date() == DAY for s in slots
    )
    gaps = [(b.at - a.at).total_seconds() / 60 for a, b in itertools.pairwise(slots)]
    assert all(96 - 2 * 12 - 1 <= g <= 96 + 2 * 12 + 1 for g in gaps), gaps  # 960 min / 10, jitter <= 12
    assert len(set(gaps)) > 1  # jitter: not a clock
    assert plan.day_plan(DAY, count=10, start=time(8), end=time(0), quiet=[]) == slots  # stable


def test_plan_avoids_no_post_windows_and_crosses_midnight() -> None:
    quiet = [QuietWindow.parse("12:00", "14:00"), QuietWindow.parse("23:30", "00:30")]
    slots = plan.day_plan(DAY, count=12, start=time(9), end=time(2), quiet=quiet)
    assert len(slots) == 12
    for s in slots:
        t = local(s)
        assert not any(w.covers(t) for w in quiet), t
    assert slots[-1].at.astimezone(TEHRAN).date() == DAY + timedelta(days=1)  # runs past midnight
    assert slots[-1].at.astimezone(TEHRAN).time() < time(2)


def test_plan_edge_cases() -> None:
    assert plan.day_plan(DAY, count=0, start=time(8), end=time(0), quiet=[]) == []
    assert plan.day_plan(DAY, count=5, start=time(8), end=time(8), quiet=[]) == []
    whole = [QuietWindow.parse("08:00", "10:00")]
    assert plan.day_plan(DAY, count=5, start=time(8), end=time(10), quiet=whole) == []
    tiny = plan.day_plan(DAY, count=48, start=time(8), end=time(8, 10), quiet=[])
    assert len(tiny) == 10  # never more slots than free minutes


@given(
    st.integers(min_value=1, max_value=48),
    st.integers(min_value=0, max_value=1439),
    st.integers(min_value=1, max_value=1439),
    st.dates(min_value=date(2025, 1, 1), max_value=date(2030, 12, 31)),
)
@settings(max_examples=300, deadline=None)
def test_plan_properties(count: int, start_min: int, length: int, day: date) -> None:
    start = time(start_min // 60, start_min % 60)
    end_min = (start_min + length) % 1440
    end = time(end_min // 60, end_min % 60)
    slots = plan.day_plan(day, count=count, start=start, end=end, quiet=[])
    begin, finish = plan.posting_window(day, start, end)
    assert len(slots) == min(count, length)
    assert all(begin <= s.at < finish for s in slots)
    assert [s.at for s in slots] == sorted({s.at for s in slots})


def test_dense_plans_keep_every_slot() -> None:
    # Ten posts in eleven free minutes (found by hypothesis): every slot still gets its own minute.
    slots = plan.day_plan(date(2025, 1, 1), count=10, start=time(0), end=time(0, 11), quiet=[])
    assert len({s.at for s in slots}) == 10
    quiet = [QuietWindow.parse("08:03", "08:58")]
    slots = plan.day_plan(DAY, count=8, start=time(8), end=time(9), quiet=quiet)
    assert len({s.at for s in slots}) == 5  # only 5 free minutes: 08:00-08:02 and 08:58-08:59


def test_due_slot_and_grace() -> None:
    slots = plan.day_plan(DAY, count=10, start=time(8), end=time(0), quiet=[])
    third = slots[2]
    kw: dict[str, Any] = {"count": 10, "start": time(8), "end": time(0), "quiet": []}
    assert plan.due_slot(third.at - timedelta(seconds=1), **kw) == slots[1] or (
        plan.due_slot(third.at - timedelta(seconds=1), **kw) is None
    )
    assert plan.due_slot(third.at, **kw) == third
    assert plan.due_slot(third.at + plan.GRACE, **kw) == third
    assert plan.due_slot(third.at + plan.GRACE + timedelta(seconds=1), **kw) is None
    early = datetime.combine(DAY, time(7), tzinfo=TEHRAN)
    assert plan.due_slot(early, **kw) is None  # yesterday's last slot is long past
    nxt = plan.upcoming(third.at, **kw, limit=2)
    assert [s.index for s in nxt] == [3, 4]


def test_due_slot_reaches_yesterdays_plan_after_midnight() -> None:
    kw: dict[str, Any] = {"count": 10, "start": time(18), "end": time(3), "quiet": []}
    last = plan.day_plan(DAY, **kw)[-1]
    assert last.at.astimezone(TEHRAN).date() == DAY + timedelta(days=1)
    assert plan.due_slot(last.at + timedelta(minutes=1), **kw) == last


# --- settings ------------------------------------------------------------------------------------


def test_settings_defaults_match_the_contract() -> None:
    d = posting.DEFAULTS
    assert (d.enabled, d.main_enabled, d.posts_per_day, d.day_start, d.day_end) == (
        True,
        False,
        10,
        "08:00",
        "00:00",
    )
    assert (d.max_mtproto, d.max_reality, d.edit_window_hours) == (6, 1, 48)  # D48
    assert d.target_role == "test"
    assert d.texts() == StatusTexts()


@pytest.mark.parametrize(
    "over",
    [
        {"posts_per_day": 49},
        {"posts_per_day": -1},
        {"posts_per_day": "10"},
        {"main_enabled": "true"},
        {"main_enabled": 1},
        {"day_start": "8:00"},
        {"day_start": "24:00"},
        {"day_start": "09:00", "day_end": "09:00"},
        {"max_mtproto": 0, "max_reality": 0},
        {"max_reality": 6},
        {"edit_window_hours": 0},
        {"recycle_after_days": 366},
        {"status_texts": {"expired": ""}},
        {"status_texts": {"expired": "x" * 81}},
        {"status_texts": {"other": "x"}},
        {"unknown": 1},
    ],
)
def test_settings_validation(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as exc:
        posting.parse({**posting.DEFAULTS.as_json(), **over})
    assert "x" * 81 not in str(exc.value.errors(include_input=False))


def test_settings_round_trip_and_one_line_texts() -> None:
    texts = {"expired": " ⌛️  تمام \n شد ", "full": "🔴", "burned": "🚫"}
    cfg = posting.parse({**posting.DEFAULTS.as_json(), "status_texts": texts})
    assert cfg.texts().expired == "⌛️ تمام شد"
    assert posting.parse(cfg.as_json()) == cfg


# --- content checks ------------------------------------------------------------------------------


def test_content_checks() -> None:
    data = content.check_input(
        "  عنوان\nدوخطی  ", "متن <b>پررنگ</b><br>خط <script>بد</script>", ["#Tips", "ترفند", "Tips"]
    )
    assert data.title == "عنوان دوخطی"
    assert data.body == "متن <b>پررنگ</b>\nخط بد"
    assert data.tags == ["tips", "ترفند"]
    assert content.check_tags(None) == []
    assert content.check_tags(["حریم‌خصوصی"]) == ["حریم‌خصوصی"]


@pytest.mark.parametrize(
    ("title", "body", "tags", "code"),
    [
        ("", "متن", [], "title_invalid"),
        ("x" * 121, "متن", [], "title_invalid"),
        (None, "متن", [], "title_invalid"),
        ("عنوان", "", [], "body_invalid"),
        ("عنوان", "<b></b>", [], "body_invalid"),
        ("عنوان", "ی" * 1501, [], "body_invalid"),
        ("عنوان", "<b>a</b>" * 400, [], "body_invalid"),  # 3200 HTML characters
        ("عنوان", 5, [], "body_invalid"),
        ("عنوان", "متن", ["دو کلمه"], "tags_invalid"),
        ("عنوان", "متن", ["x"] * 9, "tags_invalid"),
        ("عنوان", "متن", "tag", "tags_invalid"),
        ("عنوان", "متن", [1], "tags_invalid"),
    ],
)
def test_content_check_failures(title: Any, body: Any, tags: Any, code: str) -> None:
    with pytest.raises(ContentInvalid) as exc:
        content.check_input(title, body, tags)
    assert exc.value.code == code
