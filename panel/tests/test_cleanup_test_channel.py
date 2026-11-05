"""«پاک کردن کانال آزمایشی» (decision D44, proxyhub/cleanup/test_channel.py).

Telegram is never imitated (D10): the network paths run against a local port nobody listens on
(a real connection refusal) and against the real api.telegram.org with a random token (its real
401). The sweep's splitting is pure logic, driven here with outcomes as data. The database is a
real PostgreSQL. Nothing here can ever reach a main channel: the tests pin that too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_cleanup_support import MAIN_CHAT, TEST_CHAT, World, random_token, wipe
from test_fleet_support import make_vault
from test_telegram_support import closed_port_bot, real_bot, telegram_reachable

from proxyhub.cleanup import test_channel as tc
from proxyhub.reconcilers.base import lock_key
from proxyhub.telegram import outbox
from proxyhub.vault import SecretValue, Vault

# --- pure: the typed confirmation, the id range, the sweep -------------------------------------------


@pytest.mark.parametrize("typed", ["پاک کن", " پاک   کن ", "پاک‌کن", "پاک‌ کن", "پاك کن"])
def test_the_typed_confirmation(typed: str) -> None:
    assert tc.confirmed(typed)


@pytest.mark.parametrize("typed", ["", "پاک", "پاک کن!", "حذف کن", "clean", "پاک کن" * 10, None, 1])
def test_anything_else_is_not_a_confirmation(typed: object) -> None:
    assert not tc.confirmed(typed)


def test_the_id_range_starts_after_the_creation_notice_and_reaches_past_the_last_known() -> None:
    assert tc.id_range(None) == (2, 200)
    assert tc.id_range(412) == (2, 612)
    assert tc.request_count(2, 200) == 2
    assert tc.request_count(2, 612) == 7
    assert tc.request_count(2, 101) == 1
    assert tc.request_count(5, 4) == 0


def test_the_sweep_goes_newest_first_in_hundreds() -> None:
    sweep = tc.Sweep.over(2, 250)
    assert sweep.todo == [(151, 250), (51, 150), (2, 50)]
    sweep.ok()
    assert sweep.next() == (51, 150)
    sweep.ok()
    sweep.ok()
    assert sweep.next() is None
    assert sweep.covered == 249 and sweep.removed_ids() == list(range(2, 251)) and sweep.kept == []


def test_a_refused_request_is_split_into_tens_then_single_ids() -> None:
    sweep = tc.Sweep.over(2, 130)
    assert sweep.todo == [(31, 130), (2, 30)]
    sweep.refused()  # Telegram refused 31..130 as a whole
    assert sweep.todo[:3] == [(121, 130), (111, 120), (101, 110)] and sweep.todo[-1] == (2, 30)
    for _ in range(9):
        sweep.ok()
    assert sweep.next() == (31, 40)
    sweep.refused()  # one id in 31..40 cannot be removed
    assert sweep.todo[:10] == [(i, i) for i in range(40, 30, -1)]
    for i in range(40, 30, -1):
        if i == 31:
            sweep.refused()  # this one stays in the channel
        else:
            sweep.ok()
    assert sweep.next() == (2, 30)
    sweep.ok()
    assert sweep.next() is None
    assert sweep.kept == [31]
    assert sweep.removed_ids() == [i for i in range(2, 131) if i != 31]
    assert sweep.covered == 128


# --- the database ------------------------------------------------------------------------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await wipe(eng)
    yield eng
    await wipe(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


async def seed_posts(w: World) -> dict[str, int]:
    """The panel's records of posts in both channels, with a waiting edit for one test post."""
    ids: dict[str, int] = {}
    for n, message_id in enumerate((3, 4, 5, 900)):
        job = await w.job("send_post", done_at=w.old, payload={"role": "test", "text": "t"})
        ids[f"test_post_{message_id}"] = await w.post(
            role="test",
            created_at=w.old - timedelta(minutes=n),
            slot=n,
            outbox_id=job,
            links=[("mtproto:x", "active")],
        )
        ids[f"test_cp_{message_id}"] = await w.channel_post(
            role="test", chat_id=TEST_CHAT, message_id=message_id, outbox_id=job, created_at=w.old
        )
    main_job = await w.job("send_post", done_at=w.old, payload={"role": "main", "text": "m"})
    ids["main_post"] = await w.post(role="main", created_at=w.old, slot=0, outbox_id=main_job)
    ids["main_cp_3"] = await w.channel_post(
        role="main", chat_id=MAIN_CHAT, message_id=3, outbox_id=main_job, created_at=w.old
    )
    ids["edit_waiting"] = await w.job("edit_post", payload={"post_id": ids["test_cp_4"], "text": "t"})
    ids["edit_other"] = await w.job("edit_post", payload={"post_id": ids["test_cp_900"], "text": "t"})
    ids["main_edit"] = await w.job("edit_post", payload={"post_id": ids["main_cp_3"], "text": "m"})
    return ids


@pytest.mark.db
async def test_the_target_is_only_ever_the_test_channel(engine: AsyncEngine, vault: Vault) -> None:
    w = World(engine)
    await w.telegram(vault, test=False, main=True)
    async with engine.connect() as conn:
        with pytest.raises(tc.Refused) as only_main:
            await tc.target(conn)
    assert only_main.value.code == "test_channel_not_set"
    await w.telegram(vault, test=True, main=True, same=True)
    async with engine.connect() as conn:
        with pytest.raises(tc.Refused) as same:
            await tc.target(conn)
    assert same.value.code == "test_is_main" and "کانال اصلی" in same.value.message_fa
    await w.telegram(vault, test=True, main=True)
    async with engine.connect() as conn:
        t = await tc.target(conn)
    assert (t.test_chat_id, t.main_chat_id) == (TEST_CHAT, MAIN_CHAT)
    await w.telegram(vault, test=True, main=False)
    async with engine.connect() as conn:
        assert (await tc.target(conn)).main_chat_id is None


@pytest.mark.db
async def test_forgetting_removed_posts_touches_only_test_rows_of_removed_ids(
    engine: AsyncEngine, vault: Vault
) -> None:
    w = World(engine)
    await w.telegram(vault)
    ids = await seed_posts(w)
    async with engine.connect() as conn:
        t = await tc.target(conn)
    async with engine.begin() as conn:
        # ids 2..10 were removed (3 also exists in the MAIN channel, under its own chat id)
        counts = await tc.forget_removed(conn, t, list(range(2, 11)), w.now)
    assert counts == {"channel_posts": 3, "posts": 3, "post_links": 3, "edit_jobs_closed": 1}
    left = {r.message_id: r.role for r in await w.rows("SELECT message_id, role FROM channel_posts")}
    assert left == {900: "test", 3: "main"}
    posts = {r.id for r in await w.rows("SELECT id FROM posts")}
    assert posts == {ids["test_post_900"], ids["main_post"]}
    jobs = {r.id: (r.done_at is not None, r.last_error) for r in await w.rows("SELECT * FROM outbox")}
    assert jobs[ids["edit_waiting"]] == (True, None)  # closed: nothing left to edit
    assert jobs[ids["edit_other"]] == (False, None)  # its post is still there
    assert jobs[ids["main_edit"]] == (False, None)  # the main channel is never touched
    async with engine.begin() as conn:
        assert await tc.forget_removed(conn, t, [], w.now) == dict.fromkeys(counts, 0)


@pytest.mark.db
async def test_a_main_chat_id_never_loses_a_row_even_if_passed_as_removed(
    engine: AsyncEngine, vault: Vault
) -> None:
    w = World(engine)
    await w.telegram(vault)
    await seed_posts(w)
    # Even a row recorded with role "test" in the main channel's chat (a channel moved between roles).
    job = await w.job("send_post", done_at=w.old, payload={"role": "test", "text": "t"})
    await w.post(role="test", created_at=w.old, slot=9, outbox_id=job)
    await w.channel_post(role="test", chat_id=MAIN_CHAT, message_id=7, outbox_id=job, created_at=w.old)
    t = tc.Target(test_chat_id=MAIN_CHAT, main_chat_id=MAIN_CHAT, username="x", title="x")
    async with engine.begin() as conn:
        counts = await tc.forget_removed(conn, t, [3, 7], w.now)
    assert counts == {"channel_posts": 0, "posts": 0, "post_links": 0, "edit_jobs_closed": 0}
    assert await w.one("SELECT count(*) FROM channel_posts WHERE chat_id = :c", c=MAIN_CHAT) == 2
    assert await w.one("SELECT count(*) FROM posts WHERE outbox_id = :o", o=job) == 1


async def test_the_sweep_stops_on_a_refused_connection_after_one_retry() -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    t = tc.Target(test_chat_id=TEST_CHAT, main_chat_id=MAIN_CHAT, username="x", title="x")
    sweep = tc.Sweep.over(2, 250)
    async with closed_port_bot()(SecretValue(random_token())) as bot:
        out = await tc.sweep_channel(bot, t, sweep, sleep=sleep)
    assert (out.calls, out.stop) == (2, "telegram_unavailable")
    assert slept == [tc.NETWORK_RETRY_S, tc.CALL_GAP_S]
    assert sweep.done == [] and sweep.removed_ids() == []


async def test_the_sweep_stops_at_telegrams_real_401() -> None:
    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    t = tc.Target(test_chat_id=TEST_CHAT, main_chat_id=MAIN_CHAT, username="x", title="x")
    sweep = tc.Sweep.over(2, 250)
    async with real_bot(random_token()) as bot:
        out = await tc.sweep_channel(bot, t, sweep)
    assert (out.calls, out.stop) == (1, "bot_token_invalid")
    assert sweep.removed_ids() == []


async def test_the_sweep_has_a_budget() -> None:
    t = tc.Target(test_chat_id=TEST_CHAT, main_chat_id=MAIN_CHAT, username="x", title="x")
    sweep = tc.Sweep.over(2, 250)
    async with closed_port_bot()(SecretValue(random_token())) as bot:
        assert (await tc.sweep_channel(bot, t, sweep, max_calls=0)).stop == "budget"
        assert (await tc.sweep_channel(bot, t, sweep, budget_s=0)).stop == "budget"
    assert sweep.todo == [(151, 250), (51, 150), (2, 50)]


@pytest.mark.db
async def test_clean_refuses_before_asking_telegram_and_changes_nothing(
    engine: AsyncEngine, vault: Vault
) -> None:
    w = World(engine)

    async def refused() -> str:
        with pytest.raises(tc.Refused) as caught:
            await tc.clean(engine, vault, closed_port_bot(), actor="admin:owner", sleep=_no_sleep)
        return caught.value.code

    assert await refused() == "bot_not_set"
    await w.telegram(vault, test=False, main=True)
    assert await refused() == "test_channel_not_set"  # only a main channel: never cleaned
    await w.telegram(vault, test=True, main=True, same=True)
    assert await refused() == "test_is_main"
    await w.telegram(vault)
    ids = await seed_posts(w)
    pending = await w.job("send_post", payload={"role": "test", "text": "t"})
    assert await refused() == "send_pending"
    await w.run("UPDATE outbox SET done_at = now() WHERE id = :i", i=pending)
    # Another cleaning holds the lock.
    async with engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key(tc.LOCK_NAME)})
        await other.commit()
        assert await refused() == "cleanup_running"
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key(tc.LOCK_NAME)})
        await other.commit()
    # Telegram cannot be reached (a refused port): the rights cannot be read, nothing starts.
    assert await refused() == "telegram_unavailable"
    assert await w.one("SELECT count(*) FROM channel_posts") == 5
    assert await w.one("SELECT count(*) FROM posts") == 5
    assert await w.one("SELECT count(*) FROM telegram_pacing") == 0  # no hold was set
    assert await w.one("SELECT count(*) FROM actions_log") == 0
    assert ids


@pytest.mark.db
async def test_clean_gets_telegrams_real_401_for_a_random_token(engine: AsyncEngine, vault: Vault) -> None:
    if not telegram_reachable():
        pytest.skip("api.telegram.org is not reachable from here")
    w = World(engine)
    await w.telegram(vault)
    with pytest.raises(tc.Refused) as caught:
        await tc.clean(engine, vault, real_bot, actor="admin:owner", sleep=_no_sleep)
    assert caught.value.code == "bot_token_invalid" and caught.value.status == 400
    assert await w.one("SELECT count(*) FROM telegram_pacing") == 0


@pytest.mark.db
async def test_preview_says_what_blocks_the_cleaning(engine: AsyncEngine, vault: Vault) -> None:
    w = World(engine)
    out = await tc.preview(engine, vault, closed_port_bot())
    assert out["blocked"] == "bot_not_set" and out["blocked_fa"]
    await w.telegram(vault, test=False, main=True)
    out = await tc.preview(engine, vault, closed_port_bot())
    assert (out["blocked"], out["channel"], out["main_channel_set"]) == ("test_channel_not_set", None, True)
    await w.telegram(vault)
    await seed_posts(w)
    await w.run(
        "INSERT INTO analytics_post_views (role, chat_id, message_id, views, forwards, at) "
        "VALUES ('test', :c, 1500, 3, 0, now()), ('main', :m, 99999, 3, 0, now())",
        c=TEST_CHAT,
        m=MAIN_CHAT,
    )
    out = await tc.preview(engine, vault, closed_port_bot())
    assert out["blocked"] == "telegram_unavailable"
    assert out["channel"] == {
        "username": "@ipoxxo_cleanup_test",
        "title": "Proxy-Hub cleanup test",
        "chat_id": TEST_CHAT,
    }
    assert out["message_ids"] == {"first": 2, "last": 1700}  # the highest known id in the TEST chat + 200
    assert out["requests"] == 17
    assert out["rows"] == {"channel_posts": 4, "posts": 4}
    assert out["can_delete"] is None


@pytest.mark.db
async def test_an_edit_of_a_forgotten_post_ends_quietly_without_telegram(
    engine: AsyncEngine, vault: Vault
) -> None:
    """After a cleaning the publisher never queues an edit for a removed post (its rows are gone);
    an edit job that still names one fails as post_not_found before any request (the refused port
    would have made it a network retry)."""
    w = World(engine)
    await w.telegram(vault)
    ids = await seed_posts(w)
    async with engine.connect() as conn:
        t = await tc.target(conn)
    async with engine.begin() as conn:
        await tc.forget_removed(conn, t, [5], w.now)
    await w.run("UPDATE outbox SET done_at = now() WHERE done_at IS NULL")  # only this job is due
    job = await w.job("edit_post", payload={"post_id": ids["test_cp_5"], "text": "t"})
    assert await outbox.process_one(engine, vault, closed_port_bot()) == "idle"
    row = (await w.rows("SELECT done_at, last_error FROM outbox WHERE id = :i", i=job))[0]
    assert row.done_at is not None and row.last_error == "failed: post_not_found"


async def _no_sleep(_: float) -> None:
    return None
