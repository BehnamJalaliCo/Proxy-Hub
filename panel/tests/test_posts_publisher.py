"""The publisher on a real PostgreSQL: new posts on the day plan, status and host edits, alerts.

Links come from the test providers in test_posts_support.py, which read the real ``nodes`` and
``cf_zones`` tables (internal code; nothing external is imitated, D10). A post counts as sent
once the real outbox code records it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from test_posts_support import (
    PROVIDERS,
    clean_posts_tables,
    deliver,
    fail_job,
    failing_provider,
    fleet_kill_switch,
    jobs,
    make_node,
    mtproto_provider,
    node_secret,
    node_vless_id,
    rows,
    set_active_zone,
    set_node,
    setup_telegram,
    vault_at,
)

from proxyhub.links.contract import ProxyLink
from proxyhub.posts import links, plan, publisher
from proxyhub.posts import settings as posting
from proxyhub.posts.poster import BotPoster
from proxyhub.reconcilers.publisher import PublisherReconciler
from proxyhub.telegram import store
from proxyhub.telegram.schedule import TEHRAN, QuietWindow
from proxyhub.telegram.templates import ALL_DEAD_LINE
from proxyhub.vault import Vault

pytestmark = pytest.mark.db


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_posts_tables(eng)
    yield eng
    await clean_posts_tables(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return vault_at(tmp_path)


def slot_time(i: int = 0) -> datetime:
    """30 s after slot ``i`` of today's default plan (10 posts, 08:00 to midnight, Tehran)."""
    today = datetime.now(UTC).astimezone(TEHRAN).date()
    return plan.day_plan(today, count=10, start=time(8), end=time(0), quiet=[])[i].at + timedelta(seconds=30)


async def publish(engine: AsyncEngine, now: datetime, providers: list[Any] | None = None) -> str:
    async with engine.begin() as conn:

        async def harvest() -> links.Harvest:
            return await links.harvest(conn, PROVIDERS if providers is None else providers)

        return await publisher.publish_due(conn, now, harvest)


async def refresh(engine: AsyncEngine, now: datetime, providers: list[Any] | None = None) -> int:
    async with engine.begin() as conn:

        async def harvest() -> links.Harvest:
            return await links.harvest(conn, PROVIDERS if providers is None else providers)

        return await publisher.refresh_posts(conn, now, harvest)


async def configure(engine: AsyncEngine, **over: Any) -> None:
    async with engine.begin() as conn:
        await posting.save(conn, posting.parse({**posting.DEFAULTS.as_json(), **over}))


async def quiet(engine: AsyncEngine, windows: list[tuple[str, str]], interval: int = 60) -> None:
    async with engine.begin() as conn:
        await store.save_posting_settings(
            conn,
            store.PostingSettings(
                quiet_windows=[QuietWindow.parse(a, b) for a, b in windows], post_interval_min=interval
            ),
        )


async def ready_fleet(engine: AsyncEngine, n: int = 3) -> list[Any]:
    return [await make_node(engine, slot=i, ipv4=f"203.0.113.{10 + i}") for i in range(1, n + 1)]


async def posted(engine: AsyncEngine, vault: Vault, now: datetime, message_id: int = 100) -> tuple[int, int]:
    """Publish the due post and record it as sent. Returns (posts.id, channel_posts.id)."""
    assert await publish(engine, now) == "queued"
    job = (await jobs(engine, "send_post"))[-1].id
    await deliver(engine, job, message_id=message_id, at=now + timedelta(seconds=5))
    post = (await rows(engine, "SELECT p.id, c.id AS cp FROM posts p JOIN channel_posts c USING (outbox_id) "
                               "WHERE p.outbox_id = :j", j=job))[0]  # fmt: skip
    return int(post.id), int(post.cp)


async def edit_text(engine: AsyncEngine) -> str:
    return str((await jobs(engine, "edit_post"))[-1].payload["text"])


# --- new posts -----------------------------------------------------------------------------------


async def test_nothing_is_queued_without_bot_channel_or_when_disabled(
    engine: AsyncEngine, vault: Vault
) -> None:
    await ready_fleet(engine)
    now = slot_time(2)
    assert await publish(engine, now) == "no_bot"
    await setup_telegram(engine, vault, test=False)
    assert await publish(engine, now) == "no_channel"
    await setup_telegram(engine, vault, test=True)
    await configure(engine, enabled=False)
    assert await publish(engine, now) == "disabled"
    await configure(engine, posts_per_day=0)
    assert await publish(engine, now) == "disabled"
    await configure(engine, main_enabled=True)  # main is on but no main channel is set
    assert await publish(engine, now) == "no_channel"
    assert await jobs(engine) == []


async def test_due_post_carries_content_links_and_tag(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 3)
    await set_active_zone(engine, "proxyhub-posts.example")
    await setup_telegram(engine, vault)
    now = slot_time(1)
    assert await publish(engine, now) == "queued"
    [job] = await jobs(engine, "send_post")
    assert job.payload["role"] == "test" and job.payload["post_kind"] == "auto"
    body = job.payload["text"]
    first = (
        await rows(engine, "SELECT id, title FROM content_items WHERE origin = 'seed' ORDER BY id LIMIT 1")
    )[0]
    assert body.startswith(f"<b>{first.title}</b>\n\n<blockquote>")
    for i, node in enumerate(nodes, start=1):
        assert f"server=slot{i}.proxyhub-posts.example&amp;port=8443&amp;secret={node_secret(node)}" in body
        assert f"vless://{node_vless_id(node)}@slot{i}.proxyhub-posts.example:443" in body
    assert body.endswith("🔗 \u200e@ipoxxo")
    [post] = await rows(engine, "SELECT * FROM posts")
    assert (post.role, post.plan_slot, post.outbox_id, post.poster) == ("test", 1, job.id, "bot")
    assert post.content_html and post.channel_tag == "@ipoxxo" and post.content_item_id == first.id
    carried = await rows(engine, "SELECT kind, key, label, status FROM post_links ORDER BY position")
    assert [c.kind for c in carried] == ["mtproto"] * 3 + ["reality"] * 3
    assert all(c.status == "active" for c in carried)
    item = (await rows(engine, "SELECT status, used_count, last_used_at FROM content_items WHERE id = :i",
                       i=first.id))[0]  # fmt: skip
    assert (item.status, item.used_count, item.last_used_at) == ("used", 1, now)
    [action] = await rows(engine, "SELECT actor, action, reasons::text AS r FROM actions_log")
    assert (action.actor, action.action) == ("publisher", "posts.queued")
    for node in nodes:  # rule 3: no secret or VLESS id outside the post text itself
        for value in (node_secret(node), node_vless_id(node)):
            assert value not in action.r
            assert value not in str(await rows(engine, "SELECT * FROM post_links"))
            assert value not in str(await rows(engine, "SELECT * FROM posts"))


async def test_each_slot_once_gap_and_day_cap(engine: AsyncEngine, vault: Vault) -> None:
    await ready_fleet(engine)
    await setup_telegram(engine, vault)
    now = slot_time(3)
    assert await publish(engine, now) == "queued"
    await deliver(engine, (await jobs(engine, "send_post"))[0].id, message_id=1)
    assert await publish(engine, now + timedelta(minutes=1)) == "already_posted"
    assert await publish(engine, slot_time(4)) == "queued"  # ~96 min later: past the 60 min gap
    await quiet(engine, [], interval=180)
    await deliver(engine, (await jobs(engine, "send_post"))[1].id, message_id=2)
    assert await publish(engine, slot_time(5)) == "gap"
    await quiet(engine, [], interval=0)
    await configure(engine, posts_per_day=10)
    async with engine.begin() as conn:  # the day is full when it already has posts_per_day posts
        await conn.execute(text("UPDATE posts SET plan_slot = plan_slot + 100"))
    await configure(engine, posts_per_day=2)
    later = plan.day_plan(
        slot_time(0).astimezone(TEHRAN).date(), count=2, start=time(8), end=time(0), quiet=[]
    )
    assert await publish(engine, later[1].at + timedelta(seconds=5)) == "day_full"
    assert len(await jobs(engine, "send_post")) == 2


async def test_no_post_window_and_waiting_post(engine: AsyncEngine, vault: Vault) -> None:
    await ready_fleet(engine)
    await setup_telegram(engine, vault)
    now = slot_time(2)
    local = now.astimezone(TEHRAN)
    start = (local - timedelta(minutes=5)).strftime("%H:%M")
    end = (local + timedelta(minutes=60)).strftime("%H:%M")
    await quiet(engine, [(start, end)])
    # The window was added after the plan: the slot moves out of it, nothing goes now.
    assert await publish(engine, now) in {"quiet", "not_due"}
    await quiet(engine, [])
    assert await publish(engine, now) == "queued"
    # The earlier post never left the outbox (bot paused): nothing more is queued.
    assert await publish(engine, slot_time(3)) == "waiting"
    assert len(await jobs(engine, "send_post")) == 1


async def test_main_channel_only_when_turned_on(engine: AsyncEngine, vault: Vault) -> None:
    await ready_fleet(engine)
    await setup_telegram(engine, vault, main=True)
    assert await publish(engine, slot_time(1)) == "queued"
    await deliver(engine, (await jobs(engine, "send_post"))[0].id, message_id=1)
    await configure(engine, main_enabled=True)
    assert await publish(engine, slot_time(3)) == "queued"
    roles = [j.payload["role"] for j in await jobs(engine, "send_post")]
    assert roles == ["test", "main"]
    assert (await rows(engine, "SELECT role FROM posts ORDER BY id"))[1].role == "main"


async def test_no_live_link_no_post_and_one_alert_a_day(engine: AsyncEngine, vault: Vault) -> None:
    await setup_telegram(engine, vault)
    now = slot_time(2)
    assert await publish(engine, now) == "no_links"  # kill switch ON (default): nobody is alerted
    assert await jobs(engine) == []
    await fleet_kill_switch(engine, on=False)
    assert await publish(engine, now) == "no_links"
    assert await publish(engine, now + timedelta(minutes=1)) == "no_links"
    alerts = await jobs(engine, "alert")
    assert len(alerts) == 1 and "پست خودکار نرفت" in alerts[0].payload["text"]
    assert await publish(engine, now + timedelta(days=1, minutes=2)) in {"no_links", "not_due"}
    burned = await make_node(engine, slot=1, state="burned")
    assert burned
    assert await publish(engine, now) == "no_links"  # a burned node is no live link
    assert await jobs(engine, "send_post") == []


async def test_content_order_recycling_and_fallback(engine: AsyncEngine, vault: Vault) -> None:
    await ready_fleet(engine)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    async with engine.begin() as conn:
        owner_id = (
            await conn.execute(
                text(
                    "INSERT INTO content_items (title, body, status, origin) "
                    "VALUES ('نوشته‌ی مالک', 'متن مالک', 'approved', 'owner') RETURNING id"
                )
            )
        ).scalar_one()
    titles: list[str] = []
    for i in range(3):
        assert await publish(engine, slot_time(i)) == "queued"
        job = (await jobs(engine, "send_post"))[-1]
        titles.append(job.payload["text"].split("</b>", 1)[0])
        await deliver(engine, job.id, message_id=10 + i)
    seeds = await rows(engine, "SELECT title FROM content_items WHERE origin = 'seed' ORDER BY id LIMIT 2")
    assert titles == ["<b>نوشته‌ی مالک", f"<b>{seeds[0].title}", f"<b>{seeds[1].title}"]
    assert (await rows(engine, "SELECT status FROM content_items WHERE id = :i", i=owner_id))[
        0
    ].status == "used"
    await fleet_kill_switch(engine, on=False)
    # Every approved item used: the one used longest ago comes back once recycle_after_days passed.
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE content_items SET status = 'used', last_used_at = now() WHERE status = 'approved'")
        )
        await conn.execute(
            text("UPDATE content_items SET last_used_at = now() - interval '30 days' WHERE id = :i"),
            {"i": owner_id},
        )
    assert await publish(engine, slot_time(3)) == "queued"
    job = (await jobs(engine, "send_post"))[-1]
    assert job.payload["text"].startswith("<b>نوشته‌ی مالک</b>")
    assert any("محتوای تازه‌ی تأییدشده تمام شد" in a.payload["text"] for a in await jobs(engine, "alert"))
    await deliver(engine, job.id, message_id=20)
    # Nothing recyclable: the post still goes, with the fallback title, and the owner hears it.
    assert await publish(engine, slot_time(4)) == "queued"
    job = (await jobs(engine, "send_post"))[-1]
    assert job.payload["text"].startswith("<b>📡 پروکسی‌ها و کانفیگ‌های تازه</b>\n\n📡")
    assert any("پست بی محتوا رفت" in a.payload["text"] for a in await jobs(engine, "alert"))
    assert (await rows(engine, "SELECT content_item_id FROM posts ORDER BY id DESC LIMIT 1"))[
        0
    ].content_item_id is None


async def test_a_failing_provider_costs_only_its_own_links(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    async with engine.begin() as conn:
        h = await links.harvest(conn, [failing_provider, mtproto_provider])
        assert not h.complete and len(h.active("mtproto")) == 2
        await conn.execute(text("SELECT 1"))  # the transaction is still usable
    assert await publish(engine, slot_time(1), [failing_provider, mtproto_provider]) == "queued"
    body = (await jobs(engine, "send_post"))[0].payload["text"]
    assert node_secret(nodes[0]) in body and "vless://" not in body


# --- status and host edits -----------------------------------------------------------------------


async def test_burned_node_is_marked_by_an_edit_and_never_comes_back(
    engine: AsyncEngine, vault: Vault
) -> None:
    nodes = await ready_fleet(engine, 3)
    await setup_telegram(engine, vault)
    now = slot_time(2)
    post_id, cp_id = await posted(engine, vault, now)
    assert await refresh(engine, now + timedelta(minutes=1)) == 0  # nothing changed: no edit
    await set_node(engine, nodes[1], state="burned")
    assert await refresh(engine, now + timedelta(minutes=2)) == 1
    [edit] = await jobs(engine, "edit_post")
    assert edit.payload["post_id"] == cp_id
    body = edit.payload["text"]
    assert "<s>پروکسی ۲</s> 🚫" in body and node_secret(nodes[1]) not in body
    assert node_secret(nodes[0]) in body and node_secret(nodes[2]) in body
    assert "<s>ریلیتی ۲</s> — 🚫 این کانفیگ از دسترس خارج شد" in body and node_vless_id(nodes[1]) not in body
    statuses = await rows(
        engine, "SELECT label, status FROM post_links WHERE post_id = :p ORDER BY position", p=post_id
    )
    assert [s.status for s in statuses].count("burned") == 2
    # One edit job at a time: nothing more while it waits.
    assert await refresh(engine, now + timedelta(minutes=30)) == 0
    await deliver(engine, edit.id, message_id=0, at=now + timedelta(minutes=3))
    assert await refresh(engine, now + timedelta(minutes=31)) == 0  # sent: channel text == render
    # The node comes back: the post keeps its mark (status in a post never returns to active).
    await set_node(engine, nodes[1], state="ready")
    assert await refresh(engine, now + timedelta(minutes=45)) == 0
    [action] = await rows(
        engine, "SELECT reasons::text AS r FROM actions_log WHERE action = 'posts.edit_queued'"
    )
    assert "link_status" in action.r and "burned" in action.r
    assert all(node_secret(n) not in action.r for n in nodes)


async def test_domain_change_moves_recent_posts_only(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 2)
    await set_active_zone(engine, "old-posts.example")
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    old_post, _ = await posted(engine, vault, slot_time(0), message_id=1)
    async with engine.begin() as conn:  # 49 hours old: outside the 48 h edit window
        await conn.execute(text("UPDATE posts SET created_at = :t WHERE id = :p"),
                           {"t": slot_time(1) - timedelta(hours=49), "p": old_post})  # fmt: skip
    fresh_post, fresh_cp = await posted(engine, vault, slot_time(1), message_id=2)
    await set_active_zone(engine, "new-posts.example")
    assert await refresh(engine, slot_time(1) + timedelta(minutes=1)) == 1  # the 49 h old post is left alone
    [edit] = await jobs(engine, "edit_post")
    assert edit.payload["post_id"] == fresh_cp
    body = edit.payload["text"]
    assert "old-posts.example" not in body and body.count("new-posts.example") == 4
    assert f"server=slot1.new-posts.example&amp;port=8443&amp;secret={node_secret(nodes[0])}" in body
    assert "<s>" not in body  # nothing died: only the host moved
    assert old_post != fresh_post
    [action] = await rows(
        engine, "SELECT reasons::text AS r FROM actions_log WHERE action = 'posts.edit_queued'"
    )
    assert "link_moved" in action.r
    # No active domain any more: the links fall back to the nodes' IPv4 (owner decision).
    await deliver(engine, edit.id, message_id=0)
    await set_active_zone(engine, None)
    assert await refresh(engine, slot_time(1) + timedelta(minutes=20)) == 1
    body = await edit_text(engine)
    assert "server=203.0.113.11&amp;port=8443" in body and "vless://" in body and "@203.0.113.12:443" in body


async def test_min_gap_between_two_edits_of_a_post(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 3)
    await setup_telegram(engine, vault)
    now = slot_time(2)
    await posted(engine, vault, now)
    await set_node(engine, nodes[0], state="burned")
    assert await refresh(engine, now + timedelta(minutes=1)) == 1
    await deliver(engine, (await jobs(engine, "edit_post"))[0].id, message_id=0)
    await set_node(engine, nodes[1], state="burned")
    assert await refresh(engine, now + timedelta(minutes=5)) == 0  # 4 minutes after the last edit
    assert await refresh(engine, now + timedelta(minutes=12)) == 1


async def test_expired_and_gone_links(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    now = slot_time(2)
    post_id, _ = await posted(engine, vault, now)
    # The provider now reports node 1's Reality config as expired (its 24 h are over).
    await set_node(engine, nodes[0], ready_at=datetime.now(UTC) - timedelta(hours=25))
    # Node 2 is gone from every provider (slot freed): its links count as burned.
    await set_node(engine, nodes[1], slot=None)
    assert await refresh(engine, now + timedelta(minutes=1)) == 1
    body = await edit_text(engine)
    assert "<s>ریلیتی ۱</s> — ⌛️ زمان این کانفیگ تمام شد" in body
    assert "<s>پروکسی ۲</s> 🚫" in body and "<s>ریلیتی ۲</s> — 🚫" in body
    assert node_secret(nodes[0]) in body  # node 1's MTProto link still works
    assert ALL_DEAD_LINE not in body
    carried = await rows(engine, "SELECT label, status FROM post_links WHERE post_id = :p", p=post_id)
    got = {r.label: r.status for r in carried}
    assert got == {"پروکسی ۱": "active", "ریلیتی ۱": "expired", "پروکسی ۲": "burned", "ریلیتی ۲": "burned"}


async def test_everything_dead_gets_the_closing_line(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 1)
    await setup_telegram(engine, vault)
    now = slot_time(2)
    await posted(engine, vault, now)
    await set_node(engine, nodes[0], state="burned")
    assert await refresh(engine, now + timedelta(minutes=1)) == 1
    body = await edit_text(engine)
    assert ALL_DEAD_LINE in body and "<a href" not in body and "<code>" not in body
    assert body.endswith("🔗 \u200e@ipoxxo")


async def test_failed_edits_back_off_and_stop_with_one_alert(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    now = slot_time(2)
    post_id, _ = await posted(engine, vault, now)
    await set_node(engine, nodes[0], state="burned")
    t = now + timedelta(minutes=1)
    for attempt in range(1, publisher.MAX_EDIT_FAILURES + 1):
        assert await refresh(engine, t) == 1, attempt
        await fail_job(engine, (await jobs(engine, "edit_post"))[-1].id)
        assert await refresh(engine, t + timedelta(minutes=11)) == 0  # settles the failure, backs off
        [p] = await rows(engine, "SELECT edit_failures, edit_retry_at FROM posts WHERE id = :p", p=post_id)
        assert p.edit_failures == attempt
        t = p.edit_retry_at + timedelta(seconds=1) if p.edit_retry_at else t + timedelta(hours=7)
    assert await refresh(engine, t + timedelta(days=1)) == 0  # given up
    assert len(await jobs(engine, "edit_post")) == publisher.MAX_EDIT_FAILURES
    alerts = [a for a in await jobs(engine, "alert") if "ویرایش یک پست ممکن نشد" in a.payload["text"]]
    assert len(alerts) == 1
    assert publisher.edit_backoff(1) == timedelta(minutes=15) and publisher.edit_backoff(9) == timedelta(
        hours=6
    )


async def test_no_edit_from_a_partial_picture(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    now = slot_time(2)
    await posted(engine, vault, now)
    await set_node(engine, nodes[0], state="burned")
    # A provider failed: a missing link would look dead, so nothing is edited this round.
    assert await refresh(engine, now + timedelta(minutes=1), [failing_provider, *PROVIDERS]) == 0

    async def broken_url(db: AsyncConnection) -> list[ProxyLink]:
        good = await mtproto_provider(db)
        return [ProxyLink(**{**x.__dict__, "url": "https://t.me/proxy?server=x"}) for x in good]

    from test_posts_support import reality_provider

    # A provider returned a link we refused (bad URL): posts carrying it wait.
    assert await refresh(engine, now + timedelta(minutes=2), [broken_url, reality_provider]) == 0
    assert await jobs(engine, "edit_post") == []
    assert await refresh(engine, now + timedelta(minutes=3)) == 1


async def test_edits_are_bounded_per_round(engine: AsyncEngine, vault: Vault) -> None:
    nodes = await ready_fleet(engine, 1)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    for i in range(7):
        await posted(engine, vault, slot_time(i), message_id=100 + i)
    await set_node(engine, nodes[0], state="burned")
    t = slot_time(7) + timedelta(minutes=1)
    assert await refresh(engine, t) == publisher.MAX_EDITS_PER_RUN
    assert await refresh(engine, t + timedelta(seconds=30)) == 2
    assert {j.kind for j in await jobs(engine)} <= {"send_post", "edit_post", "alert"}  # rule 6


# --- the loop ------------------------------------------------------------------------------------


class _EditBreaks(BotPoster):
    async def queue_edit(self, db: AsyncConnection, *, channel_post_id: int, text_html: str) -> int:
        raise RuntimeError(f"edit step breaks; text was {text_html}")  # the text holds proxy links


async def test_reconciler_runs_both_steps_and_one_failure_does_not_block_the_other(
    engine: AsyncEngine, vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    nodes = await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    first = slot_time(1)
    loop = PublisherReconciler(now=lambda: first, providers=PROVIDERS)
    assert await loop.try_run_once(engine)
    assert loop.last == {"edits": 0, "post": "queued"}
    await deliver(engine, (await jobs(engine, "send_post"))[0].id, message_id=1)
    await set_node(engine, nodes[0], state="burned")
    second = slot_time(2)
    broken = PublisherReconciler(now=lambda: second, providers=PROVIDERS, poster=_EditBreaks())
    with caplog.at_level(logging.DEBUG):
        assert await broken.try_run_once(engine)
    assert broken.last == {"edits": "error", "post": "queued"}
    assert len(await jobs(engine, "send_post")) == 2 and await jobs(engine, "edit_post") == []
    assert "RuntimeError" in caplog.text and "edit step breaks; text was" not in caplog.text.replace(
        "edit step breaks; text was {text_html}", ""
    )
    for n in nodes:
        assert node_secret(n) not in caplog.text and node_vless_id(n) not in caplog.text
    assert loop.name == "publisher" and loop.interval_seconds == 30.0


async def test_worker_registers_the_publisher() -> None:
    from proxyhub.worker import reconcilers

    assert [r for r in reconcilers() if isinstance(r, PublisherReconciler)]


async def test_no_secret_reaches_logs_or_other_tables(
    engine: AsyncEngine, vault: Vault, caplog: pytest.LogCaptureFixture
) -> None:
    nodes = await ready_fleet(engine, 2)
    await set_active_zone(engine, "leak-posts.example")
    await setup_telegram(engine, vault)
    await fleet_kill_switch(engine, on=False)
    now = slot_time(2)
    with caplog.at_level(logging.DEBUG):
        await posted(engine, vault, now)
        await set_node(engine, nodes[0], state="burned")
        await set_active_zone(engine, "leak2-posts.example")
        assert await refresh(engine, now + timedelta(minutes=1), [failing_provider, *PROVIDERS]) == 0
        assert await refresh(engine, now + timedelta(minutes=2)) == 1
    dumps = [
        caplog.text,
        str(await rows(engine, "SELECT * FROM actions_log")),
        str(await rows(engine, "SELECT * FROM posts")),
        str(await rows(engine, "SELECT * FROM post_links")),
        str(await rows(engine, "SELECT * FROM settings")),
        str(await rows(engine, "SELECT * FROM content_items")),
    ]
    for n in nodes:
        for value in (node_secret(n), node_vless_id(n)):
            assert all(value not in d for d in dumps)
    assert "failing_provider" in caplog.text  # the failure itself is visible (name and type only)
