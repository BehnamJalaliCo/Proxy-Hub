"""Analytics parsers and pure rules, without a database.

Hetzner and Telegram answers are their published JSON shapes (tests/test_analytics_support.py) turned
into objects by the REAL hcloud / Telethon classes; failures are a real refused local port (D10).
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from telethon.tl import functions, types
from test_analytics_support import (
    CHANNEL_MAIN,
    INVITE_LINK_RESULT,
    TB,
    ClosedPortReader,
    bound_server,
    chat_member_update,
    metrics_json,
    metrics_object,
    server_json,
)
from test_telegram_support import closed_port_bot, free_closed_port, random_token

from proxyhub.accounts.hetzner_io import HetznerFailure, call_hetzner
from proxyhub.agent_api.reports import ReportRejected, parse_report
from proxyhub.analytics import hetzner_usage, sampling
from proxyhub.analytics.members import parse_member_change
from proxyhub.analytics.messages import cpu_alert_fa, traffic_alert_fa
from proxyhub.analytics.settings import DEFAULTS, AnalyticsSettings, parse_settings
from proxyhub.analytics.usage import Bucket, UsageAgg, fold_small
from proxyhub.mtproxy import channel_stats
from proxyhub.mtproxy.userclient import (
    ALLOWED_REQUESTS,
    CHANNEL_STATS_REQUESTS,
    ChannelScope,
    RequestNotAllowed,
    check_request,
)
from proxyhub.telegram.client import InvalidResponse, InviteLink, NetworkError
from proxyhub.vault import SecretValue

# --- Hetzner: traffic counters and metrics -----------------------------------------------------------


def test_server_usage_parses_from_the_real_hcloud_object() -> None:
    u = hetzner_usage.usage_from_server(
        bound_server(server_json(4711, out_bytes=5 * TB, in_bytes=TB, cores=2))
    )
    assert u.hetzner_id == 4711 and u.location == "fsn1" and u.cores == 2
    assert (u.out_bytes, u.in_bytes, u.included_bytes) == (5 * TB, TB, 20 * TB)
    assert u.traffic_price_per_tb_eur == Decimal("1.0000000000")
    assert u.labels["managed-by"] == "proxy-hub"
    # The parsed usage holds no address of any kind (only our own server has one in the JSON).
    assert "203.0.113" not in repr(u)


def test_null_counters_stay_unknown() -> None:
    u = hetzner_usage.usage_from_server(
        bound_server(server_json(1, out_bytes=None, in_bytes=None, included=None))
    )
    assert (u.out_bytes, u.in_bytes, u.included_bytes) == (None, None, None)


def test_traffic_price_is_taken_for_the_servers_own_location_only() -> None:
    data = server_json(2, location="hel1")
    data["server_type"]["prices"][0]["location"] = "fsn1"  # a price for another location only
    assert hetzner_usage.usage_from_server(bound_server(data)).traffic_price_per_tb_eur is None


def test_cpu_is_divided_by_the_vcpu_count() -> None:
    # Hetzner: 100 = one full vCPU. On a 2-vCPU server, 150 means 75 % of the whole server.
    m = metrics_object(metrics_json([150.0, 50.0, 100.0], [1000.0, 3000.0, 2000.0], [10.0, 20.0, 30.0]))
    load = hetzner_usage.load_from_metrics(m, cores=2)
    assert load.cpu_avg == pytest.approx(50.0) and load.cpu_max == pytest.approx(75.0)
    assert load.net_out_bps_avg == pytest.approx(2000.0) and load.net_in_bps_avg == pytest.approx(20.0)
    assert load.points == 3 and load.step_s == 60


def test_metrics_with_gaps_and_odd_values() -> None:
    data = metrics_json([90.0], [5.0], [1.0])
    data["metrics"]["time_series"]["cpu"]["values"] += [
        [1793800900, "NaN"],
        [1793800960, "x"],
        [1793801020, "-1"],
    ]
    load = hetzner_usage.load_from_metrics(metrics_object(data), cores=None)
    assert load.cpu_avg == pytest.approx(90.0)  # unknown cores: divided by 1, capped at 100
    empty = hetzner_usage.load_from_metrics(metrics_object(metrics_json([], [], [])), cores=4)
    assert empty.cpu_avg is None and empty.cpu_max is None and empty.net_out_bps_avg is None


async def test_closed_port_is_an_unreachable_failure_and_names_no_token() -> None:
    token = secrets.token_hex(32)
    reader = ClosedPortReader(token, free_closed_port())
    with pytest.raises(HetznerFailure) as exc:
        await call_hetzner(lambda: hetzner_usage.read_managed_usage(reader), limit_s=20)
    assert exc.value.kind == "unreachable"
    assert token not in str(exc.value) and token not in repr(exc.value)
    now = datetime.now(UTC)
    with pytest.raises(HetznerFailure):
        await call_hetzner(
            lambda: hetzner_usage.read_load(reader, 1, now - timedelta(minutes=15), now, 2), limit_s=20
        )


def test_reads_are_get_only() -> None:
    from pathlib import Path

    source = Path(hetzner_usage.__file__).read_text(encoding="utf-8")
    for call in ("get_all(", "get_metrics("):
        assert call in source
    for forbidden in (".create(", ".delete(", ".update(", ".rebuild(", ".power_", ".request(", "change_"):
        assert forbidden not in source, forbidden


# --- traffic counters -> deltas, signals -----------------------------------------------------------------


def test_counter_delta_rules() -> None:
    assert sampling.counter_delta(500, 200, first_counts=False) == 300
    assert sampling.counter_delta(80, 200, first_counts=False) == 80  # reset by Hetzner: since the reset
    assert sampling.counter_delta(700, None, first_counts=True) == 700  # a new node: from zero
    assert sampling.counter_delta(700, None, first_counts=False) == 0  # an older node: baseline only
    assert sampling.counter_delta(None, 200, first_counts=True) is None


def test_classify_and_samples_needed() -> None:
    s = AnalyticsSettings()
    assert sampling.classify(None, None, s) == "ok"
    assert sampling.classify(79.9, 79.9, s) == "ok"
    assert sampling.classify(80.0, 10.0, s) == "near_traffic_limit"
    assert sampling.classify(10.0, 80.0, s) == "hot"
    assert sampling.classify(95.0, 99.0, s) == "near_traffic_limit"  # traffic costs money: it wins
    assert (
        sampling.samples_needed(60) == 2
        and sampling.samples_needed(15) == 1
        and sampling.samples_needed(180) == 6
    )
    assert sampling.traffic_pct(5 * TB, 20 * TB) == pytest.approx(25.0)
    assert sampling.traffic_pct(5, 0) is None and sampling.traffic_pct(None, 20) is None


def test_settings_defaults_and_validation() -> None:
    assert DEFAULTS.model_dump() == {
        "traffic_warn_pct": 80,
        "cpu_warn_pct": 80,
        "cpu_window_min": 60,
        "alerts_enabled": True,
    }
    for bad in (
        {"traffic_warn_pct": 0, "cpu_warn_pct": 80, "cpu_window_min": 60, "alerts_enabled": True},
        {"traffic_warn_pct": 80, "cpu_warn_pct": 101, "cpu_window_min": 60, "alerts_enabled": True},
        {"traffic_warn_pct": 80, "cpu_warn_pct": 80, "cpu_window_min": 5, "alerts_enabled": True},
        {"traffic_warn_pct": "80", "cpu_warn_pct": 80, "cpu_window_min": 60, "alerts_enabled": True},
        {"traffic_warn_pct": 80, "cpu_warn_pct": 80, "cpu_window_min": 60, "alerts_enabled": True, "x": 1},
        {"traffic_warn_pct": 80},  # every field, always
        [80, 80, 60, True],
    ):
        with pytest.raises(ValueError):
            parse_settings(bad)


def test_alert_texts_are_persian_and_escape_names() -> None:
    t = traffic_alert_fa("<node&1>", 85.4, 80, 17.08, 20.0)
    assert "&lt;node&amp;1&gt;" in t and "۸۵٪" in t and "۱۷٫۰۸" not in t and "۱۷.۰۸" in t and "ترابایت" in t
    c = cpu_alert_fa("n1", 91.2, 80, 60)
    assert "۹۱٪" in c and "۶۰ دقیقه" in c


# --- chat_member -> join/leave ----------------------------------------------------------------------------


def _change(**kw: Any) -> Any:
    return parse_member_change(
        chat_member_update(1, chat_id=CHANNEL_MAIN, user_id=777, date=1793800000, **kw)
    )


def test_join_via_a_named_link() -> None:
    c = _change(old="left", new="member", invite_link="https://t.me/+abc", link_name="campaign-x")
    assert (c.kind, c.source, c.link_name, c.link_url, c.user_id) == (
        "join",
        "link",
        "campaign-x",
        "https://t.me/+abc",
        777,
    )
    assert c.at == datetime.fromtimestamp(1793800000, UTC) and c.chat_id == CHANNEL_MAIN


def test_join_sources() -> None:
    assert _change(old="left", new="member", invite_link="https://t.me/+abc").source == "link"
    assert _change(old="left", new="member", invite_link="https://t.me/+abc").link_name is None
    assert _change(old="left", new="member", via_folder=True).source == "folder"
    assert _change(old="kicked", new="member").source == "other"
    assert _change(old="left", new="restricted", new_is_member=True).kind == "join"


def test_leave_and_non_changes() -> None:
    leave = _change(old="member", new="left", invite_link="https://t.me/+abc", link_name="x")
    assert leave.kind == "leave" and leave.source == "other" and leave.link_name is None
    assert _change(old="member", new="kicked").kind == "leave"
    assert _change(old="member", new="administrator") is None  # a promotion
    assert _change(old="left", new="kicked") is None  # banned while not a member
    assert _change(old="left", new="restricted", new_is_member=False) is None
    assert _change(old="left", new="member", is_bot=True) is None  # bots are not audience
    assert parse_member_change({"update_id": 1, "message": {}}) is None
    assert parse_member_change({"update_id": 1, "chat_member": {"chat": {"id": "x"}}}) is None


def test_a_member_change_keeps_only_the_allowed_facts() -> None:
    c = _change(old="left", new="member", first_name="Secret Name", username="secret_handle", link_name="n")
    assert set(vars(c)) == {"chat_id", "user_id", "at", "kind", "source", "link_name", "link_url"}
    assert "Secret" not in repr(c) and "secret_handle" not in repr(c)


# --- Bot API additions ----------------------------------------------------------------------------------


def test_invite_link_result_shape() -> None:
    link = InviteLink.from_result(INVITE_LINK_RESULT)
    assert link.invite_link.startswith("https://t.me/+") and link.name == "کانال الف - آبان"
    assert not link.creates_join_request and not link.is_revoked
    with pytest.raises(InvalidResponse):
        InviteLink.from_result({"name": "x"})
    with pytest.raises(InvalidResponse):
        InviteLink.from_result({"invite_link": "javascript:alert(1)"})


async def test_new_bot_methods_fail_cleanly_on_a_refused_port() -> None:
    token = random_token()
    async with closed_port_bot()(SecretValue(token)) as bot:
        with pytest.raises(NetworkError) as exc:
            await bot.get_chat_member_count(CHANNEL_MAIN)
        assert token not in str(exc.value) and exc.value.maybe_sent is False
        with pytest.raises(NetworkError):
            await bot.create_chat_invite_link(CHANNEL_MAIN, name="campaign")
        with pytest.raises(ValueError, match="1 to 32"):
            await bot.create_chat_invite_link(CHANNEL_MAIN, name="x" * 33)


# --- usage_agg (D39) ------------------------------------------------------------------------------------


def usage_block(**over: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "boot_id": "0123456789abcdef",
        "day": datetime.now(UTC).date().isoformat(),
        "unique_users": 1250,
        "hours": [
            {
                "hour": 9,
                "kind": "mtproto",
                "ref": "u_main1",
                "bytes_up": 10**8,
                "bytes_down": 9 * 10**9,
                "conns_max": 420,
            },
            {
                "hour": 9,
                "kind": "reality",
                "ref": "rl_de1",
                "bytes_up": 10**8,
                "bytes_down": 4 * 10**9,
                "conns_max": 80,
            },
        ],
        "by_country": [
            {"key": "IR", "users": 1200, "bytes": 10**10},
            {"key": "DE", "users": 2, "bytes": 10**6},
        ],
        "by_region": [
            {"key": "Tehran", "users": 600, "bytes": 5 * 10**9},
            {"key": "Razavi Khorasan", "users": 1, "bytes": 1},
        ],
        "by_asn": [
            {"key": "AS197207", "users": 500, "bytes": 4 * 10**9},
            {"key": "AS44244", "users": 400, "bytes": 3 * 10**9},
        ],
        "per_user": {"users": 1250, "p50_mb": 35.5, "p90_mb": 410.0, "p99_mb": 2900.0},
    }
    block.update(over)
    return block


def report_with(block: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": 1,
        "agent_version": "0.3.0",
        "applied_etag": None,
        "apply_ok": True,
        "apply_error": None,
        "telemt": {
            "up": True,
            "version": "3.5.16",
            "uptime_s": 10.0,
            "connections_total": 1,
            "connections_bad_total": 0,
            "handshake_timeouts_total": 0,
            "users": [],
        },
        "system": {
            "uptime_s": 1.0,
            "load1": 0.1,
            "mem_total_bytes": 1,
            "mem_available_bytes": 1,
            "cpu_count": 2,
        },
    }
    if block is not None:
        body["usage_agg"] = block
    return body


def test_a_report_without_usage_agg_still_parses() -> None:
    assert parse_report(report_with(None)).usage_agg is None


def test_usage_agg_parses_and_folds_small_buckets() -> None:
    agg = parse_report(report_with(usage_block())).usage_agg
    assert isinstance(agg, UsageAgg)
    assert fold_small(agg.by_country) == [("IR", 1200, 10**10), ("other", 2, 10**6)]
    assert fold_small(agg.by_region) == [("Tehran", 600, 5 * 10**9), ("other", 1, 1)]
    assert fold_small([Bucket(key="other", users=7, bytes=5), Bucket(key="FR", users=1, bytes=1)]) == [
        ("other", 8, 6)
    ]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("by_country", [{"key": "IR", "users": 5, "bytes": 1, "ip": "x"}], "schema"),
        ("by_country", [{"key": "Iran", "users": 5, "bytes": 1}], "schema"),  # not a country code
        ("by_asn", [{"key": "197207", "users": 5, "bytes": 1}], "schema"),
        ("by_region", [{"key": "Tehran 1", "users": 5, "bytes": 1}], "schema"),  # digits never allowed
        ("by_region", [{"key": "10.0.0.1", "users": 5, "bytes": 1}], "ip_like_value"),
        ("by_asn", [{"key": "2001:db8::1", "users": 5, "bytes": 1}], "ip_like_value"),
        (
            "hours",
            [{"hour": 24, "kind": "mtproto", "ref": "u1", "bytes_up": 1, "bytes_down": 1, "conns_max": 1}],
            "schema",
        ),
        (
            "hours",
            [{"hour": 1, "kind": "vpn", "ref": "u1", "bytes_up": 1, "bytes_down": 1, "conns_max": 1}],
            "schema",
        ),
        ("day", "2026-13-45", "schema"),
        ("boot_id", "XYZ", "schema"),
        ("per_user", {"users": 9, "p50_mb": 5.0, "p90_mb": 4.0, "p99_mb": 9.0}, "schema"),
        ("unique_users", "12", "schema"),
        ("client_ips", ["1.2.3.4"], "ip_like_value"),
        ("client_hashes", ["ab"], "schema"),
    ],
)
def test_usage_agg_rejects(field: str, value: Any, code: str) -> None:
    with pytest.raises(ReportRejected) as exc:
        parse_report(report_with(usage_block(**{field: value})))
    assert exc.value.code == code


def test_duplicate_buckets_and_hours_are_refused() -> None:
    with pytest.raises(ReportRejected):
        parse_report(report_with(usage_block(by_country=[{"key": "IR", "users": 5, "bytes": 1}] * 2)))
    row = {"hour": 1, "kind": "mtproto", "ref": "u1", "bytes_up": 1, "bytes_down": 1, "conns_max": 1}
    with pytest.raises(ReportRejected):
        parse_report(report_with(usage_block(hours=[row, row])))


# --- channel statistics via the operator account (D38) ---------------------------------------------------

MAIN_ID = channel_stats.bare_channel_id(CHANNEL_MAIN)
assert MAIN_ID is not None
OTHER_ID = 987654321


def scope() -> ChannelScope:
    return ChannelScope(channel_ids=frozenset({MAIN_ID}), usernames=frozenset({"ipoxxo"}))


def test_bare_channel_id() -> None:
    assert channel_stats.bare_channel_id(-1001234567890) == 1234567890
    assert channel_stats.bare_channel_id(-123) is None and channel_stats.bare_channel_id(5) is None


def test_exactly_four_channel_requests_were_added() -> None:
    assert CHANNEL_STATS_REQUESTS == {
        functions.channels.GetFullChannelRequest,
        functions.stats.GetBroadcastStatsRequest,
        functions.stats.LoadAsyncGraphRequest,
        functions.messages.GetMessagesViewsRequest,
    }
    # 9 for the login and the bot chat's helpers, these 4, and the operator poster's 2 reads (D38:
    # the account's own rights, the owner's emoji pack; tests/test_operator_poster_allowlist.py).
    assert CHANNEL_STATS_REQUESTS <= ALLOWED_REQUESTS and len(ALLOWED_REQUESTS) == 15


def _ours() -> types.InputChannel:
    return types.InputChannel(channel_id=MAIN_ID, access_hash=42)


def _other() -> types.InputChannel:
    return types.InputChannel(channel_id=OTHER_ID, access_hash=42)


def test_get_full_channel_only_for_our_channels() -> None:
    check_request(functions.channels.GetFullChannelRequest(_ours()), scope())
    for s in (scope(), None):
        with pytest.raises(RequestNotAllowed):
            check_request(functions.channels.GetFullChannelRequest(_other()), s)
    with pytest.raises(RequestNotAllowed):  # no scope: nothing about any channel
        check_request(functions.channels.GetFullChannelRequest(_ours()))


def test_broadcast_stats_only_for_our_channels() -> None:
    check_request(functions.stats.GetBroadcastStatsRequest(channel=_ours(), dark=False), scope())
    with pytest.raises(RequestNotAllowed):
        check_request(functions.stats.GetBroadcastStatsRequest(channel=_other()), scope())
    with pytest.raises(RequestNotAllowed):
        check_request(functions.stats.GetBroadcastStatsRequest(channel=_ours()))


def test_load_async_graph_only_for_tokens_from_our_answer() -> None:
    s = scope()
    with pytest.raises(RequestNotAllowed):
        check_request(functions.stats.LoadAsyncGraphRequest(token="tok-1"), s)
    s.graph_tokens.add("tok-1")
    check_request(functions.stats.LoadAsyncGraphRequest(token="tok-1"), s)
    with pytest.raises(RequestNotAllowed):  # zooming into another range is not needed
        check_request(functions.stats.LoadAsyncGraphRequest(token="tok-1", x=5), s)
    with pytest.raises(RequestNotAllowed):
        check_request(functions.stats.LoadAsyncGraphRequest(token="tok-1"))


def test_messages_views_only_for_our_channels_and_never_counting() -> None:
    peer = types.InputPeerChannel(channel_id=MAIN_ID, access_hash=42)
    check_request(functions.messages.GetMessagesViewsRequest(peer=peer, id=[1, 2], increment=False), scope())
    for bad in (
        functions.messages.GetMessagesViewsRequest(peer=peer, id=[1], increment=True),
        functions.messages.GetMessagesViewsRequest(
            peer=types.InputPeerChannel(channel_id=OTHER_ID, access_hash=1), id=[1], increment=False
        ),
        functions.messages.GetMessagesViewsRequest(peer=types.InputPeerSelf(), id=[1], increment=False),
        functions.messages.GetMessagesViewsRequest(peer=peer, id=[], increment=False),
        functions.messages.GetMessagesViewsRequest(peer=peer, id=list(range(1, 102)), increment=False),
    ):
        with pytest.raises(RequestNotAllowed):
            check_request(bad, scope())


def test_resolve_username_only_mtproxybot_and_our_channels() -> None:
    check_request(functions.contacts.ResolveUsernameRequest(username="MTProxybot"))
    check_request(functions.contacts.ResolveUsernameRequest(username="ipoxxo"), scope())
    for name, s in (("ipoxxo", None), ("durov", scope()), ("telegram", scope())):
        with pytest.raises(RequestNotAllowed):
            check_request(functions.contacts.ResolveUsernameRequest(username=name), s)


def test_deletes_and_sends_stay_forbidden_even_with_a_scope() -> None:
    peer = types.InputPeerChannel(channel_id=MAIN_ID, access_hash=42)
    for request in (
        functions.channels.DeleteMessagesRequest(channel=_ours(), id=[1]),
        functions.messages.DeleteMessagesRequest(id=[1]),
        functions.channels.DeleteChannelRequest(channel=_ours()),
        functions.messages.SendMessageRequest(peer=peer, message="x"),
        functions.messages.GetHistoryRequest(peer, 0, None, 0, 10, 0, 0, 0),
        functions.channels.GetParticipantsRequest(_ours(), types.ChannelParticipantsRecent(), 0, 10, 0),
        functions.stats.GetMessageStatsRequest(channel=_ours(), msg_id=1),
        functions.auth.ExportAuthorizationRequest(dc_id=4),
    ):
        with pytest.raises(RequestNotAllowed):
            check_request(request, scope())


GRAPH_FOLLOWERS = json.dumps(
    {
        "columns": [["x", 1793404800000, 1793491200000, 1793577600000], ["y0", 1200, 1240, 1310]],
        "types": {"y0": "line", "x": "x"},
        "names": {"y0": "Followers"},
        "colors": {"y0": "#3497ED"},
        "hidden": [],
    }
)
GRAPH_LANGUAGES = json.dumps(
    {
        "columns": [["x", 1793404800000, 1793491200000], ["y0", 900, 950], ["y1", 250, 260], ["y2", 50, 55]],
        "types": {"y0": "area", "y1": "area", "y2": "area", "x": "x"},
        "names": {"y0": "Persian", "y1": "English", "y2": "Russian"},
        "percentage": True,
        "stacked": True,
    }
)


def broadcast_stats() -> Any:
    """A real Telethon ``stats.BroadcastStats`` in the documented shape (core.telegram.org/api/stats)."""
    num = types.StatsAbsValueAndPrev
    graph = types.StatsGraph
    return types.stats.BroadcastStats(
        period=types.StatsDateRangeDays(
            min_date=datetime(2026, 10, 5, tzinfo=UTC), max_date=datetime(2026, 11, 4, tzinfo=UTC)
        ),
        followers=num(current=1310, previous=1180),
        views_per_post=num(current=820, previous=700),
        shares_per_post=num(current=31, previous=25),
        reactions_per_post=num(current=12, previous=9),
        views_per_story=num(current=0, previous=0),
        shares_per_story=num(current=0, previous=0),
        reactions_per_story=num(current=0, previous=0),
        enabled_notifications=types.StatsPercentValue(part=393, total=1310),
        growth_graph=graph(json=types.DataJSON(data=GRAPH_FOLLOWERS)),
        followers_graph=graph(json=types.DataJSON(data=GRAPH_FOLLOWERS)),
        mute_graph=types.StatsGraphError(error="not enough data"),
        top_hours_graph=types.StatsGraphAsync(token="tok-hours"),
        interactions_graph=types.StatsGraphAsync(token="tok-interactions"),
        iv_interactions_graph=types.StatsGraphError(error="no instant view"),
        views_by_source_graph=types.StatsGraphAsync(token="tok-sources"),
        new_followers_by_source_graph=types.StatsGraphAsync(token="tok-new"),
        languages_graph=graph(json=types.DataJSON(data=GRAPH_LANGUAGES)),
        reactions_by_emotion_graph=types.StatsGraphError(error="none"),
        story_interactions_graph=types.StatsGraphError(error="none"),
        story_reactions_by_emotion_graph=types.StatsGraphError(error="none"),
        recent_posts_interactions=[
            types.PostInteractionCountersMessage(msg_id=101, views=900, forwards=40, reactions=15),
            types.PostInteractionCountersStory(story_id=3, views=10, forwards=0, reactions=0),
        ],
    )


def test_broadcast_stats_parse_into_a_snapshot() -> None:
    snap = channel_stats.Snapshot(role="main", chat_id=CHANNEL_MAIN)
    pending = channel_stats.parse_broadcast_stats(broadcast_stats(), snap)
    assert snap.numbers["followers"] == 1310 and snap.numbers["followers_prev"] == 1180
    assert snap.numbers["views_per_post"] == 820 and snap.numbers["shares_per_post"] == 31
    assert snap.numbers["notifications_pct"] == pytest.approx(30.0)
    assert snap.period_start == datetime(2026, 10, 5, tzinfo=UTC)
    assert set(snap.graphs) == {"followers", "growth", "languages"}
    assert snap.graphs["followers"]["x"][0] == 1793404800000
    assert snap.graphs["followers"]["series"] == [
        {"key": "y0", "name": "Followers", "type": "line", "values": [1200.0, 1240.0, 1310.0]}
    ]
    langs = snap.graphs["languages"]
    assert [s["name"] for s in langs["series"]] == ["Persian", "English", "Russian"] and langs["percentage"]
    assert sorted(pending) == [
        ("interactions", "tok-interactions"),
        ("new_followers_by_source", "tok-new"),
        ("top_hours", "tok-hours"),
        ("views_by_source", "tok-sources"),
    ]
    assert snap.posts == [(101, 900, 40, 15)]  # stories are not posts


def test_graph_parser_is_bounded_and_rejects_junk() -> None:
    assert channel_stats.parse_graph_json("not json") is None
    assert channel_stats.parse_graph_json("[]") is None
    big = json.dumps({"columns": [["x", *range(2000)], *[[f"y{i}", *range(2000)] for i in range(40)]]})
    parsed = channel_stats.parse_graph_json(big)
    assert parsed is not None and len(parsed["x"]) == channel_stats.MAX_POINTS
    assert len(parsed["series"]) == channel_stats.MAX_SERIES
    assert channel_stats.parse_graph(types.StatsGraphError(error="e" * 500)) == {"error": "e" * 64}
    assert channel_stats.parse_graph(None) is None
