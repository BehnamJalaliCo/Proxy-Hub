"""«پاک کردن پایان‌یافته‌ها» on a real PostgreSQL (proxyhub/cleanup/ended.py).

One world holds, side by side, everything the purge must remove and everything it must keep: a
deleted node with every kind of row that hangs on a node, deleted nodes that are still in use for
each reason, live nodes, ended and active Reality configs, retired and live sponsor registrations,
finished and recent posts of both channels, finished, recent and pending outbox jobs, the cost
ledger and the actions log. The tests prove: active rows survive, ended rows go, the foreign-key
order works (a registration a deleted node's Telemt user points at goes in the same transaction),
the ledger and the log are untouched, the counts are exact, and the preview changes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_cleanup_support import MAIN_CHAT, TEST_CHAT, World, mtproto_key, wipe
from test_fleet_support import make_vault

from proxyhub.cleanup import ended
from proxyhub.mtproto.urls import user_name_for
from proxyhub.posts import settings as posting
from proxyhub.telegram.schedule import TEHRAN

pytestmark = pytest.mark.db

EVERY_TABLE = (*ended.ALLOWED_TABLES, *sorted(ended.NEVER_TABLES), "probe_cycles", "agent_ca_certificates")


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await wipe(eng)
    yield eng
    await wipe(eng)
    await eng.dispose()


@dataclass
class Seeded:
    w: World
    ids: dict[str, Any]


async def seed(engine: AsyncEngine, tmp_path: Path) -> Seeded:
    w = World(engine)
    ids: dict[str, Any] = {}
    await w.telegram(make_vault(tmp_path))

    # --- nodes -------------------------------------------------------------------------------------
    gone = ids["gone"] = await w.node(state="deleted", deleted_at=w.old)
    await w.node_rows(gone)
    gone_key = await w.reality_key(node_id=gone)
    ids["gone_config"] = cfg = await w.reality_config(gone, gone_key, status="expired", ended_at=w.old)
    await w.reality_usage(cfg, gone)

    ids["young"] = await w.node(state="deleted", deleted_at=w.recent)  # too_recent
    ids["reach"] = reach = await w.node(state="deleted", deleted_at=w.old)  # location_reach
    await w.node_rows(reach, reach_at=w.now - timedelta(days=1))
    ids["ledger"] = ledger = await w.node(state="deleted", deleted_at=w.old)  # cost_ledger
    await w.run(
        "INSERT INTO cost_ledger (day, account_id, node_id, euros, source) VALUES (:d, :a, :n, 1.25, "
        "'hetzner')",
        d=w.old.date(),
        a=w.account,
        n=ledger,
    )
    ids["dns"] = dns = await w.node(state="deleted", deleted_at=w.old)  # dns_record
    await w.run(
        "INSERT INTO cf_zones (id, name, status, pool_state, seen_at) "
        "VALUES (:z, 'cleanup-test.example', 'active', 'active', now())",
        z=uuid.uuid4().hex,
    )
    await w.run(
        "INSERT INTO cf_records (id, zone_id, cf_record_id, name, type, content, ttl, proxied, purpose, "
        "node_id) "
        "SELECT :i, id, :c, 'slot9.cleanup-test.example', 'A', '203.0.113.9', 60, false, 'slot', :n "
        "FROM cf_zones",
        i=uuid.uuid4(),
        c=uuid.uuid4().hex,
        n=dns,
    )
    ids["slot_node"] = slot_node = await w.node(state="deleted", deleted_at=w.old, slot=3)  # reality_in_use
    slot_key = await w.reality_key(slot=3)
    ids["slot_active_config"] = await w.reality_config(slot_node, slot_key)
    ids["in_post"] = in_post = await w.node(state="deleted", deleted_at=w.old)  # in_recent_post
    ids["live"] = live = await w.node(state="ready", slot=1)
    await w.node_rows(live, reach_at=w.now - timedelta(days=1))

    # --- reality (slot 1, served by the live node) ---------------------------------------------
    key1 = await w.reality_key(slot=1)
    ids["active_config"] = active = await w.reality_config(live, key1)
    await w.reality_usage(active, live)
    ids["ended_config"] = ended_cfg = await w.reality_config(live, key1, status="full", ended_at=w.old)
    await w.reality_usage(ended_cfg, live)
    ids["young_config"] = await w.reality_config(live, key1, status="expired", ended_at=w.recent)
    ids["carried_config"] = carried = await w.reality_config(live, key1, status="burned", ended_at=w.old)

    # --- sponsor ---------------------------------------------------------------------------------
    ids["reg_gone"] = reg_gone = await w.registration(
        user=user_name_for(gone), state="retired", updated_at=w.old, stats=2
    )
    # The deleted node's Telemt user still points at it: the node must go first (FK order).
    await w.run("UPDATE telemt_users SET registration_id = :r WHERE node_id = :n", r=reg_gone, n=gone)
    ids["reg_live"] = await w.registration(user=user_name_for(live), state="retired", updated_at=w.old)
    ids["reg_young"] = await w.registration(user="mt_00000000young000", state="retired", updated_at=w.recent)
    ids["reg_registered"] = await w.registration(
        user="mt_0000000000active", state="registered", updated_at=w.old
    )

    # --- posts and outbox ------------------------------------------------------------------------
    long_ago = w.now - timedelta(days=5)
    send_old = await w.job("send_post", done_at=long_ago, payload={"role": "test", "text": "t"})
    edit_old = await w.job("edit_post", done_at=long_ago, payload={"post_id": 1, "text": "t"})
    ids["test_post_old"] = await w.post(
        role="test",
        created_at=long_ago,
        slot=0,
        outbox_id=send_old,
        last_edit_outbox_id=edit_old,
        links=[(mtproto_key(gone), "expired"), (f"reality:{ended_cfg}", "full")],
    )
    ids["test_cp_old"] = await w.channel_post(
        role="test", chat_id=TEST_CHAT, message_id=10, outbox_id=send_old, created_at=long_ago
    )
    button_job = await w.job("send_post", done_at=long_ago, payload={"role": "test", "text": "t"})
    ids["test_cp_button"] = await w.channel_post(
        role="test", chat_id=TEST_CHAT, message_id=11, outbox_id=button_job, created_at=long_ago
    )
    ids["test_cp_in_main_chat"] = await w.channel_post(
        role="test", chat_id=MAIN_CHAT, message_id=12, outbox_id=None, created_at=long_ago
    )
    # A recent «پست آزمایشی» (no posts row): kept, and so is its finished job (its row points at it).
    ids["button_job_recent"] = button_recent = await w.job(
        "send_post", done_at=w.now - timedelta(hours=2), payload={"role": "test", "text": "t"}
    )
    await w.channel_post(
        role="test",
        chat_id=TEST_CHAT,
        message_id=15,
        outbox_id=button_recent,
        created_at=w.now - timedelta(hours=2),
    )
    # A finished test post whose edit is still waiting: not finished, kept.
    send_w = await w.job("send_post", done_at=long_ago, payload={"role": "test", "text": "t"})
    ids["waiting_edit_job"] = await w.job("edit_post", payload={"post_id": 0, "text": "t"})
    ids["test_post_waiting"] = await w.post(
        role="test",
        created_at=long_ago,
        slot=1,
        outbox_id=send_w,
        last_edit_outbox_id=ids["waiting_edit_job"],
    )
    await w.channel_post(role="test", chat_id=TEST_CHAT, message_id=13, outbox_id=send_w, created_at=long_ago)
    # A recent test post (inside the edit window): kept with its job.
    send_new = await w.job("send_post", done_at=w.now - timedelta(hours=1), payload={"role": "test"})
    ids["test_post_new"] = await w.post(
        role="test", created_at=w.now - timedelta(hours=1), slot=2, outbox_id=send_new
    )
    await w.channel_post(
        role="test",
        chat_id=TEST_CHAT,
        message_id=14,
        outbox_id=send_new,
        created_at=w.now - timedelta(hours=1),
    )
    # The MAIN channel: an old post (never touched) and a recent one that carries two ended links.
    send_main = await w.job("send_post", done_at=long_ago, payload={"role": "main", "text": "m"})
    ids["main_post_old"] = await w.post(role="main", created_at=long_ago, slot=0, outbox_id=send_main)
    ids["main_cp_old"] = await w.channel_post(
        role="main", chat_id=MAIN_CHAT, message_id=20, outbox_id=send_main, created_at=long_ago
    )
    send_main2 = await w.job("send_post", done_at=w.now - timedelta(hours=2), payload={"role": "main"})
    ids["main_post_new"] = await w.post(
        role="main",
        created_at=w.now - timedelta(hours=2),
        slot=1,
        outbox_id=send_main2,
        links=[(mtproto_key(in_post), "active"), (f"reality:{carried}:xhttp", "active")],
    )
    ids["alert_old"] = await w.job("alert", done_at=w.old)
    ids["failed_old"] = await w.job("alert", done_at=w.old, failed=True)
    ids["alert_recent"] = await w.job("alert", done_at=w.recent)
    ids["pending"] = await w.job("alert")
    ids["analytics_job"] = analytics_job = await w.job("alert", done_at=w.old)
    await w.run(
        "INSERT INTO analytics_alerts (node_id, kind, day, at, value, threshold, outbox_id) "
        "VALUES (:n, 'cpu', :d, :t, 95, 90, :o)",
        n=live,
        d=w.old.date(),
        t=w.old,
        o=analytics_job,
    )
    ids["removed_jobs"] = {send_old, edit_old, button_job, ids["alert_old"], ids["failed_old"]}
    await w.run(
        "INSERT INTO actions_log (actor, action, target, dry_run, allowed, reasons) "
        "VALUES ('test', 'something', 'x', false, true, '{}')"
    )
    return Seeded(w=w, ids=ids)


EXPECTED_REMOVED = {
    "reality_usage": 2,
    "reality_configs": 2,
    "probe_network_results": 2,
    "probe_measurements": 1,
    "probe_rounds": 1,
    "probe_node_state": 1,
    "probe_location_reach": 1,
    "agent_node_reports": 2,
    "agent_nodes": 1,
    "agent_certificates": 1,
    "agent_registration_tokens": 1,
    "telemt_users": 1,
    "telemt_node_configs": 1,
    "mtproto_proxies": 1,
    "node_operations": 1,
    "reality_node_status": 1,
    "reality_keys": 1,
    "node_events": 2,
    "nodes": 1,
    "mtproxy_stats": 2,
    "sponsor_flows": 1,
    "mtproxy_registrations": 1,
    "post_links": 2,
    "posts": 1,
    "channel_posts": 2,
    "outbox": 5,
}
EXPECTED_KEPT = {
    "nodes": {
        "too_recent": 1,
        "location_reach": 1,
        "cost_ledger": 1,
        "dns_record": 1,
        "reality_in_use": 1,
        "in_recent_post": 1,
    },
    "reality_configs": {"too_recent": 1, "in_recent_post": 1},
    "registrations": {"too_recent": 1, "proxy_exists": 1},
}


async def run_purge(w: World, *, dry_run: bool, min_age_hours: int = 1) -> ended.PurgeResult:
    async with w.engine.begin() as conn:
        result = await ended.purge_ended(conn, now=w.now, min_age_hours=min_age_hours, dry_run=dry_run)
        if dry_run:
            await conn.rollback()
    return result


async def test_preview_counts_exactly_and_changes_nothing(engine: AsyncEngine, tmp_path: Path) -> None:
    s = await seed(engine, tmp_path)
    before = await s.w.counts(EVERY_TABLE)
    result = await run_purge(s.w, dry_run=True)
    assert result.removed == EXPECTED_REMOVED
    assert result.total == sum(EXPECTED_REMOVED.values())
    assert result.kept == EXPECTED_KEPT
    assert result.ip_history_unlinked == 1
    assert result.as_json()["dry_run"] is True
    assert await s.w.counts(EVERY_TABLE) == before  # nothing changed


async def test_purge_removes_ended_rows_and_keeps_everything_else(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    s = await seed(engine, tmp_path)
    w, ids = s.w, s.ids
    before = await w.counts(EVERY_TABLE)
    ledger_before = await w.rows("SELECT * FROM cost_ledger ORDER BY id")
    log_before = await w.rows("SELECT * FROM actions_log ORDER BY id")
    settings_before = await w.rows("SELECT key, value FROM settings ORDER BY key")
    secrets_before = await w.rows("SELECT name FROM secrets ORDER BY name")

    preview = await run_purge(w, dry_run=True)
    result = await run_purge(w, dry_run=False)
    assert result.removed == preview.removed == EXPECTED_REMOVED
    assert result.kept == EXPECTED_KEPT
    after = await w.counts(EVERY_TABLE)
    for table in ended.ALLOWED_TABLES:
        assert after[table] == before[table] - EXPECTED_REMOVED[table], table
    for table in ended.NEVER_TABLES:  # the ledger, the log, settings, secrets, accounts, DNS, ...
        assert after[table] == before[table], table
    assert await w.rows("SELECT * FROM cost_ledger ORDER BY id") == ledger_before
    assert await w.rows("SELECT * FROM actions_log ORDER BY id") == log_before
    assert await w.rows("SELECT key, value FROM settings ORDER BY key") == settings_before
    assert await w.rows("SELECT name FROM secrets ORDER BY name") == secrets_before

    # The deleted node went with everything on it; the burned-IP memory stays, unlinked.
    gone = ids["gone"]
    assert not await w.rows("SELECT 1 FROM nodes WHERE id = :n", n=gone)
    for table in ("node_events", "agent_nodes", "telemt_users", "mtproto_proxies", "probe_location_reach"):
        assert not await w.rows(f"SELECT 1 FROM {table} WHERE node_id = :n", n=gone), table  # noqa: S608
    assert await w.one("SELECT count(*) FROM ip_history WHERE node_id IS NULL") == 1
    # Every deleted node in use is still there, and the live node with all of its rows.
    for name in ("young", "reach", "ledger", "dns", "slot_node", "in_post", "live"):
        assert await w.rows("SELECT 1 FROM nodes WHERE id = :n", n=ids[name]), name
    live = ids["live"]
    for table in ("node_events", "agent_nodes", "telemt_users", "mtproto_proxies", "probe_location_reach"):
        assert await w.rows(f"SELECT 1 FROM {table} WHERE node_id = :n", n=live), table  # noqa: S608
    # Reality: active configs stay (also the one made for a deleted slot node), ended ones go.
    configs = {r.id: r.status for r in await w.rows("SELECT id, status FROM reality_configs")}
    assert ids["active_config"] in configs and ids["slot_active_config"] in configs
    assert ids["young_config"] in configs and ids["carried_config"] in configs
    assert ids["ended_config"] not in configs and ids["gone_config"] not in configs
    assert (
        await w.one("SELECT total_bytes FROM reality_usage WHERE config_id = :c", c=ids["active_config"])
        == 10
    )
    # Sponsor: only the retired registration whose proxy is gone.
    regs = {r.id for r in await w.rows("SELECT id FROM mtproxy_registrations")}
    assert regs == {ids["reg_live"], ids["reg_young"], ids["reg_registered"]}
    assert await w.one("SELECT count(*) FROM sponsor_flows") == 3
    # Posts: the old finished test post and test-channel rows went; the main channel's never.
    posts = {r.id for r in await w.rows("SELECT id FROM posts")}
    assert posts == {
        ids["test_post_waiting"],
        ids["test_post_new"],
        ids["main_post_old"],
        ids["main_post_new"],
    }
    cps = {r.id: (r.role, r.chat_id) for r in await w.rows("SELECT id, role, chat_id FROM channel_posts")}
    assert ids["test_cp_old"] not in cps and ids["test_cp_button"] not in cps
    assert ids["test_cp_in_main_chat"] in cps and ids["main_cp_old"] in cps
    assert all(role == "main" or chat == TEST_CHAT for role, chat in cps.values() if chat != MAIN_CHAT)
    # Outbox: finished, unreferenced jobs older than the cutoff only.
    jobs = {r.id for r in await w.rows("SELECT id FROM outbox")}
    assert not jobs & ids["removed_jobs"]
    for name in ("alert_recent", "pending", "analytics_job", "waiting_edit_job", "button_job_recent"):
        assert ids[name] in jobs, name
    assert await w.one("SELECT count(*) FROM channel_posts WHERE outbox_id IS NULL") == 1  # none unlinked
    # The main channel's posts still find their message and their edit through the outbox.
    linked = await w.rows(
        "SELECT p.id FROM posts p JOIN channel_posts cp ON cp.outbox_id = p.outbox_id WHERE p.role = 'main'"
    )
    assert {r.id for r in linked} == {ids["main_post_old"]}

    again = await run_purge(w, dry_run=False)
    assert again.total == 0  # nothing ended is left that may go


async def test_test_posts_of_yesterday_stay_for_the_plan_even_with_a_short_edit_window(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """The publisher counts a day's posts and its taken slots by ``plan_day`` (yesterday's plan
    still runs past midnight): with the owner's edit window at 1 hour, a test post of yesterday's
    plan stays, one of the day before goes."""
    w = World(engine)
    await w.telegram(make_vault(tmp_path))
    async with engine.begin() as conn:
        await posting.save(conn, posting.PostingConfig(edit_window_hours=1))
    today = w.now.astimezone(TEHRAN).date()
    kept, gone = [], []
    for slot, (day, bucket) in enumerate(
        ((today - timedelta(days=1), kept), (today - timedelta(days=2), gone))
    ):
        job = await w.job("send_post", done_at=w.now - timedelta(hours=25), payload={"role": "test"})
        bucket.append(
            await w.post(
                role="test", created_at=w.now - timedelta(hours=25), slot=slot, outbox_id=job, plan_day=day
            )
        )
    result = await run_purge(w, dry_run=False)
    assert result.removed["posts"] == 1
    assert {r.id for r in await w.rows("SELECT id FROM posts")} == set(kept)


async def test_min_age_decides_what_is_old_enough(engine: AsyncEngine, tmp_path: Path) -> None:
    s = await seed(engine, tmp_path)
    week = await run_purge(s.w, dry_run=True, min_age_hours=24 * 7)
    assert week.total == 0  # everything ended 3 to 5 days ago
    assert week.kept["nodes"] == {"too_recent": 7}
    day = await run_purge(s.w, dry_run=True, min_age_hours=24)
    assert day.removed == EXPECTED_REMOVED
    for bad in (0, -1, 721, True):
        with pytest.raises(ValueError, match="min_age_hours"):
            async with s.w.engine.begin() as conn:
                await ended.purge_ended(conn, now=s.w.now, min_age_hours=bad, dry_run=True)  # type: ignore[arg-type]


async def test_only_allowlisted_tables_can_be_deleted_from(engine: AsyncEngine) -> None:
    assert not set(ended.ALLOWED_TABLES) & ended.NEVER_TABLES
    assert len(set(ended.ALLOWED_TABLES)) == len(ended.ALLOWED_TABLES)
    async with engine.begin() as conn:
        deleter = ended._Deleter(
            conn, ended.PurgeResult(dry_run=True, min_age_hours=1, cutoff=datetime.now(UTC))
        )
        for table in (
            "cost_ledger",
            "actions_log",
            "settings",
            "secrets",
            "nodes; DROP TABLE x",
            "ip_history",
        ):
            with pytest.raises(ValueError, match="may not touch"):
                await deleter.delete(table, "true", {})
        await conn.rollback()
    source = ended_source()
    assert source.count('"DELETE FROM"') == 1  # every DELETE goes through _Deleter.delete


async def test_every_foreign_key_into_a_purged_table_is_handled(engine: AsyncEngine) -> None:
    """A new table that points at nodes, configs, registrations, posts or the outbox must be added to
    ``ended.REFERENCES`` (deleted first, guarded, or kept with its link cleared) before the purge
    can run: the catalog of the real database is compared with it."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.conrelid::regclass::text AS child, a.attname AS col, "
                    "c.confrelid::regclass::text AS parent, c.confdeltype::text AS on_delete "
                    "FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid "
                    "AND a.attnum = ANY(c.conkey) WHERE c.contype = 'f'"
                )
            )
        ).all()
    found = {
        f"{r.child}.{r.col} -> {r.parent}": r.on_delete for r in rows if r.parent in ended.ALLOWED_TABLES
    }
    assert set(found) == set(ended.REFERENCES), sorted(set(found) ^ set(ended.REFERENCES))
    for ref, how in ended.REFERENCES.items():
        child = ref.split(".", 1)[0]
        if how == ended.DELETED_FIRST:
            assert child in ended.ALLOWED_TABLES, ref
        if how == ended.SET_NULL:
            assert found[ref] == "n" and child in ended.NEVER_TABLES, ref
    # Everything the purge may never touch is guarded or kept, never deleted.
    for ref, how in ended.REFERENCES.items():
        if ref.split(".", 1)[0] in ended.NEVER_TABLES:
            assert how in (ended.GUARD, ended.SET_NULL), ref


def ended_source() -> str:
    return Path(ended.__file__).read_text(encoding="utf-8")
