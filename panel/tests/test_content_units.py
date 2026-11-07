# ruff: noqa: E501
"""The content engine without a database (decision D51): the fetch guard on a real local HTTPS site,
the Telegram preview and feed parsers, the caption cleaning and the media checks.

The preview page below follows the markup of Telegram's real public preview (t.me/s/<name>, read on
2026-11-06: BBC Persian and Durov's channel); the real page itself is read by test_content_real.py.
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_content_support import LocalSite, Page, jpeg_header, mp4, png, site_http

from proxyhub.content import clean, media
from proxyhub.content.items import SourceInvalid
from proxyhub.content.net import (
    FetchError,
    GuardedBackend,
    SafeHttp,
    check_url,
    public_address,
    valid_hostname,
)
from proxyhub.content.rss import RssFeed, parse_feed
from proxyhub.content.telegram_preview import (
    TelegramPreview,
    check_name,
    parse_duration,
    parse_page,
    parse_views,
)

# --- URLs and addresses ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://news.example/feed",  # not https
        "https://news.example:8443/feed",  # not 443
        "https://user:pw@news.example/",  # credentials
        "https://127.0.0.1/feed",  # an IP literal
        "https://[::1]/feed",
        "https://localhost/feed",
        "https://printer.local/feed",
        "https://intranet/feed",  # one label
        "https://metadata.google.internal/",
        "ftp://news.example/feed",
        "https://news.example/a b",
        "",
    ],
)
def test_bad_urls_are_refused(url: str) -> None:
    with pytest.raises(FetchError, match="bad_url"):
        check_url(url)


def test_good_urls_are_normalized() -> None:
    assert check_url("https://News.Example/fa/خبر?x=۱#top") == (
        "https://news.example/fa/%D8%AE%D8%A8%D8%B1?x=%DB%B1"
    )
    assert check_url("https://t.me/s/bbcpersian") == "https://t.me/s/bbcpersian"
    assert valid_hostname("cdn4.telesco.pe") and not valid_hostname("CDN.example")


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("149.154.167.99", True),
        ("2001:4860:4860::8888", True),
        ("10.0.0.5", False),
        ("172.16.3.4", False),
        ("192.168.1.1", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),  # the cloud metadata address
        ("100.64.0.1", False),  # CGNAT
        ("0.0.0.0", False),  # noqa: S104
        ("224.0.0.1", False),
        ("192.0.2.10", False),  # documentation
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4 inside IPv6 is judged as IPv4
        ("::ffff:8.8.8.8", True),
        ("2002:7f00:1::", False),  # 6to4 of 127.0.0.1
        ("64:ff9b::a00:5", False),  # NAT64
        ("not an address", False),
    ],
)
def test_only_global_unicast_addresses_are_public(address: str, public: bool) -> None:
    assert public_address(address) is public


async def test_the_guarded_backend_refuses_before_any_connection() -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return {"a.example": ["10.0.0.1"], "b.example": ["8.8.8.8", "127.0.0.1"], "c.example": []}[host]

    backend = GuardedBackend(resolver=resolve)
    for host in ("a.example", "b.example", "c.example", "localhost", "127.0.0.1"):
        with pytest.raises(FetchError, match="blocked_address"):
            await backend.connect_tcp(host, 443)
    with pytest.raises(FetchError, match="blocked_address"):
        await backend.connect_tcp("a.example", 80)
    with pytest.raises(FetchError, match="blocked_address"):
        await backend.connect_unix_socket("/run/docker.sock")


# --- the fetcher on a real local HTTPS site -------------------------------------------------------


async def test_a_page_is_read_and_every_redirect_hop_is_checked(tmp_path: Path) -> None:
    async with LocalSite(tmp_path) as site, site_http(site) as http:
        site.pages[("news.example", "/feed")] = Page(
            body=b"<rss/>", headers={"Content-Type": "text/xml; charset=UTF-8"}
        )
        site.pages[("news.example", "/hop")] = Page(302, headers={"Location": "/feed"})
        got = await http.get("https://news.example/hop", limit=1000)
        assert (got.url, got.body, got.content_type, got.charset) == (
            "https://news.example/feed",
            b"<rss/>",
            "text/xml",
            "utf-8",
        )
        assert site.requests[0][2]["accept-encoding"] == "identity"
        for location, code in (
            ("https://evil.example/x", "blocked_address"),  # resolves to 10.0.0.5
            ("https://metadata.example/latest", "blocked_address"),  # 169.254.169.254
            ("https://mixed.example/x", "blocked_address"),  # one private address is enough
            ("http://news.example/feed", "bad_url"),
            ("https://127.0.0.1/feed", "bad_url"),
            ("https://news.example:8443/feed", "bad_url"),
        ):
            site.pages[("news.example", "/away")] = Page(301, headers={"Location": location})
            with pytest.raises(FetchError, match=code):
                await http.get("https://news.example/away", limit=1000)
        site.pages[("news.example", "/loop")] = Page(302, headers={"Location": "/loop"})
        with pytest.raises(FetchError, match="too_many_redirects"):
            await http.get("https://news.example/loop", limit=1000)
        with pytest.raises(FetchError, match="http_404"):
            await http.get("https://news.example/nothing", limit=1000)
        # The guard is the connection's own, not the first URL's: a direct request is refused too.
        with pytest.raises(FetchError, match="blocked_address"):
            await http.get("https://evil.example/", limit=1000)
    assert all(host != "evil.example" for host, _, _ in site.requests)


async def test_bodies_are_bounded_also_when_compressed(tmp_path: Path) -> None:
    async with LocalSite(tmp_path) as site, site_http(site) as http:
        site.pages[("news.example", "/big")] = Page(body=b"x" * 5000)
        site.pages[("news.example", "/unsized")] = Page(body=b"x" * 5000, send_length=False)
        bomb = gzip.compress(b"\x00" * 2_000_000)
        site.pages[("news.example", "/bomb")] = Page(body=bomb, headers={"Content-Encoding": "gzip"})
        site.pages[("news.example", "/small.gz")] = Page(
            body=gzip.compress(b"ok"), headers={"Content-Encoding": "gzip"}
        )
        site.pages[("news.example", "/br")] = Page(body=b"x", headers={"Content-Encoding": "br"})
        with pytest.raises(FetchError, match="too_big"):
            await http.get("https://news.example/big", limit=4096)
        with pytest.raises(FetchError, match="too_big"):  # no length given: counted while it streams
            await http.get("https://news.example/unsized", limit=4096)
        with pytest.raises(FetchError, match="too_big"):
            await http.get("https://news.example/bomb", limit=100_000)
        assert (await http.get("https://news.example/small.gz", limit=100)).body == b"ok"
        with pytest.raises(FetchError, match="bad_encoding"):
            await http.get("https://news.example/br", limit=100)


async def test_the_certificate_is_checked_for_the_name(tmp_path: Path) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    async with LocalSite(tmp_path) as site:
        site.pages[("news.example", "/")] = Page(body=b"ok")
        # the default certificate store does not know the test CA
        async with SafeHttp(
            resolver=resolve, allow=lambda a: a == "127.0.0.1", connect_port=site.port
        ) as http:
            with pytest.raises(FetchError, match="tls_failed"):
                await http.get("https://news.example/", limit=100)


# --- Telegram's public preview --------------------------------------------------------------------

BG = "background-image:url('https://cdn4.telesco.pe/file/{}.jpg')"


def _message(post: int, inner: str, views: str = "6.79K", classes: str = "") -> str:
    return (
        f'<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message '
        f'text_not_supported_wrap js-widget_message {classes}" data-post="BBCPersian/{post}" data-view="x">'
        '<div class="tgme_widget_message_user"><a href="https://t.me/bbcpersian"><i class="tgme_widget_message_user_photo">'
        f'<img src="https://cdn4.telesco.pe/file/avatar.jpg"></i></a></div><div class="tgme_widget_message_bubble">{inner}'
        '<div class="tgme_widget_message_footer compact js-message_footer"><div class="tgme_widget_message_info short">'
        f'<span class="tgme_widget_message_views">{views}</span><span class="copyonly"> views</span>'
        '<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/bbcpersian/1">'
        f'<time datetime="2026-11-06T17:{post % 60:02d}:32+00:00" class="time">17:17</time></a></span></div></div>'
        "</div></div></div>"
    )


PREVIEW = (
    '<html><body><div class="tgme_channel_info"><div class="tgme_channel_info_header">BBC</div></div>'
    '<section class="tgme_channel_history">'
    + _message(
        101,
        f'<a class="tgme_widget_message_photo_wrap 1 2" href="https://t.me/bbcpersian/101" style="width:800px;{BG.format("p101")}">'
        '<div class="tgme_widget_message_photo"></div></a>'
        '<div class="tgme_widget_message_text js-message_text" dir="auto"><i class="emoji"><b>🔻</b></i>'
        'وزیر خارجه عراق هشدار داد.<br/><br/>متن دوم &amp; بیشتر <a href="https://t.me/BBCPersian">@BBCPersian</a></div>',
        views="1.2M",
    )
    + _message(
        102,
        '<a class="tgme_widget_message_reply" href="https://t.me/bbcpersian/90"><div class="tgme_widget_message_text">'
        "متن پست قبلی که نباید بیاید</div></a>"
        '<a class="tgme_widget_message_video_player js-message_video_player" href="https://t.me/bbcpersian/102">'
        f'<i class="tgme_widget_message_video_thumb" style="{BG.format("thumb102")}"></i>'
        '<video src="https://cdn4.telesco.pe/file/blur.mp4?token=a" class="tgme_widget_message_video blured js-message_video_blured"></video>'
        '<div class="tgme_widget_message_video_wrap"><video src="https://cdn4.telesco.pe/file/v102.mp4?token=b" '
        'class="tgme_widget_message_video js-message_video"></video></div>'
        '<time class="message_video_duration js-message_video_duration">1:05</time></a>'
        '<div class="tgme_widget_message_text js-message_text">ویدیوی تازه</div>',
        views="980",
    )
    + _message(
        103,
        '<div class="tgme_widget_message_text js-message_text">فقط متن با <a href="https://example.com/x">لینک</a></div>'
        '<a class="tgme_widget_message_link_preview" href="https://example.com/x">'
        f'<i class="link_preview_image" style="{BG.format("preview")}"></i>'
        f'<a class="tgme_widget_message_photo_wrap" style="{BG.format("in_preview")}"></a></a>',
    )
    + _message(104, '<div class="tgme_widget_message_text">پیام سرویس</div>', classes="service_message")
    + _message(
        105,
        '<a class="tgme_widget_message_photo_wrap" style="background-image:url(\'http://insecure.example/p.jpg\')"></a>'
        '<div class="tgme_widget_message_text">عکس ناامن</div>',
    )
    + "</section></body></html>"
)


def test_the_preview_page_gives_text_media_views_and_time() -> None:
    items, is_channel = parse_page(PREVIEW, "bbcpersian")
    assert is_channel
    assert [i.external_id for i in items] == ["101", "102", "103", "105"]  # the service message is skipped
    photo, video, plain, insecure = items
    assert photo.url == "https://t.me/bbcpersian/101"
    assert photo.text == "🔻وزیر خارجه عراق هشدار داد.\n\nمتن دوم & بیشتر @BBCPersian"
    assert photo.views == 1_200_000
    assert photo.published_at == datetime(2026, 11, 6, 17, 41, 32, tzinfo=UTC)
    assert photo.media is not None and photo.media.kind == "photo"
    assert photo.media.url == "https://cdn4.telesco.pe/file/p101.jpg"
    assert video.text == "ویدیوی تازه"  # the reply's quote is not this post's text
    assert video.media is not None and video.media.kind == "video"
    assert video.media.url == "https://cdn4.telesco.pe/file/v102.mp4?token=b"  # not the blurred copy
    assert video.media.thumb_url == "https://cdn4.telesco.pe/file/thumb102.jpg"
    assert video.media.duration == 65 and video.views == 980
    assert plain.media is None  # a link preview's image is not the post's media
    assert insecure.media is None  # http is dropped


def test_preview_numbers() -> None:
    assert parse_views("6.79K") == 6790 and parse_views("1.2M") == 1_200_000 and parse_views("980") == 980
    assert parse_views("") is None and parse_views("lots") is None
    assert parse_duration("0:20") == 20 and parse_duration("1:02:03") == 3723 and parse_duration("x") is None


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("@BBCPersian", "bbcpersian"),
        ("bbcpersian", "bbcpersian"),
        ("https://t.me/bbcpersian", "bbcpersian"),
        ("t.me/s/bbcpersian/", "bbcpersian"),
        ("https://telegram.me/Iran_News_1", "iran_news_1"),
    ],
)
def test_channel_names(raw: str, name: str) -> None:
    assert check_name(raw) == name
    assert TelegramPreview().link(name) == f"https://t.me/{name}"


@pytest.mark.parametrize(
    "raw",
    ["", "@abc", "https://t.me/joinchat/AAAA", "https://t.me/+AbCdEf", "https://evil.example/bbc", "a__b_c",
     "1channel", "chan_", "https://t.me/bbcpersian/123", "@" + "a" * 40, "https://t.me/proxy"],
)  # fmt: skip
def test_bad_channel_names(raw: str) -> None:
    with pytest.raises(SourceInvalid, match="telegram_name_invalid"):
        check_name(raw)


async def test_the_preview_plugin_goes_through_the_guard_too(tmp_path: Path) -> None:
    async with LocalSite(tmp_path) as site, site_http(site) as http:
        assert TelegramPreview().page_url("bbcpersian") == "https://t.me/s/bbcpersian"
        with pytest.raises(FetchError, match="blocked_address"):  # t.me has no address in this test
            await TelegramPreview().fetch(http, "bbcpersian")
    # a page with neither posts nor the channel's header is not a channel's preview
    assert parse_page("<html><body>Telegram: Contact @x</body></html>", "x") == ([], False)


# --- feeds ----------------------------------------------------------------------------------------

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>خبر</title>
<item><title><![CDATA[«تونلی تاریک»؛ هشدار عراق]]></title>
<description><![CDATA[<p>فواد حسین هشدار داد.</p><script>alert(1)</script><img src="https://cdn.example/a.jpg">]]></description>
<link>https://news.example/a?utm=rss</link><guid isPermaLink="false">https://news.example/a#0</guid>
<pubDate>Fri, 06 Nov 2026 10:15:18 GMT</pubDate></item>
<item><title>ویدیو</title><link>https://news.example/v</link>
<media:content url="https://cdn.example/v.mp4" type="video/mp4" medium="video"/>
<media:thumbnail url="https://cdn.example/v.jpg"/></item>
<item><title>با پیوست</title><link>http://news.example/insecure</link><guid>g3</guid>
<enclosure url="http://cdn.example/x.jpg" type="image/jpeg" length="1"/></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>عنوان اتم</title><id>tag:news.example,2026:1</id><updated>2026-11-06T12:00:00Z</updated>
<link rel="alternate" href="https://news.example/atom/1"/><link rel="enclosure" type="image/png" href="https://cdn.example/1.png"/>
<summary type="html">&lt;b&gt;خلاصه&lt;/b&gt; خبر</summary></entry>
</feed>"""


def test_rss_and_atom_entries() -> None:
    a, v, plain = parse_feed(RSS.encode())
    assert a.external_id == "https://news.example/a#0" and a.url == "https://news.example/a?utm=rss"
    assert a.text == "«تونلی تاریک»؛ هشدار عراق\nفواد حسین هشدار داد."  # no script text
    assert a.published_at == datetime(2026, 11, 6, 10, 15, 18, tzinfo=UTC)
    assert a.media is not None and (a.media.kind, a.media.url) == ("photo", "https://cdn.example/a.jpg")
    assert v.external_id == "https://news.example/v"
    assert (
        v.media is not None and v.media.kind == "video" and v.media.thumb_url == "https://cdn.example/v.jpg"
    )
    assert plain.url is None and plain.media is None  # http links and files are dropped
    (entry,) = parse_feed(ATOM.encode())
    assert (entry.external_id, entry.url) == ("tag:news.example,2026:1", "https://news.example/atom/1")
    assert entry.text == "عنوان اتم\nخلاصه خبر"
    assert entry.media is not None and entry.media.url == "https://cdn.example/1.png"


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (  # billion laughs
            '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
            "<rss><channel><item><title>&lol2;</title></item></channel></rss>",
            "feed_unsafe",
        ),
        (  # an external entity: a local file
            '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item>'
            "<title>&x;</title></item></channel></rss>",
            "feed_unsafe",
        ),
        ('<!DOCTYPE rss SYSTEM "https://evil.example/x.dtd"><rss/>', "feed_unsafe"),
        ("<rss><channel><item><title>&undefined;</title></item></channel></rss>", "feed_invalid"),
        ("<html><body>not a feed</body></html>", "feed_invalid"),
        ("<rss>" + "<a>" * 100 + "</a>" * 100 + "</rss>", "feed_invalid"),  # too deep
        ("not xml at all", "feed_invalid"),
    ],
)
async def test_unsafe_or_broken_feeds_are_refused(tmp_path: Path, document: str, code: str) -> None:
    async with LocalSite(tmp_path) as site, site_http(site) as http:
        site.pages[("news.example", "/feed")] = Page(
            body=document.encode(), headers={"Content-Type": "application/rss+xml"}
        )
        with pytest.raises(FetchError, match=code):
            await RssFeed().fetch(http, "https://news.example/feed")


async def test_a_feed_is_read_over_the_guard(tmp_path: Path) -> None:
    async with LocalSite(tmp_path) as site, site_http(site) as http:
        site.pages[("news.example", "/feed")] = Page(
            body=RSS.encode(), headers={"Content-Type": "application/rss+xml"}
        )
        site.pages[("news.example", "/page")] = Page(
            body=b"<html></html>", headers={"Content-Type": "text/html"}
        )
        assert len(await RssFeed().fetch(http, "https://news.example/feed")) == 3
        with pytest.raises(FetchError, match="bad_content_type"):
            await RssFeed().fetch(http, "https://news.example/page")


@pytest.mark.parametrize(
    "raw", ["http://news.example/feed", "https://10.0.0.1/feed", "news.example/feed", "javascript:alert(1)"]
)
def test_bad_feed_urls(raw: str) -> None:
    with pytest.raises(SourceInvalid, match="feed_url_invalid"):
        RssFeed().check_ref(raw)


# --- cleaning and captions ------------------------------------------------------------------------


def test_links_mentions_tags_and_the_signature_go() -> None:
    text = (
        "‌\n🔻مارک لوون، خبرنگار بی‌بی‌سی، مصاحبه را نیمه‌تمام گذاشت. https://bbc.in/x\n\n"
        "بنابر آمار #وزارت_کشور فرانسه، ۶۰۰ هزار نفر شرکت کردند. #فوری\n"
        "#فرانسه #اعتراض\n"
        "➖➖➖➖\n"
        "🆔 @BBCPersian\n"
        "اینستاگرام | تلگرام | یوتیوب"
    )
    cleaned = clean.clean_source_text(text, ["bbcpersian", "بی‌بی‌سی فارسی"])
    assert cleaned == (
        "🔻مارک لوون، خبرنگار بی‌بی‌سی، مصاحبه را نیمه‌تمام گذاشت.\n\n"
        "بنابر آمار وزارت کشور فرانسه، ۶۰۰ هزار نفر شرکت کردند. فوری"
    )
    # one platform's name in the news itself is not a signature
    assert (
        clean.clean_source_text("دسترسی به تلگرام در ایران محدودتر شد")
        == "دسترسی به تلگرام در ایران محدودتر شد"
    )
    assert clean.clean_source_text("t.me/joinchat/abc @x #y_z") == "y z"
    assert clean.clean_source_text("‮متن‏ معکوس​") == "متن معکوس"


def test_the_caption_is_a_title_and_a_short_body() -> None:
    assert clean.split_caption("🔻عنوان کوتاه\n\nمتن اول.\nمتن دوم.") == (
        "🔻عنوان کوتاه",
        "متن اول.\nمتن دوم.",
    )
    long_first = "جمله‌ی اول خبر است که طولانی هم هست. " + "واژه " * 40
    title, body = clean.split_caption(long_first)
    assert title == "جمله‌ی اول خبر است که طولانی هم هست." and body.startswith("واژه")
    title, body = clean.split_caption("واژه " * 60)
    assert title.endswith("…") and len(title) <= clean.TITLE_MAX and body.startswith("…")
    body = clean.shorten("یک. " * 200, 100)
    assert len(body) <= 100 and body.endswith(".")
    assert clean.split_caption("") == ("", "")


def test_same_news_from_two_sources_is_similar() -> None:
    a = clean.norm_text(
        "فواد حسین، وزیر خارجه عراق نسبت به ورود گروه‌های مسلح نزدیک به ایران به هرگونه درگیری نظامی "
        "احتمالی میان ایران و آمریکا هشدار داد و گفت چنین اقدامی عراق را به تونلی تاریک می‌کشاند."
    )
    b = clean.norm_text(
        "🔴 فوری | وزير خارجه عراق، فواد حسين، نسبت به ورود گروه‌هاي مسلح نزديك به ايران به هرگونه درگيري "
        "نظامي احتمالي ميان ايران و آمريكا هشدار داد https://t.me/x"
    )
    c = clean.norm_text(
        "تیم ملی فوتبال ایران در دیدار دوستانه برابر تیم ملی ازبکستان با دو گل به پیروزی رسید و "
        "سرمربی تیم از عملکرد بازیکنان جوان راضی بود."
    )
    assert clean.similar(a, b) and clean.similar(b, a)
    assert not clean.similar(a, c)
    assert not clean.similar(clean.norm_text("🇦🇫😈"), clean.norm_text("🇦🇫😈"))  # too short to tell
    assert clean.norm_text("كتاب ۱۲۳ يكشنبه") == "کتاب 123 یکشنبه"


def test_html_to_text_drops_scripts_and_keeps_breaks() -> None:
    text, img = clean.html_to_text(
        '<p>یک</p><script>bad()</script><style>x{}</style><p>دو<br>سه</p><img src="a.jpg">'
    )
    assert text == "یک\n\nدو\nسه" and img == "a.jpg"


# --- media ----------------------------------------------------------------------------------------


def test_real_bytes_decide_the_type() -> None:
    assert media.sniff(png(4, 3)) == media.MediaInfo("image/png", 4, 3)
    assert media.sniff(jpeg_header(1280, 720)) == media.MediaInfo("image/jpeg", 1280, 720)
    assert media.sniff(mp4(640, 360, 12)) == media.MediaInfo("video/mp4", 640, 360, 12)
    for data, code in (
        (mp4(brand=b"qt  "), "video_not_mp4"),
        (b"GIF89a....", "unknown_type"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "unknown_type"),
        (b"\xff\xd8\xff\xd9", "image_broken"),
        (jpeg_header(9000, 2000), "image_shape"),  # width + height over 10000
        (jpeg_header(4200, 200), "image_shape"),  # ratio over 20
        (b"\x00\x00\x00\x18ftypisom" + b"\x00" * 12, "video_no_moov"),
    ):
        with pytest.raises(media.MediaRefused, match=code):
            media.sniff(data)


def test_each_role_has_its_type_and_size() -> None:
    assert media.check_for("photo", png()).mime == "image/png"
    assert media.check_for("video", mp4()).mime == "video/mp4"
    with pytest.raises(media.MediaRefused, match="wrong_type"):
        media.check_for("photo", mp4())
    with pytest.raises(media.MediaRefused, match="wrong_type"):
        media.check_for("video", png())
    with pytest.raises(media.MediaRefused, match="too_big"):
        media.check_for("thumb", png() + b"\x00" * media.THUMB_MAX_BYTES)
    with pytest.raises(media.MediaRefused, match="empty"):
        media.check_for("photo", b"")
    # the declared type may not contradict the role, and never replaces the real bytes
    assert media.check_download("photo", "image/png", png()).mime == "image/png"
    assert media.check_download("video", "application/octet-stream", mp4()).mime == "video/mp4"
    for kind, declared, data in (
        ("photo", "text/html", png()),
        ("video", "image/png", mp4()),
        ("thumb", "video/mp4", png()),
    ):
        with pytest.raises(media.MediaRefused, match="wrong_type"):
            media.check_download(kind, declared, data)  # type: ignore[arg-type]
    with pytest.raises(media.MediaRefused, match="unknown_type"):
        media.check_download("photo", "image/jpeg", b"<svg/>")
    # the panel's limits stay under Telegram's (bot: 10 MB photo, 50 MB file)
    assert media.PHOTO_MAX_BYTES < media.TG_BOT_PHOTO_MAX and media.VIDEO_MAX_BYTES < media.TG_BOT_FILE_MAX


def test_a_caption_body_keeps_its_lines_and_one_bold_phrase() -> None:
    """The owner (2026-11-07, after the competitor's channel): short lines, the key phrase bold."""
    from proxyhub.content.candidates import body_html, caption_html

    assert body_html("خط اول\n**این مهم است**\nخط سوم") == "خط اول\n<b>این مهم است</b>\nخط سوم"
    assert body_html("<i>x</i> **a<b>c**") == "&lt;i&gt;x&lt;/i&gt; <b>a&lt;b&gt;c</b>"  # escaped first
    assert body_html("ستاره‌ی تنها ** بی جفت") == "ستاره‌ی تنها  بی جفت"  # a stray pair goes
    assert body_html("**دو\nخط**") == "دو\nخط"  # bold never spans a line
    html_out = caption_html("🔻 عنوان", "خط یک\n**مهم**")
    assert html_out == "<b>🔻 عنوان</b>\n\nخط یک\n<b>مهم</b>" and "<blockquote" not in html_out
