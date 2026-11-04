"""Analytics on a REAL PostgreSQL: samples and deltas, signals and alerts, chat_member events (D7 fields
only), usage counters (D39), channel statistics snapshots (D38), the report numbers, the loops'
failure paths (a refused local port), and the migration."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_analytics_support import (
    CHANNEL_MAIN,
    CHANNEL_TEST,
    STRANGER_CHANNEL,
    TB,
    ClosedPortReader,
    add_account,
    add_node,
    add_offer,
    bound_server,
    chat_member_update,
    clean_analytics_tables,
    metrics_json,
    metrics_object,
    random_link,
    server_json,
)
from test_analytics_units import broadcast_stats, usage_block
from test_fleet_support import assert_absent_from_database, clean_fleet_tables, make_vault
from test_mtproxy_support import clean as clean_mtproxy
from test_mtproxy_support import seed_connected
from test_telegram_support import clean_telegram_tables, closed_port_bot, free_closed_port, random_token

from proxyhub.analytics import hetzner_usage, report, sampling
from proxyhub.analytics.settings import AnalyticsSettings, save_settings
from proxyhub.analytics.usage import UsageAgg, store_usage_agg
from proxyhub.fleet.settings_store import save_fleet_settings
from proxyhub.mtproxy import channel_stats
from proxyhub.mtproxy.userclient import ClientOptions, Endpoint
from proxyhub.reconcilers.analytics import STATUS_KEY, AnalyticsReconciler, ChannelStatsReconciler
from proxyhub.reconcilers.telegram import handle_update
from proxyhub.settings_schema import FleetSettings
from proxyhub.telegram import store
from proxyhub.telegram.client import Update
from proxyhub.vault import SecretValue, Vault, hcloud_secret_name

pytestmark = pytest.mark.db

OWNER_CHAT = 111222333


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await _clean(eng)
    yield eng
    await _clean(eng)
    await eng.dispose()


async def _clean(eng: AsyncEngine) -> None:
    await clean_analytics_tables(eng)
    await clean_telegram_tables(eng)
    await clean_mtproxy(eng)
    await clean_fleet_tables(eng)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


async def sampled_node(engine: AsyncEngine, **kw: Any) -> sampling.SampledNode:
    async with engine.connect() as conn:
        nodes = await sampling.sampleable_nodes(conn)
    match = [n for n in nodes if all(getattr(n, k) == v for k, v in kw.items())]
    assert len(match) == 1
    return match[0]


async def store_sample(
    engine: AsyncEngine,
    node: sampling.SampledNode,
    at: datetime,
    *,
    out_bytes: int,
    cpu: list[float] | None = None,
    included: int = 20 * TB,
) -> None:
    usage = hetzner_usage.usage_from_server(
        bound_server(server_json(node.hetzner_id, out_bytes=out_bytes, included=included))
    )
    load = hetzner_usage.load_from_metrics(
        metrics_object(metrics_json(cpu or [20.0], [1e6], [1e5])), usage.cores
    )
    async with engine.begin() as conn:
        prev = (await sampling.last_counters(conn, [node.node_id])).get(node.node_id)
        await sampling.store_node_sample(conn, node, usage, load, at, prev)


async def channels(engine: AsyncEngine, *, owner: bool = False) -> None:
    async with engine.begin() as conn:
        await store.save_channel(
            conn, "main", {"chat_id": CHANNEL_MAIN, "username": "@ipoxxo", "title": "IPOXXO"}
        )
        await store.save_channel(
            conn, "test", {"chat_id": CHANNEL_TEST, "username": "-1009876543210", "title": "T"}
        )
        if owner:
            await store.set_owner_chat(conn, OWNER_CHAT, datetime.now(UTC))


# --- samples --------------------------------------------------------------------------------------


async def test_deltas_for_new_and_older_nodes_and_counter_resets(engine: AsyncEngine) -> None:
    acc = await add_account(engine)
    now = sampling.minute(datetime.now(UTC))
    await add_node(engine, acc, 101, name="new-node", created_at=now - timedelta(minutes=10))
    await add_node(engine, acc, 102, name="old-node", created_at=now - timedelta(days=12))
    new, old = await sampled_node(engine, hetzner_id=101), await sampled_node(engine, hetzner_id=102)
    await store_sample(engine, new, now, out_bytes=5 * 10**9)
    await store_sample(engine, old, now, out_bytes=4 * TB)
    await store_sample(engine, new, now + timedelta(minutes=15), out_bytes=8 * 10**9)
    await store_sample(engine, old, now + timedelta(minutes=15), out_bytes=10**9)  # Hetzner reset its counter
    got = await rows(
        engine,
        "SELECT node_name, out_bytes, out_delta_bytes FROM analytics_node_samples ORDER BY node_name, at",
    )
    assert [(r.node_name, r.out_delta_bytes) for r in got] == [
        ("new-node", 5 * 10**9),  # a new node's counter started at zero
        ("new-node", 3 * 10**9),
        ("old-node", 0),  # an older node's first sample is only a baseline
        ("old-node", 10**9),
    ]
    # The same node and minute twice is stored once.
    await store_sample(engine, new, now, out_bytes=9 * 10**9)
    assert len(await rows(engine, "SELECT 1 FROM analytics_node_samples")) == 4


async def test_only_live_nodes_with_a_hetzner_id_in_an_observed_account_are_sampled(
    engine: AsyncEngine,
) -> None:
    live = await add_account(engine)
    locked = await add_account(engine, state="locked")
    await add_node(engine, live, 201, name="a")
    await add_node(engine, live, None, name="b-requested", state="requested")
    await add_node(engine, live, 203, name="c-deleted", state="deleted", deleted_at=datetime.now(UTC))
    await add_node(engine, locked, 204, name="d-locked-account")
    async with engine.connect() as conn:
        names = [n.name for n in await sampling.sampleable_nodes(conn)]
    assert names == ["a"]


# --- signals and alerts ---------------------------------------------------------------------------


async def hot_node(
    engine: AsyncEngine, now: datetime, *, out_bytes: int = 17 * TB, cpu: float = 90.0
) -> sampling.SampledNode:
    acc = await add_account(engine)
    await add_node(engine, acc, 301, name="fsn-1", created_at=now - timedelta(days=20))
    node = await sampled_node(engine, hetzner_id=301)
    for i in range(4):
        await store_sample(
            engine, node, now - timedelta(minutes=45 - 15 * i), out_bytes=out_bytes + i, cpu=[cpu * 2]
        )
    return node


async def test_signals_and_one_alert_per_node_limit_and_day(engine: AsyncEngine) -> None:
    now = sampling.minute(datetime.now(UTC))
    await channels(engine, owner=True)
    node = await hot_node(engine, now)
    async with engine.connect() as conn:
        (sig,) = await sampling.capacity_signals(conn, now=now)
    assert sig.signal == "near_traffic_limit" and sig.fresh
    assert sig.traffic_pct == pytest.approx(85.0) and sig.cpu_window_avg == pytest.approx(90.0)
    async with engine.begin() as conn:
        raised = await sampling.raise_threshold_alerts(conn, now)
    assert sorted(a.kind for a in raised) == ["cpu", "traffic"] and all(a.outbox_id for a in raised)
    jobs = await rows(engine, "SELECT kind, payload FROM outbox ORDER BY id")
    assert [j.kind for j in jobs] == ["alert", "alert"]
    texts = " ".join(j.payload["text"] for j in jobs)
    assert "fsn-1" in texts and "۸۵٪" in texts and "۹۰٪" in texts
    async with engine.begin() as conn:
        assert await sampling.raise_threshold_alerts(conn, now + timedelta(minutes=15)) == []
    logged = await rows(
        engine, "SELECT action, target FROM actions_log WHERE actor = 'analytics' ORDER BY id"
    )
    assert sorted(r.action for r in logged) == ["analytics.cpu_warning", "analytics.traffic_warning"]
    stored = await rows(engine, "SELECT node_id, kind, day FROM analytics_alerts")
    assert {r.node_id for r in stored} == {node.node_id} and len(stored) == 2


async def test_no_owner_chat_means_no_queued_alert_but_the_warning_stays(engine: AsyncEngine) -> None:
    now = sampling.minute(datetime.now(UTC))
    await hot_node(engine, now, cpu=10.0)
    async with engine.begin() as conn:
        raised = await sampling.raise_threshold_alerts(conn, now)
        summary = await report.summary(conn, now)
    assert [(a.kind, a.outbox_id) for a in raised] == [("traffic", None)]
    assert await rows(engine, "SELECT 1 FROM outbox") == []
    assert [(w["kind"], w["name"]) for w in summary["warnings"]] == [("traffic", "fsn-1")]


async def test_alerts_off_and_stale_samples(engine: AsyncEngine) -> None:
    now = sampling.minute(datetime.now(UTC))
    await channels(engine, owner=True)
    await hot_node(engine, now)
    async with engine.begin() as conn:
        await save_settings(conn, AnalyticsSettings(alerts_enabled=False))
        assert await sampling.raise_threshold_alerts(conn, now) == []
        later = now + timedelta(hours=2)  # the newest sample is now too old to say anything
        (sig,) = await sampling.capacity_signals(conn, now=later)
    assert not sig.fresh and sig.signal == "ok" and sig.traffic_pct is None


# --- chat_member (D7) -----------------------------------------------------------------------------

SECRET_NAME = "Nm" + secrets.token_hex(4)
SECRET_USERNAME = "u_" + secrets.token_hex(5)


async def apply(engine: AsyncEngine, raw: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        assert await handle_update(conn, Update.from_result(raw), datetime.now(UTC)) is None


async def test_chat_member_events_store_only_the_d7_fields(engine: AsyncEngine) -> None:
    await channels(engine)
    tracking = random_link()
    other_link = random_link()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO analytics_tracking_links "
                "(chat_id, name, purpose, invite_link, telegram_name, created_by) "
                "VALUES (:c, 'ad-x', 'ad on channel x', :l, 'ad-x', 'admin:owner')"
            ),
            {"c": CHANNEL_MAIN, "l": tracking},
        )
    t0 = 1793800000
    common = {"first_name": SECRET_NAME, "username": SECRET_USERNAME}
    await apply(
        engine,
        chat_member_update(
            1,
            chat_id=CHANNEL_MAIN,
            user_id=9001,
            old="left",
            new="member",
            date=t0,
            invite_link=tracking,
            link_name="ad-x",
            **common,
        ),
    )
    await apply(
        engine,
        chat_member_update(
            2,
            chat_id=CHANNEL_MAIN,
            user_id=9002,
            old="left",
            new="member",
            date=t0 + 1,
            invite_link=other_link,
            link_name="someone-elses",
            **common,
        ),
    )
    await apply(
        engine,
        chat_member_update(
            3,
            chat_id=CHANNEL_MAIN,
            user_id=9003,
            old="left",
            new="member",
            date=t0 + 2,
            via_folder=True,
            **common,
        ),
    )
    await apply(
        engine,
        chat_member_update(
            4, chat_id=CHANNEL_TEST, user_id=9004, old="left", new="member", date=t0 + 3, **common
        ),
    )
    await apply(
        engine,
        chat_member_update(
            5, chat_id=CHANNEL_MAIN, user_id=9001, old="member", new="left", date=t0 + 60, **common
        ),
    )
    await apply(
        engine,
        chat_member_update(
            6, chat_id=STRANGER_CHANNEL, user_id=9005, old="left", new="member", date=t0, **common
        ),
    )
    await apply(
        engine,
        chat_member_update(
            7, chat_id=CHANNEL_MAIN, user_id=9006, old="member", new="administrator", date=t0, **common
        ),
    )
    # The same update again (e.g. re-read after a crash) is stored once.
    await apply(
        engine,
        chat_member_update(
            1,
            chat_id=CHANNEL_MAIN,
            user_id=9001,
            old="left",
            new="member",
            date=t0,
            invite_link=tracking,
            link_name="ad-x",
            **common,
        ),
    )

    columns = [
        r[0]
        for r in await rows(
            engine,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'analytics_member_events' ORDER BY ordinal_position",
        )
    ]
    assert columns == [
        "id",
        "role",
        "chat_id",
        "at",
        "kind",
        "telegram_user_id",
        "source",
        "link_name",
        "tracking_link_id",
    ]
    got = await rows(
        engine,
        "SELECT role, chat_id, kind, telegram_user_id, source, link_name, "
        "tracking_link_id IS NOT NULL AS tracked FROM analytics_member_events ORDER BY at, id",
    )
    assert [tuple(r) for r in got] == [
        ("main", CHANNEL_MAIN, "join", 9001, "link", "ad-x", True),
        ("main", CHANNEL_MAIN, "join", 9002, "link", "someone-elses", False),
        ("main", CHANNEL_MAIN, "join", 9003, "folder", None, False),
        ("test", CHANNEL_TEST, "join", 9004, "other", None, False),
        ("main", CHANNEL_MAIN, "leave", 9001, "link", "ad-x", True),  # the leave carries its join's source
    ]
    for needle in (SECRET_NAME, SECRET_USERNAME, other_link, "9005", "9006", "language"):
        await assert_absent_from_database(engine, needle)
    assert await rows(engine, "SELECT 1 FROM tg_updates") == []
    async with engine.connect() as conn:
        links = await report.links(conn)
    assert [(lk["name"], lk["joins"], lk["leaves"], lk["net"]) for lk in links] == [("ad-x", 1, 1, 0)]


# --- usage counters (D39) -------------------------------------------------------------------------


async def test_usage_agg_is_stored_folded_and_runs_add_up(engine: AsyncEngine) -> None:
    node_id = uuid.uuid4()
    now = datetime.now(UTC)
    first = UsageAgg.model_validate(usage_block())
    async with engine.begin() as conn:
        assert await store_usage_agg(conn, node_id, first, now)
        # A later report of the same run replaces the counters (they are cumulative).
        again = UsageAgg.model_validate(usage_block(unique_users=1300))
        assert await store_usage_agg(conn, node_id, again, now)
        # Another run on the same day (agent restarted): its counters are added, not overwritten.
        other_run = UsageAgg.model_validate(usage_block(boot_id="fedcba9876543210", unique_users=50))
        assert await store_usage_agg(conn, node_id, other_run, now)
        stale = UsageAgg.model_validate(usage_block(day=(now - timedelta(days=9)).date().isoformat()))
        assert not await store_usage_agg(conn, node_id, stale, now)
    dims = await rows(
        engine,
        "SELECT dim, key, users FROM analytics_usage_dims "
        "WHERE boot_id = '0123456789abcdef' ORDER BY dim, users DESC",
    )
    assert [tuple(r) for r in dims] == [
        ("asn", "AS197207", 500),
        ("asn", "AS44244", 400),
        ("country", "IR", 1200),
        ("country", "other", 2),
        ("region", "Tehran", 600),
        ("region", "other", 1),
    ]
    async with engine.connect() as conn:
        u = await report.usage(conn, now)
    assert u["has_data"] and u["days"][-1]["unique_users"] == 1300  # max of the runs of one node
    assert u["by_country"][0]["key"] == "IR" and u["per_user_mb"]["p50"] == pytest.approx(35.5)
    assert {p["ref"] for p in u["proxies"]} == {"u_main1", "rl_de1"}
    assert sum(h["bytes"] for h in u["heatmap"]) == 2 * (2 * 10**8 + 13 * 10**9)
    # The database itself refuses a bucket of fewer than 3 users that is not "other".
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO analytics_usage_dims "
                    "(node_id, boot_id, day, dim, key, users, bytes, received_at) "
                    "VALUES (:n, 'aaaaaaaaaaaaaaaa', current_date, 'country', 'FR', 2, 1, now())"
                ),
                {"n": node_id},
            )


async def test_percentiles_are_dropped_below_three_users(engine: AsyncEngine) -> None:
    few = UsageAgg.model_validate(
        usage_block(per_user={"users": 2, "p50_mb": 1.0, "p90_mb": 2.0, "p99_mb": 3.0})
    )
    async with engine.begin() as conn:
        await store_usage_agg(conn, uuid.uuid4(), few, datetime.now(UTC))
    assert [
        tuple(r) for r in await rows(engine, "SELECT p50_mb, p90_mb, p99_mb FROM analytics_usage_daily")
    ] == [(None, None, None)]


# --- channel statistics (D38) ---------------------------------------------------------------------


async def test_channel_stats_snapshot_and_post_views(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    snap = channel_stats.Snapshot(role="main", chat_id=CHANNEL_MAIN, participants=1310)
    channel_stats.parse_broadcast_stats(broadcast_stats(), snap)
    await channel_stats.store_snapshot(engine, snap, now)
    snap.posts = [(101, 950, 41, None)]  # a later read through getMessagesViews (no reactions)
    await channel_stats.store_snapshot(engine, snap, now + timedelta(hours=3))
    async with engine.connect() as conn:
        got = await report.channel_stats(conn)
    main = got["main"]
    assert main["status"] == "ok" and main["participants"] == 1310 and main["followers"] == 1310
    assert set(main["graphs"]) == {"followers", "growth", "languages"}
    assert got["posts"] == [
        {
            "message_id": 101,
            "views": 950,
            "forwards": 41,
            "reactions": 15,
            "at": got["posts"][0]["at"],
            "posted_at": None,
            "kind": None,
        }
    ]
    assert got["test"] is None


async def test_channel_stats_loop_runs_only_with_kill_switch_off_and_a_connected_account(
    engine: AsyncEngine, vault: Vault
) -> None:
    await channels(engine)
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", free_closed_port()), deadline_s=5)
    loop = ChannelStatsReconciler(vault_factory=lambda: vault, options=options)
    assert await loop.try_run_once(engine) and loop.last_outcome == "kill_switch"  # no fleet row: ON
    async with engine.begin() as conn:
        await save_fleet_settings(conn, FleetSettings(kill_switch=False))
    assert await loop.try_run_once(engine) and loop.last_outcome == "not_connected"
    await seed_connected(engine, vault, port=free_closed_port())
    assert await loop.try_run_once(engine) and loop.last_outcome == "failed:telegram_unavailable"
    assert not await loop.try_run_once(engine)  # not due again for 3 hours
    assert await rows(engine, "SELECT 1 FROM analytics_channel_stats") == []
    async with engine.connect() as conn:
        status = await store.get_setting(conn, STATUS_KEY)
    assert status is not None and status["channel_stats"]["outcome"] == "failed:telegram_unavailable"


# --- the analytics loop ---------------------------------------------------------------------------


async def test_loop_records_a_refused_hetzner_and_telegram_without_secrets(
    engine: AsyncEngine, vault: Vault
) -> None:
    acc = await add_account(engine)
    await add_node(engine, acc, 401, name="n-401")
    token, bot_token = secrets.token_hex(32), random_token()
    port = free_closed_port()
    await channels(engine)
    async with engine.begin() as conn:
        await vault.put(conn, hcloud_secret_name(acc), SecretValue(token))
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=8123456789, username="proxyhub_test_bot"),
            token=SecretValue(bot_token),
            now=datetime.now(UTC),
        )
        await store.save_channel(
            conn, "main", {"chat_id": CHANNEL_MAIN, "username": "@ipoxxo", "title": "IPOXXO"}
        )
    loop = AnalyticsReconciler(
        vault_factory=lambda: vault,
        reader_factory=lambda t: ClosedPortReader(t, port),
        bot_factory=closed_port_bot(),
    )
    assert await loop.try_run_once(engine)
    async with engine.connect() as conn:
        status = await store.get_setting(conn, STATUS_KEY)
    assert status is not None
    assert status["nodes"]["sampled"] == 0 and list(status["nodes"]["errors"].values()) == ["unreachable"]
    assert status["channels"]["sampled"] == 0 and set(status["channels"]["errors"]) == {"main"}
    assert await rows(engine, "SELECT 1 FROM analytics_node_samples") == []
    for secret in (token, bot_token, bot_token.split(":")[1]):
        await assert_absent_from_database(engine, secret)
        assert secret not in str(status)
    # Not due again before 15 minutes / an hour.
    assert loop.next_nodes_at is not None and loop.next_channels_at is not None
    assert await loop.try_run_once(engine)
    assert await rows(engine, "SELECT 1 FROM analytics_node_samples") == []


async def test_loop_without_nodes_or_bot_only_notes_it(engine: AsyncEngine, vault: Vault) -> None:
    loop = AnalyticsReconciler(vault_factory=lambda: vault, bot_factory=closed_port_bot())
    assert await loop.try_run_once(engine)
    async with engine.connect() as conn:
        status = await store.get_setting(conn, STATUS_KEY)
    assert (
        status is not None and status["nodes"]["nodes"] == 0 and status["channels"]["error"] == "bot_not_set"
    )


# --- the report numbers ---------------------------------------------------------------------------


async def test_summary_and_series_numbers(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    acc = await add_account(engine)
    await add_offer(engine, acc)
    start = now - timedelta(hours=30)
    await add_node(engine, acc, 501, name="fsn-1", created_at=start)
    node = await sampled_node(engine, hetzner_id=501)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE nodes SET created_at = :t WHERE id = :id"),
            {"t": start - timedelta(minutes=5), "id": node.node_id},
        )
    node = await sampled_node(engine, hetzner_id=501)
    await store_sample(engine, node, sampling.minute(start), out_bytes=0)
    await store_sample(engine, node, sampling.minute(now - timedelta(minutes=5)), out_bytes=TB // 2)
    await channels(engine)
    async with engine.begin() as conn:
        await sampling.store_channel_sample(
            conn, "main", CHANNEL_MAIN, sampling.minute(now - timedelta(hours=1)), 1200
        )
        await sampling.store_channel_sample(conn, "main", CHANNEL_MAIN, sampling.minute(now), 1234)
    for i in range(3):
        await apply(
            engine,
            chat_member_update(
                10 + i,
                chat_id=CHANNEL_MAIN,
                user_id=7000 + i,
                old="left",
                new="member",
                date=int(now.timestamp()) - 60 * (i + 1),
                via_folder=i == 0,
            ),
        )
    await apply(
        engine,
        chat_member_update(
            20, chat_id=CHANNEL_MAIN, user_id=7001, old="member", new="left", date=int(now.timestamp()) - 10
        ),
    )
    async with engine.connect() as conn:
        s = await report.summary(conn, now)
        ser = await report.series(conn, now, 7)
    month_in_window = sampling.minute(start) >= report.month_bounds(now)[0]
    if month_in_window:
        assert s["traffic"]["month_out_bytes"] == TB // 2
        assert s["cost"]["per_tb_eur"] is not None
    (srv,) = s["servers"]
    assert srv["name"] == "fsn-1" and srv["traffic_pct"] == pytest.approx(2.5) and srv["state"] == "ready"
    assert s["channel"]["main"]["members"] == 1234
    assert (s["channel"]["main"]["joins_today"], s["channel"]["main"]["leaves_today"]) in (
        (3, 1),
        (0, 0),
        (1, 0),
        (2, 0),
        (2, 1),
        (3, 0),
    )
    assert s["channel"]["main"]["net_7d"] == 2
    assert {(t["kind"], t["joins"], t["leaves"]) for t in s["top_sources"]} == {
        ("folder", 1, 0),
        ("other", 2, 1),
    }
    assert sum(d["out_bytes"] or 0 for d in ser["days"]) == TB // 2
    assert sum(d["joins"] or 0 for d in ser["days"]) == 3 and len(ser["days"]) == 7
    assert [m["count"] for m in ser["members"]] == [1200, 1234] and len(ser["months"]) == 6


async def test_a_deleted_node_keeps_its_history(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    acc = await add_account(engine)
    node_id = await add_node(engine, acc, 601, name="gone-1", created_at=now - timedelta(minutes=20))
    node = await sampled_node(engine, hetzner_id=601)
    await store_sample(engine, node, sampling.minute(now - timedelta(minutes=10)), out_bytes=10**9)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE nodes SET state = 'deleted', deleted_at = now() WHERE id = :id"), {"id": node_id}
        )
    async with engine.connect() as conn:
        s = await report.summary(conn, now)
    if sampling.minute(now - timedelta(minutes=10)) >= report.month_bounds(now)[0]:
        assert [(x["name"], x["state"], x["month_out_bytes"]) for x in s["servers"]] == [
            ("gone-1", "deleted", 10**9)
        ]
    assert len(await rows(engine, "SELECT 1 FROM analytics_node_samples")) == 1


# --- migration ------------------------------------------------------------------------------------


async def test_migration_round_trip_single_head_and_no_drift(engine: AsyncEngine) -> None:
    from alembic import command
    from alembic.script import ScriptDirectory

    from proxyhub.cli import _alembic_config, downgrade, migrate

    cfg = _alembic_config()
    assert ScriptDirectory.from_config(cfg).get_heads() == ["0016_analytics"]
    await asyncio.to_thread(downgrade, "0010_domains")
    tables = await rows(engine, "SELECT tablename FROM pg_tables WHERE tablename LIKE 'analytics_%'")
    assert tables == []
    await asyncio.to_thread(migrate, "head")
    await asyncio.to_thread(command.check, cfg)
