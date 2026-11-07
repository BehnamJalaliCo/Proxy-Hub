"""Breaking news (decision D56) on a real PostgreSQL: what makes a story breaking (young, several
sources, Claude's word), its promotion, and the out-of-plan post, in no-post hours too, within its
budget. Telegram is never answered: posts stop at the outbox, as in test_content_publisher.py."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_content_support import clean_content_tables
from test_posts_publisher import configure, publish, quiet, ready_fleet
from test_posts_support import PROVIDERS, clean_posts_tables, jobs, rows, setup_telegram, vault_at

from proxyhub.content import breaking, candidates, qc_prompt, qc_settings, sources
from proxyhub.content.items import FetchedItem
from proxyhub.content.qc_settings import FLAGS, QcConfig
from proxyhub.posts import links, publisher
from proxyhub.telegram import outbox
from proxyhub.vault import Vault

EXTERNAL = itertools.count(500)
NEWS = (
    "حمله‌ی موشکی به یک پایگاه نظامی در شمال کشور گزارش شد و مقام‌های رسمی آن را تأیید کردند؛ "
    "جزئیات بیشتر هنوز منتشر نشده است و خبرگزاری‌ها از ادامه‌ی بررسی‌ها خبر میدهند."
)
OTHER = (
    "قیمت سکه و دلار در بازار امروز تهران تغییر محسوسی نداشت و معامله‌گران منتظر تصمیم‌های تازه‌ی "
    "بانک مرکزی ماندند؛ کارشناسان نوسان کم را تا پایان هفته پیش‌بینی میکنند."
)


# --- units ------------------------------------------------------------------------------------------


def verdict(**over: Any) -> str:
    data: dict[str, Any] = {
        "decision": "approve",
        "reasons": ["خبر مهم و تأییدشده است."],
        "category": "politics",
        "flags": dict.fromkeys(FLAGS, False),
        "title": "🔻 حمله به پایگاه نظامی",
        "body": "حمله‌ی موشکی به یک پایگاه نظامی در شمال کشور گزارش و تأیید شد.",
        "breaking": True,
    }
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


def test_claude_says_breaking_only_with_an_approval_and_strictly() -> None:
    assert qc_prompt.parse_verdict(verdict()).breaking is True
    assert qc_prompt.parse_verdict(verdict(breaking=False)).breaking is False
    assert qc_prompt.parse_verdict(verdict(decision="reject", title="", body="")).breaking is False
    for bad in (verdict(breaking="yes"), verdict(breaking=None), verdict(breaking=1)):
        with pytest.raises(qc_prompt.VerdictInvalid):
            qc_prompt.parse_verdict(bad)
    without = json.loads(verdict())
    del without["breaking"]
    with pytest.raises(qc_prompt.VerdictInvalid):
        qc_prompt.parse_verdict(json.dumps(without))
    assert "breaking" in qc_prompt.output_schema()["required"]
    prompt = qc_prompt.system_prompt(
        QcConfig(), qc_prompt.limits_for(None, media=False), "n0nce", media_kind=None, image_sent=False
    )
    assert '"breaking": true or false' in prompt and "rumour" in prompt


def test_the_breaking_title() -> None:
    assert breaking.title("🔻 حمله به پایگاه نظامی") == "🔴 فوری | حمله به پایگاه نظامی"
    assert breaking.title("حمله به پایگاه") == "🔴 فوری | حمله به پایگاه"
    assert breaking.title("🇮🇷 خبر ایران") == "🔴 فوری | خبر ایران"  # a flag is one emoji
    assert breaking.title("  ") == "🔴 فوری"


def test_a_story_must_be_young_and_carried_by_several_sources() -> None:
    now = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    config = QcConfig()
    young = breaking.Story(1, now - timedelta(minutes=10), 2)
    assert breaking.qualifies(young, config, now)
    assert not breaking.qualifies(breaking.Story(1, now - timedelta(minutes=10), 1), config, now)
    assert not breaking.qualifies(breaking.Story(1, now - timedelta(minutes=31), 3), config, now)
    assert not breaking.qualifies(young, QcConfig(breaking_min_sources=3), now)
    assert not breaking.qualifies(None, config, now)


def test_out_of_slot_reviews_have_their_own_caps() -> None:
    now = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
    state: dict[str, Any] = {}
    for i in range(breaking.REVIEWS_PER_HOUR):
        assert breaking.review_allowed(state, now + timedelta(minutes=i))
        breaking.note_review(state, now + timedelta(minutes=i))
    assert not breaking.review_allowed(state, now + timedelta(minutes=10))
    assert breaking.review_allowed(state, now + timedelta(minutes=70))  # an hour later
    state[breaking.STATE_KEY] = [(now - timedelta(minutes=50 * i)).isoformat() for i in range(1, 25)]
    assert not breaking.review_allowed(state, now)  # 24 in the last day
    assert not breaking.enabled(QcConfig(mode="review")) and not breaking.enabled(
        QcConfig(breaking_per_day=0)
    )


# --- on the database ------------------------------------------------------------------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_posts_tables(eng)
    await clean_content_tables(eng)
    yield eng
    await clean_posts_tables(eng)
    await clean_content_tables(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return vault_at(tmp_path)


async def source(engine: AsyncEngine, name: str, now: datetime) -> sources.Source:
    async with engine.begin() as conn:
        return await sources.create(conn, kind="telegram", ref=f"@{name}", label=name, now=now)


async def give(engine: AsyncEngine, src: sources.Source, body: str, when: datetime, now: datetime) -> None:
    async with engine.begin() as conn:
        item = FetchedItem(str(next(EXTERNAL)), None, when, body)
        await candidates.store_items(conn, src, [item], now)


async def story_ids(engine: AsyncEngine, now: datetime, config: QcConfig | None = None) -> list[int]:
    async with engine.connect() as conn:
        return await breaking.stories(conn, config or QcConfig(), now)


async def originals(engine: AsyncEngine) -> list[int]:
    found = await rows(engine, "SELECT id FROM content_candidates WHERE duplicate_of IS NULL ORDER BY id")
    return [int(r.id) for r in found]


@pytest.mark.db
async def test_stories_and_promotion(engine: AsyncEngine) -> None:
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    a, b, c = [await source(engine, n, t0) for n in ("news_a", "news_b", "news_c")]
    await give(engine, a, NEWS, t0, t0)
    await give(engine, a, OTHER, t0, t0)
    assert await story_ids(engine, t0) == []  # one source each: never breaking
    await give(
        engine, a, NEWS, t0 + timedelta(minutes=1), t0 + timedelta(minutes=1)
    )  # the same channel again
    assert await story_ids(engine, t0 + timedelta(minutes=2)) == []
    await give(engine, b, NEWS, t0 + timedelta(minutes=3), t0 + timedelta(minutes=3))
    news_id = (await originals(engine))[0]
    assert await story_ids(engine, t0 + timedelta(minutes=4)) == [news_id]
    assert await story_ids(engine, t0 + timedelta(minutes=4), QcConfig(breaking_min_sources=3)) == []
    assert await story_ids(engine, t0 + timedelta(minutes=31)) == []  # older than 30 minutes

    # Claude's word is needed: an approved candidate without it is never promoted.
    now = t0 + timedelta(minutes=4)
    async with engine.begin() as conn:
        await candidates.set_status(conn, news_id, "approved", now)
        assert await breaking.promote(conn, QcConfig(), now) == []
        await conn.execute(
            text("UPDATE content_candidates SET qc_breaking = true WHERE id = :i"), {"i": news_id}
        )
        assert await breaking.promote(conn, QcConfig(mode="review"), now) == []
        assert await breaking.promote(conn, QcConfig(), t0 + timedelta(minutes=40)) == []  # too old by then
        assert await breaking.promote(conn, QcConfig(), now) == [news_id]
        assert await breaking.promote(conn, QcConfig(), now) == []  # once
        found = await breaking.next_to_post(conn, now)
        assert found is not None and found.id == news_id
        assert await breaking.next_to_post(conn, now + breaking.POST_WITHIN + timedelta(minutes=1)) is None
    assert c.id > 0


async def publish_now(engine: AsyncEngine, now: datetime) -> str:
    async with engine.begin() as conn:

        async def harvest() -> links.Harvest:
            return await links.harvest(conn, PROVIDERS)

        return await publisher.publish_breaking(conn, now, harvest)


async def promoted(
    engine: AsyncEngine, now: datetime, body: str = NEWS, title: str = "🔻 حمله به پایگاه"
) -> int:
    a = await source(engine, f"source{next(EXTERNAL)}", now)
    b = await source(engine, f"source{next(EXTERNAL)}", now)
    await give(engine, a, body, now - timedelta(minutes=3), now - timedelta(minutes=3))
    await give(engine, b, body, now - timedelta(minutes=1), now - timedelta(minutes=1))
    cid = max(await originals(engine))
    async with engine.begin() as conn:
        await candidates.edit(conn, cid, title, "حمله‌ی موشکی به یک پایگاه نظامی گزارش و تأیید شد.", now)
        await candidates.set_status(conn, cid, "approved", now)
        await conn.execute(text("UPDATE content_candidates SET qc_breaking = true WHERE id = :i"), {"i": cid})
        assert await breaking.promote(conn, QcConfig(), now) == [cid]
    return cid


@pytest.mark.db
async def test_a_breaking_post_goes_at_once_in_no_post_hours_within_its_budget(
    engine: AsyncEngine, vault: Vault
) -> None:
    await ready_fleet(engine, 3)
    await setup_telegram(engine, vault, main=True)
    await configure(engine, main_enabled=True)
    await quiet(engine, [("00:00", "23:59")], interval=60)  # a no-post window all day
    now = datetime.now(UTC).replace(microsecond=0)
    cid = await promoted(engine, now)
    assert await publish(engine, now) in ("quiet", "not_due")  # the day plan waits
    assert await publish_now(engine, now) == "queued"
    (job,) = await jobs(engine, "send_post")
    assert job.payload["breaking"] is True and job.payload["role"] == "main"
    assert job.payload["text"].startswith("<b>🔴 فوری | حمله به پایگاه</b>")
    (post,) = await rows(engine, "SELECT breaking, plan_slot, content_candidate_id FROM posts")
    assert (
        post.breaking is True
        and post.plan_slot >= publisher.BREAKING_SLOT_BASE
        and post.content_candidate_id == cid
    )
    assert (await rows(engine, "SELECT status FROM content_candidates WHERE id = :i", i=cid))[
        0
    ].status == "used"
    async with engine.connect() as conn:  # not one of the day plan's posts
        assert await publisher.posts_on(conn, "main", now.astimezone(publisher.TEHRAN).date()) == 0
    log = (await rows(engine, "SELECT reasons FROM actions_log WHERE action = 'posts.queued'"))[0].reasons
    assert log["reasons"] == ["breaking"] and log["context"]["breaking"] is True

    # The outbox sends it in the no-post window; an ordinary post to the main channel waits.
    async with engine.begin() as conn:
        picked = await outbox.pick(conn, now + timedelta(seconds=30))  # queued at the database's clock
    assert picked is not None and picked[0].id == job.id
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE outbox SET done_at = :t WHERE id = :i"), {"t": now, "i": job.id})

    # The budget: the gap between two, the day's count, the switch.
    second = await promoted(engine, now, body=OTHER, title="🔻 خبر دوم")
    assert await publish_now(engine, now + timedelta(minutes=2)) == "gap"
    assert await publish_now(engine, now + timedelta(minutes=10)) == "gap"
    async with engine.begin() as conn:
        await qc_settings.save(conn, QcConfig(breaking_per_day=1))
    assert await publish_now(engine, now + timedelta(minutes=25)) == "day_full"
    async with engine.begin() as conn:
        await qc_settings.save(conn, QcConfig(breaking_enabled=False))
    assert await publish_now(engine, now + timedelta(minutes=25)) == "off"
    async with engine.begin() as conn:
        await qc_settings.save(conn, QcConfig())
        # Past its hour, a promoted item is an ordinary approved one for the next slot.
        assert await breaking.next_to_post(conn, now + breaking.POST_WITHIN + timedelta(minutes=5)) is None
        found = await candidates.next_for_post(conn, now + timedelta(minutes=25))
        assert found is not None and found.id == second
    assert await publish_now(engine, now + timedelta(minutes=25)) == "queued"
    assert len(await jobs(engine, "send_post")) == 2
