"""Pure parts of the posts team: post layout, link checks, the day plan, settings and content checks."""

from __future__ import annotations

import itertools
import secrets
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from proxyhub.links.contract import ProxyLink
from proxyhub.posts import content, links, plan
from proxyhub.posts import settings as posting
from proxyhub.posts.content import ContentInvalid
from proxyhub.posts.publisher import MISSING_GRACE, _Carried, current_view, fit
from proxyhub.telegram.schedule import TEHRAN, QuietWindow
from proxyhub.telegram.templates import (
    ALL_DEAD_LINE,
    BODY_TAGS,
    MAX_MESSAGE_CHARS,
    PAUSED_LINE,
    PostLink,
    PostTooLong,
    StatusTexts,
    compose_content,
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
    return PostLink(kind="mtproto", label=f"پروکسی {i}", status=status, url=url)  # type: ignore[arg-type]


def rl(i: int, status: str = "active", expires: datetime | None = None) -> PostLink:
    url = vless_url(f"vless://{secrets.token_hex(4)}-aaaa-bbbb-cccc-{secrets.token_hex(6)}@slot{i}.example.com:443"
                    f"?security=reality&sni=www.example.com&fp=chrome#ipoxxo-{i}")  # fmt: skip
    return PostLink(kind="reality", label=f"ریلیتی {i}", status=status, url=url, expires_at=expires)  # type: ignore[arg-type]


CONTENT = compose_content("💡 عنوان <آزمایشی>", "متن <b>پررنگ</b> و <i>کج</i>\nخط دوم")


# --- layout --------------------------------------------------------------------------------------


def test_post_has_content_links_and_tag_in_that_order() -> None:
    sec = secret()
    exp = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)  # Thursday 18:30 Tehran
    post = render_post(CONTENT, [mt(1, sec=sec), mt(2), rl(1, expires=exp)], "@ipoxxo").text
    assert post.startswith("<b>💡 عنوان &lt;آزمایشی&gt;</b>\n\n<blockquote>متن <b>پررنگ</b>")
    assert (
        post.index("</blockquote>") < post.index("📡 <b>پروکسی تلگرام</b>") < post.index("🔐 <b>کانفیگ VLESS")
    )
    assert post.endswith("\n\n🔗 \u200e@ipoxxo")
    assert (
        f'<a href="https://t.me/proxy?server=slot1.example.com&amp;port=8443&amp;secret={sec}">پروکسی 1</a>'
        in post
    )
    assert " • " in post
    assert "<b>ریلیتی 1</b> — ⏳ اعتبار تا پنجشنبه ساعت ۱۸:۳۰\n<code>vless://" in post
    assert ALL_DEAD_LINE not in post
    assert fits(post)
    assert "·" not in post  # a middle dot next to Persian digits reads as a zero


def test_rendering_is_deterministic() -> None:
    items = [mt(1), mt(2), rl(1, expires=datetime(2026, 11, 5, tzinfo=UTC))]
    assert render_post(CONTENT, items, "@ipoxxo") == render_post(CONTENT, items, "@ipoxxo")


def test_dead_links_lose_their_link_and_get_marks() -> None:
    sec = secret()
    live = mt(2, sec=sec)
    items = [
        PostLink(kind="mtproto", label="پروکسی 1", status="burned"),
        live,
        PostLink(kind="mtproto", label="پروکسی 3", status="expired"),
        PostLink(kind="mtproto", label="پروکسی 4", status="full"),
        PostLink(kind="reality", label="ریلیتی 1", status="expired"),
        PostLink(kind="reality", label="ریلیتی 2", status="full"),
        PostLink(kind="reality", label="ریلیتی 3", status="burned"),
    ]
    post = render_post(CONTENT, items, "@ipoxxo").text
    assert "<s>پروکسی 1</s> 🚫" in post
    assert "<s>پروکسی 3</s> ⌛️" in post
    assert "<s>پروکسی 4</s> 🔴" in post
    assert post.count("<a href=") == 1 and sec in post
    assert "<s>ریلیتی 1</s> — ⌛️ زمان این کانفیگ تمام شد" in post
    assert "<s>ریلیتی 2</s> — 🔴 ظرفیت این کانفیگ پر شد" in post
    assert "<s>ریلیتی 3</s> — 🚫 این کانفیگ از دسترس خارج شد" in post
    assert "<code>" not in post and "vless://" not in post
    assert ALL_DEAD_LINE not in post  # one link still works


def test_all_dead_gets_the_closing_line_and_owner_texts() -> None:
    texts = StatusTexts(expired="⌛️ تمام شد <b>", full="🔴 پر", burned="🚫 رفت")
    items = [PostLink(kind="mtproto", label="پروکسی 1", status="burned"),
             PostLink(kind="reality", label="ریلیتی 1", status="expired")]  # fmt: skip
    post = render_post(CONTENT, items, "@ipoxxo", texts).text
    assert ALL_DEAD_LINE in post
    assert "⌛️ تمام شد &lt;b&gt;" in post  # owner text is escaped
    assert post.index(ALL_DEAD_LINE) < post.index("🔗 \u200e@ipoxxo")


def test_expiry_names_the_tehran_weekday() -> None:
    # 23:00 UTC on Thursday is 02:30 on Friday in Tehran: the weekday follows Tehran, not UTC.
    late = render_post(None, [rl(1, expires=datetime(2026, 11, 5, 23, 0, tzinfo=UTC))], "@ipoxxo").text
    assert "اعتبار تا جمعه ساعت ۰۲:۳۰" in late
    days = {
        render_post(None, [rl(1, expires=datetime(2026, 11, d, 9, 0, tzinfo=UTC))], "@ipoxxo")
        .text.split("اعتبار تا ", 1)[1]
        .split(" ساعت", 1)[0]
        for d in range(2, 9)
    }
    assert days == {"دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"}


def test_paused_links_keep_their_label_without_a_link_and_are_not_dead() -> None:
    sec = secret()
    items = [
        PostLink(kind="mtproto", label="پروکسی 1", status="paused", url=mt(1, sec=sec).url),
        PostLink(kind="reality", label="ریلیتی 1", status="paused", url=rl(1).url),
    ]
    post = render_post(None, items, "@ipoxxo").text
    assert "پروکسی 1 ⏸" in post and "<s>پروکسی 1</s>" not in post
    assert f"<b>ریلیتی 1</b> — {PAUSED_LINE}" in post and "<s>ریلیتی 1</s>" not in post
    assert "<a " not in post and "<code>" not in post and sec not in post and "vless://" not in post
    assert ALL_DEAD_LINE not in post  # paused is not dead: it may come back
    dead_and_paused = [PostLink(kind="mtproto", label="پروکسی 2", status="burned"), items[0]]
    assert ALL_DEAD_LINE not in render_post(None, dead_and_paused, "@ipoxxo").text


def test_body_tags_have_no_custom_emoji_for_the_bot() -> None:
    assert "tg-emoji" not in BODY_TAGS
    out = compose_content("t", 'a <tg-emoji emoji-id="5368324170671202286">👍</tg-emoji> b')
    assert "tg-emoji" not in out and "👍" in out


def test_active_link_without_url_is_never_a_link() -> None:
    post = render_post(None, [PostLink(kind="mtproto", label="پروکسی 1", status="active")], "@ipoxxo").text
    assert "<a " not in post and "<s>پروکسی 1</s> 🚫" in post


def test_a_dead_link_never_shows_its_url_even_if_it_still_has_one() -> None:
    sec = secret()
    dead = [
        PostLink(kind="mtproto", label="پروکسی 1", status="burned", url=mt(1, sec=sec).url),
        PostLink(kind="reality", label="ریلیتی 1", status="expired", url=rl(1).url),
    ]
    post = render_post(None, dead, "@ipoxxo").text
    assert "<a " not in post and "<code>" not in post and sec not in post and "vless://" not in post


def test_labels_are_escaped_and_bounded() -> None:
    item = PostLink(kind="mtproto", label="<b>x</b>" + "ی" * 100, status="burned")
    post = render_post(None, [item], "@ipoxxo").text
    assert "&lt;b&gt;x&lt;/b&gt;" in post and "ی" * 30 not in post


def test_content_is_shortened_but_links_and_tag_never() -> None:
    long_body = "کلمه " * 2000
    many = [mt(i) for i in range(1, 13)] + [rl(i) for i in range(1, 4)]
    post = render_post(compose_content("عنوان", long_body[:2900]), many, "@ipoxxo")
    assert fits(post.text)
    assert post.text.count("<a href=") == 12 and post.text.count("<code>") == 3
    assert post.text.endswith("🔗 \u200e@ipoxxo")
    assert post.content_html.endswith("…</blockquote>")
    assert len(post.text) <= MAX_MESSAGE_CHARS and utf16_len(visible_text(post.text)) <= MAX_MESSAGE_CHARS


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
    assert fits(post) and post.endswith("🔗 \u200e@ipoxxo")


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
    assert (d.max_mtproto, d.max_reality, d.edit_window_hours) == (12, 3, 48)
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
