"""Pure logic of the Telegram part: template, quiet windows, pacing, Bot API response parsing (real
error JSON shapes as data), pairing codes, redaction, the method allowlist. No network (the allowlist
test points the client at a refused local port, which it must never reach) and no database here."""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from test_telegram_support import BOT_API_ERRORS, closed_port_bot, random_token

from proxyhub.telegram import pairing
from proxyhub.telegram.client import (
    ALLOWED_METHODS,
    CAN_REMOVE_POSTS_FLAG,
    TOKEN_RE,
    BadRequest,
    BotClient,
    BotUser,
    ChatInfo,
    Conflict,
    Forbidden,
    InvalidResponse,
    InvalidToken,
    MemberRights,
    NotFound,
    RetryAfter,
    SentMessage,
    ServerError,
    Unauthorized,
    Update,
    parse_api_response,
    redact,
    retry_after_seconds,
)
from proxyhub.telegram.schedule import (
    CHANNEL_GAP,
    OWNER_GAP,
    TEHRAN,
    InvalidSchedule,
    QuietWindow,
    backoff,
    covers_whole_day,
    next_post_time,
    next_send_time,
    quiet_until,
    validate_interval,
    validate_windows,
)
from proxyhub.telegram.templates import (
    MAX_MESSAGE_CHARS,
    PostTooLong,
    channel_tag,
    proxy_link,
    render_proxy_post,
    sanitize_html,
    utf16_len,
    visible_text,
)
from proxyhub.vault import SecretValue

REPO = Path(__file__).resolve().parents[2]
SECRET = "0123456789abcdef0123456789abcdef"


def tehran(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=TEHRAN).astimezone(UTC)


def body(name: str) -> bytes:
    return json.dumps(BOT_API_ERRORS[name]).encode()


# --- template (D17) ------------------------------------------------------------------------------


def test_post_has_content_proxy_words_and_channel_tag_last() -> None:
    post = render_proxy_post(
        "<b>خبر مهم</b>\nمتن کوتاه",
        [("203.0.113.10", 443, SECRET), ("p2.example.com", 8443, "ee" + SECRET + "676f6f676c652e636f6d")],
        "@ipoxxo",
    )
    assert post.startswith("<b>خبر مهم</b>\nمتن کوتاه\n\n📡 <b>پروکسی‌ها</b>\n")
    assert post.splitlines()[-1] == "🔗 @ipoxxo"
    words = re.findall(r'<a href="([^"]+)">([^<]+)</a>', post)
    assert [w for _, w in words] == ["پروکسی ۱", "پروکسی ۲"]
    first = urlsplit(html.unescape(words[0][0]))
    assert (first.scheme, first.netloc, first.path) == ("https", "t.me", "/proxy")
    assert parse_qs(first.query) == {"server": ["203.0.113.10"], "port": ["443"], "secret": [SECRET]}
    assert "&amp;port=443&amp;secret=" in post  # the href is HTML-escaped
    assert len(post) <= MAX_MESSAGE_CHARS


def test_post_without_content_or_proxies() -> None:
    assert render_proxy_post(None, [], "ipoxxo") == "🔗 @ipoxxo"
    only_proxies = render_proxy_post("   ", [("1.2.3.4", 443, SECRET)], "@ipoxxo")
    assert only_proxies.startswith("📡 <b>پروکسی‌ها</b>")


def test_content_is_rebuilt_and_escaped() -> None:
    post = render_proxy_post(
        '<b>a & b</b> <script>alert(1)</script> <a href="javascript:x">bad</a> '
        '<a href="https://example.com/?a=1&b=2">ok</a> <span class="tg-spoiler">s</span>'
        '<span style="x">plain</span> 5 < 6 <unknown>t</unknown><br>line',
        [],
        "@ipoxxo",
    )
    content = post.split("\n\n🔗")[0]
    assert content == (
        "<b>a &amp; b</b> alert(1) bad "
        '<a href="https://example.com/?a=1&amp;b=2">ok</a> <span class="tg-spoiler">s</span>'
        "plain 5 &lt; 6 t\nline"
    )


def test_unclosed_and_stray_tags_are_balanced() -> None:
    out = sanitize_html("<b>bold <i>both</b> after</i> </u>end", max_html=500, max_visible=500)
    assert out == "<b>bold <i>both</i></b> after end"
    assert sanitize_html("<b><i>open", max_html=500, max_visible=500) == "<b><i>open</i></b>"


@pytest.mark.parametrize(
    "server,port,secret",
    [
        ("1.2.3.4", 0, SECRET),
        ("1.2.3.4", 65536, SECRET),
        ("1.2.3.4", True, SECRET),
        ("bad host", 443, SECRET),
        ("x", 443, SECRET),
        ("1.2.3.4", 443, "zz" + SECRET[2:]),
        ("1.2.3.4", 443, SECRET[:-1]),
        ("1.2.3.4", 443, "ee" + SECRET),  # fake-TLS needs a domain
        ("1.2.3.4&port=1", 443, SECRET),
    ],
)
def test_invalid_proxy_values_are_refused(server: str, port: int, secret: str) -> None:
    with pytest.raises(ValueError):
        proxy_link(server, port, secret)


def test_ipv6_and_dd_secret_are_encoded() -> None:
    link = proxy_link("2001:db8::1", 443, "DD" + SECRET.upper())
    assert parse_qs(urlsplit(link).query) == {
        "server": ["2001:db8::1"],
        "port": ["443"],
        "secret": ["dd" + SECRET],
    }
    assert "%3A" in link  # ':' is URL-encoded


def test_channel_tag_validation() -> None:
    assert channel_tag(" ipoxxo ") == "@ipoxxo"
    for bad in ("@ab", "@1abc", "@bad-name", "", "@" + "a" * 40):
        with pytest.raises(ValueError):
            channel_tag(bad)


def test_long_content_is_cut_to_the_4096_limit() -> None:
    proxies = [(f"10.0.0.{i}", 443, SECRET) for i in range(1, 11)]
    long_text = "<b>تیتر</b>\n" + " ".join(["واژه‌ی بلند فارسی & نماد"] * 600)
    post = render_proxy_post(long_text, proxies, "@ipoxxo")
    assert len(post) <= MAX_MESSAGE_CHARS
    assert utf16_len(visible_text(post)) <= MAX_MESSAGE_CHARS
    assert "…" in post and post.endswith("🔗 @ipoxxo")
    assert post.count("<a href=") == 10  # every proxy survives; only the content is cut
    assert "&amp;" in post and not re.search(r"&(?!amp;|lt;|gt;|quot;)", post)


def test_emoji_count_twice_toward_the_visible_limit() -> None:
    post = render_proxy_post("😀" * 5000, [], "@ipoxxo")
    assert utf16_len(visible_text(post)) <= MAX_MESSAGE_CHARS
    assert len(post) < MAX_MESSAGE_CHARS  # each emoji is 1 Python character but 2 UTF-16 units


def test_cut_inside_nested_tags_closes_them() -> None:
    post = render_proxy_post("<b><i>" + "کلمه " * 2000 + "</i></b>", [], "@ipoxxo")
    content = post.split("\n\n🔗")[0]
    assert content.startswith("<b><i>") and content.endswith("…</i></b>")
    assert len(post) <= MAX_MESSAGE_CHARS


def test_too_many_proxies_is_an_error_not_a_broken_post() -> None:
    proxies = [(f"host{i}.example.com", 443, "ee" + SECRET + "61" * 200) for i in range(12)]
    with pytest.raises(PostTooLong):
        render_proxy_post(None, proxies, "@ipoxxo")


# --- quiet windows (D21) and pacing --------------------------------------------------------------


def test_window_parsing_and_validation() -> None:
    assert QuietWindow.parse("23:00", "01:30").crosses_midnight
    for start, end in (("24:00", "01:00"), ("9:00", "10:00"), ("10:00", "10:00"), ("10:60", "11:00")):
        with pytest.raises(InvalidSchedule):
            QuietWindow.parse(start, end)
    with pytest.raises(InvalidSchedule):
        validate_windows([QuietWindow.parse("00:00", "12:00"), QuietWindow.parse("12:00", "00:00")])
    with pytest.raises(InvalidSchedule):
        validate_windows([QuietWindow.parse("01:00", "02:00")] * 25)
    assert covers_whole_day([QuietWindow.parse("06:00", "05:59"), QuietWindow.parse("05:59", "06:00")])
    with pytest.raises(InvalidSchedule):
        validate_interval(1441)
    assert validate_interval(0) == 0


def test_window_covers_including_midnight() -> None:
    w = QuietWindow.parse("23:00", "01:00")
    assert w.covers(time(23, 0)) and w.covers(time(0, 30)) and w.covers(time(0, 59, 59))
    assert not w.covers(time(1, 0)) and not w.covers(time(22, 59))
    day = QuietWindow.parse("10:00", "12:00")
    assert day.covers(time(10, 0)) and not day.covers(time(12, 0))


def test_quiet_until_in_tehran_time() -> None:
    windows = [QuietWindow.parse("20:00", "22:00")]
    assert quiet_until(tehran(2026, 11, 3, 19, 59), windows) is None
    assert quiet_until(tehran(2026, 11, 3, 21, 15), windows) == tehran(2026, 11, 3, 22, 0)
    # Asia/Tehran is UTC+03:30 (no DST since 2022): 21:15 Tehran = 17:45 UTC.
    assert tehran(2026, 6, 1, 21, 15) == datetime(2026, 6, 1, 17, 45, tzinfo=UTC)


def test_quiet_until_crossing_midnight() -> None:
    windows = [QuietWindow.parse("23:30", "02:00")]
    assert quiet_until(tehran(2026, 11, 3, 23, 45), windows) == tehran(2026, 11, 4, 2, 0)
    assert quiet_until(tehran(2026, 11, 4, 1, 10), windows) == tehran(2026, 11, 4, 2, 0)
    assert quiet_until(tehran(2026, 11, 4, 2, 0), windows) is None


def test_back_to_back_windows_are_followed_to_the_real_end() -> None:
    windows = [
        QuietWindow.parse("22:00", "23:00"),
        QuietWindow.parse("23:00", "00:30"),
        QuietWindow.parse("00:15", "03:00"),
    ]
    assert quiet_until(tehran(2026, 11, 3, 22, 10), windows) == tehran(2026, 11, 4, 3, 0)


def test_next_post_time_combines_interval_and_windows() -> None:
    now = tehran(2026, 11, 3, 19, 0)
    windows = [QuietWindow.parse("19:30", "21:00")]
    assert next_post_time(now, windows=windows, interval_min=60, last_post_at=None) is None
    assert (
        next_post_time(now, windows=windows, interval_min=60, last_post_at=now - timedelta(minutes=90))
        is None
    )
    # Interval says 19:20 -> allowed (before the window).
    assert next_post_time(
        now, windows=windows, interval_min=60, last_post_at=now - timedelta(minutes=40)
    ) == (now + timedelta(minutes=20))
    # Interval says 19:40 -> inside the window -> 21:00.
    assert next_post_time(
        now, windows=windows, interval_min=60, last_post_at=now - timedelta(minutes=20)
    ) == (tehran(2026, 11, 3, 21, 0))
    inside = tehran(2026, 11, 3, 20, 0)
    assert next_post_time(inside, windows=windows, interval_min=0, last_post_at=None) == tehran(
        2026, 11, 3, 21, 0
    )


def test_pacing_and_backoff() -> None:
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert CHANNEL_GAP == timedelta(seconds=3) and OWNER_GAP == timedelta(seconds=1)
    assert next_send_time(now, last_sent_at=None, retry_until=None, gap=CHANNEL_GAP) is None
    assert next_send_time(
        now, last_sent_at=now - timedelta(seconds=1), retry_until=None, gap=CHANNEL_GAP
    ) == (now + timedelta(seconds=2))
    assert (
        next_send_time(now, last_sent_at=now - timedelta(seconds=5), retry_until=None, gap=CHANNEL_GAP)
        is None
    )
    assert next_send_time(
        now, last_sent_at=None, retry_until=now + timedelta(seconds=35), gap=OWNER_GAP
    ) == now + timedelta(seconds=35)
    assert [backoff(n).total_seconds() for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert backoff(30) == timedelta(minutes=30)


# --- Bot API parsing (real response shapes as data) ----------------------------------------------


@pytest.mark.parametrize(
    "name,cls,status",
    [
        ("unauthorized", Unauthorized, 401),
        ("not_found", NotFound, 404),
        ("flood", RetryAfter, 429),
        ("chat_not_found", BadRequest, 400),
        ("not_modified", BadRequest, 400),
        ("kicked", Forbidden, 403),
        ("conflict", Conflict, 409),
        ("migrated", BadRequest, 400),
        ("server", ServerError, 502),
    ],
)
def test_error_shapes_map_to_typed_errors(name: str, cls: type[Exception], status: int) -> None:
    with pytest.raises(cls) as info:
        parse_api_response("sendMessage", status, body(name))
    err = info.value
    assert type(err) is cls
    assert err.error_code == status  # type: ignore[attr-defined]
    assert str(err).startswith(f"sendMessage: {status} ")


def test_retry_after_comes_from_parameters_or_text() -> None:
    with pytest.raises(RetryAfter) as info:
        parse_api_response("sendMessage", 429, body("flood"))
    assert info.value.seconds == 35
    assert retry_after_seconds({}, "Too Many Requests: retry after 7") == 7
    assert retry_after_seconds({"retry_after": True}, "Too Many Requests") == 5
    assert retry_after_seconds({}, "Too Many Requests") == 5


def test_not_modified_and_chat_not_found_flags() -> None:
    with pytest.raises(BadRequest) as info:
        parse_api_response("editMessageText", 400, body("not_modified"))
    assert info.value.not_modified and not info.value.chat_not_found
    with pytest.raises(BadRequest) as info:
        parse_api_response("getChat", 400, body("chat_not_found"))
    assert info.value.chat_not_found
    with pytest.raises(BadRequest) as info:
        parse_api_response("sendMessage", 400, body("migrated"))
    assert info.value.parameters == {"migrate_to_chat_id": -1001234567890}


@pytest.mark.parametrize("raw", [b"<html>502</html>", b"[]", b'{"result": 1}', b'{"ok": true}', b"\xff"])
def test_unreadable_answers(raw: bytes) -> None:
    with pytest.raises(InvalidResponse):
        parse_api_response("getMe", 200, raw)


def test_success_shapes() -> None:
    me = BotUser.from_result(
        parse_api_response(
            "getMe",
            200,
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "id": 8123456789,
                        "is_bot": True,
                        "first_name": "Proxy Hub",
                        "username": "proxyhub_test_bot",
                        "can_join_groups": True,
                        "can_read_all_group_messages": False,
                        "supports_inline_queries": False,
                    },
                }
            ).encode(),
        )
    )
    assert me == BotUser(id=8123456789, username="proxyhub_test_bot", first_name="Proxy Hub")
    with pytest.raises(InvalidResponse):
        BotUser.from_result({"id": 1, "is_bot": False, "first_name": "a person", "username": "x"})
    chat = ChatInfo.from_result(
        {"id": -1001234567890, "type": "channel", "title": "IPOXXO | VPN & Proxy", "username": "ipoxxo"}
    )
    assert (chat.id, chat.type, chat.username) == (-1001234567890, "channel", "ipoxxo")
    sent = SentMessage.from_result(
        {
            "message_id": 42,
            "sender_chat": {"id": -1001234567890, "title": "t", "type": "channel"},
            "chat": {"id": -1001234567890, "title": "t", "type": "channel"},
            "date": 1793734000,
            "text": "x",
        },
        "sendMessage",
    )
    assert sent == SentMessage(message_id=42, chat_id=-1001234567890)


def test_member_rights_from_real_chat_member_shapes() -> None:
    admin = {
        "status": "administrator",
        "user": {
            "id": 8123456789,
            "is_bot": True,
            "first_name": "Proxy Hub",
            "username": "proxyhub_test_bot",
        },
        "can_be_edited": False,
        "is_anonymous": False,
        "can_manage_chat": True,
        "can_delete_messages": False,
        "can_manage_video_chats": False,
        "can_restrict_members": True,
        "can_promote_members": False,
        "can_change_info": False,
        "can_invite_users": True,
        "can_post_messages": True,
        "can_edit_messages": True,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
    }
    assert CAN_REMOVE_POSTS_FLAG == "can_delete_messages"
    rights = MemberRights.from_result(admin)
    assert (rights.status, rights.can_post, rights.can_edit, rights.can_remove) == (
        "administrator",
        True,
        True,
        False,
    )
    assert MemberRights.from_result({**admin, "can_delete_messages": True}).can_remove
    assert not MemberRights.from_result({**admin, "can_edit_messages": False}).can_edit
    member = MemberRights.from_result({"status": "member", "user": admin["user"]})
    assert (member.can_post, member.can_edit) == (False, False)
    left = MemberRights.from_result({"status": "left", "user": admin["user"]})
    assert left.status == "left" and not left.can_post


def test_update_private_text_only() -> None:
    private = Update.from_result(
        {
            "update_id": 900000001,
            "message": {
                "message_id": 5,
                "from": {"id": 111222333, "is_bot": False, "first_name": "Owner", "language_code": "fa"},
                "chat": {"id": 111222333, "first_name": "Owner", "type": "private"},
                "date": 1793734000,
                "text": "/start ABCDEFGH",
                "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
            },
        }
    )
    assert private.private_text() == (111222333, "/start ABCDEFGH")
    group = Update.from_result(
        {
            "update_id": 900000002,
            "message": {
                "message_id": 6,
                "chat": {"id": -100555, "title": "g", "type": "supergroup"},
                "date": 1793734000,
                "text": "/start ABCDEFGH",
            },
        }
    )
    assert group.private_text() is None
    photo = Update.from_result(
        {
            "update_id": 3,
            "message": {"message_id": 7, "chat": {"id": 1, "type": "private"}, "date": 1, "photo": []},
        }
    )
    assert photo.private_text() is None
    assert Update.from_result({"update_id": 4, "my_chat_member": {}}).private_text() is None


# --- token handling and redaction ----------------------------------------------------------------


def test_token_shape_is_checked_before_any_request() -> None:
    assert TOKEN_RE.fullmatch(random_token())
    for bad in (
        "",
        "123:short",
        "abc:" + "x" * 35,
        "123456:" + "x" * 35 + "/getMe",
        "123456:" + "x" * 30 + "?a=b",
    ):
        with pytest.raises(InvalidToken):
            BotClient(bad)


# --- rule 6: the client cannot remove a post or ban anyone ---------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "deleteMessage",
        "deleteMessages",
        "banChatMember",
        "banChatSenderChat",
        "leaveChat",
        "setWebhook",
        "restrictChatMember",
        "unpinAllChatMessages",
        "sendmessage",  # the allowlist is exact, not case-insensitive
        "sendMessage/../deleteMessage",
        "",
    ],
)
async def test_only_allowed_methods_can_be_called(method: str) -> None:
    # The client points at a refused port: had a request been sent, this would be a NetworkError.
    async with closed_port_bot()(SecretValue(random_token())) as bot:
        with pytest.raises(ValueError, match="not allowed"):
            await bot._call(method, {"chat_id": -1001234567890, "message_id": 1})


def test_the_allowlist_is_exactly_the_typed_methods() -> None:
    assert (
        frozenset({"getMe", "getUpdates", "sendMessage", "editMessageText", "getChat", "getChatMember"})
        == ALLOWED_METHODS
    )
    assert not hasattr(BotClient, "call")  # no public "send any method" entry point
    public = {name for name in vars(BotClient) if not name.startswith("_")}
    assert public == {
        "aclose",
        "get_me",
        "get_updates",
        "send_message",
        "edit_message_text",
        "get_chat",
        "get_chat_member",
    }


def test_redact_hides_tokens_in_urls_and_bare() -> None:
    token = random_token()
    bot_id, secret = token.split(":")
    for raw in (
        f"POST https://api.telegram.org/bot{token}/sendMessage",
        f"https://api.telegram.org/bot{bot_id}%3A{secret}/getMe",
        f"token is {token}.",
    ):
        cleaned = redact(raw)
        assert token not in cleaned and secret not in cleaned
    assert redact("retry after 35 at 12:30") == "retry after 35 at 12:30"
    assert repr(BotClient(token)) == "BotClient(token=***)"


# --- pairing codes -------------------------------------------------------------------------------


def test_pairing_codes() -> None:
    codes = {pairing.new_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert len(code) == 8 and set(code) <= set(pairing.ALPHABET)
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code)  # valid in a t.me ?start= deep link
    assert pairing.normalize(" abcdefgh ") == "ABCDEFGH"
    assert pairing.normalize("ABCDEFG0") is None  # 0 is not in the alphabet (looks like O)
    assert pairing.normalize("ABCDEFGHJ") is None
    assert pairing.start_argument("/start ABCDEFGH") == "ABCDEFGH"
    assert pairing.start_argument("/start@proxyhub_test_bot  abcdefgh ") == "abcdefgh"
    assert pairing.start_argument("/start") is None
    assert pairing.start_argument("hello /start ABCDEFGH") is None
    assert pairing.code_hash("ABCDEFGH") != pairing.code_hash("ABCDEFGJ")
    assert re.fullmatch(r"[0-9a-f]{64}", pairing.code_hash("ABCDEFGH"))


# --- deploy files agree with the code ------------------------------------------------------------


def test_alertmanager_posts_to_the_internal_route_on_a_shared_network() -> None:
    from proxyhub.api.routes_telegram import internal

    assert {getattr(r, "path", "") for r in internal.routes} == {"/internal/alerts/alertmanager"}
    entry = (REPO / "deploy/monitoring/alertmanager/entrypoint.sh").read_text(encoding="utf-8")
    assert "- url: http://api:8000/internal/alerts/alertmanager" in entry
    assert re.search(
        r"webhook_configs:\n\s+- url: http://api:8000/internal/alerts/alertmanager\n\s+send_resolved: true",
        entry,
    )
    compose = yaml.safe_load((REPO / "deploy/docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert "backend" in services["alertmanager"]["networks"]
    assert "backend" in services["api"]["networks"]
    caddy = (REPO / "deploy/Caddyfile").read_text(encoding="utf-8")
    assert "/internal/*" in caddy


def test_worker_runs_the_telegram_loops_without_reading_the_key_early() -> None:
    from proxyhub.reconcilers.telegram import ApiWatchdogReconciler
    from proxyhub.worker import reconcilers

    loops = {r.name: r for r in reconcilers()}  # no master key file here: nothing reads it yet
    assert {"telegram_updates", "telegram_outbox", "telegram_api_watchdog"} <= set(loops)
    watchdog = loops["telegram_api_watchdog"]
    assert isinstance(watchdog, ApiWatchdogReconciler)
    assert (watchdog.interval_seconds, watchdog.url, watchdog.failures_to_alert) == (
        60,
        "http://api:8000/livez",
        3,
    )
