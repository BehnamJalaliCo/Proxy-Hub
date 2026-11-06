"""An approved candidate becomes the next post (decision D51), on a real PostgreSQL: a photo or video
with the content and the links as its caption (within 1024 characters, for both posters), a status
edit of that caption, the text fallbacks, and the outboxes' media paths.

Links come from the posts team's test providers (test_posts_support.py: real tables). Telegram is
never answered here: the bot's requests go to a local port nobody listens on, so the outbox records
which Bot API method it tried (a refused connection: nothing was sent, the job is retried).
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_content_support import clean_content_tables, mp4, png
from test_posts_publisher import configure, publish, quiet, ready_fleet, refresh, slot_time
from test_posts_support import (
    PROVIDERS,
    clean_posts_tables,
    deliver,
    jobs,
    rows,
    set_node,
    setup_telegram,
    vault_at,
)
from test_telegram_support import closed_port_bot

from proxyhub.content import candidates, sources
from proxyhub.content.items import FetchedItem, MediaRef
from proxyhub.content.media import check_for
from proxyhub.posts import links, operator, operator_outbox, publisher
from proxyhub.posts.poster import BOT, OPERATOR, Poster
from proxyhub.telegram import outbox
from proxyhub.telegram.templates import CAPTION_LIMIT, utf16_len, visible_text
from proxyhub.vault import Vault

pytestmark = pytest.mark.db
EXTERNAL = itertools.count(100)


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


async def approved(
    engine: AsyncEngine,
    *,
    kind: str | None = "photo",
    title: str = "🔻 هشدار عراق درباره درگیری",
    body: str = "وزیر خارجه عراق گفت ورود گروه‌های مسلح به درگیری، عراق را به «تونلی تاریک» میکشاند.",
    failed: bool = False,
) -> tuple[int, int | None]:
    """An approved candidate (with its photo or video downloaded). Returns (candidate id, media id)."""
    when = slot_time(0) - timedelta(minutes=30)
    async with engine.begin() as conn:
        found = await sources.list_all(conn)
        source = (
            found[0]
            if found
            else await sources.create(conn, kind="telegram", ref="@bbcpersian", label="BBC", now=when)
        )
        ref = MediaRef(kind=kind, url=f"https://cdn.example/{kind}") if kind else None  # type: ignore[arg-type]
        external = str(next(EXTERNAL))
        await candidates.store_items(
            conn, source, [FetchedItem(external, None, when, f"{title}\n{body}", media=ref)], when
        )
        (candidate,) = await candidates.queue(conn, "pending", "new", when)
        media_id = None
        if kind is not None:
            (task,) = [
                t for t in await candidates.media_tasks(conn, 10) if t.candidate_id == candidate.id
            ] or [candidates.MediaTask(candidate.id, "video", "https://cdn.example/video")]
            if failed:
                await candidates.media_failed(conn, task, "blocked_address", when, final=True)
            else:
                data = png(32, 24) if kind == "photo" else mp4(640, 360, 12)
                info = check_for("photo" if kind == "photo" else "video", data)
                media_id = await candidates.save_media(conn, task, info, data, when)
        await candidates.edit(conn, candidate.id, title, body, when)
        await candidates.set_status(conn, candidate.id, "approved", when)
    return candidate.id, media_id


async def seeds_used(engine: AsyncEngine) -> int:
    return int((await rows(engine, "SELECT count(*) AS n FROM content_items WHERE used_count > 0"))[0].n)


def visible(html_text: str) -> int:
    return utf16_len(visible_text(html_text))


async def test_an_approved_photo_goes_first_with_its_caption_and_edits_change_the_caption(
    engine: AsyncEngine, vault: Vault
) -> None:
    nodes = await ready_fleet(engine, 3)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    cid, media_id = await approved(engine)
    now = slot_time(1)
    assert await publish(engine, now) == "queued"
    job = (await jobs(engine, "send_post"))[-1]
    assert job.payload["media"] == {
        "kind": "photo",
        "media_id": media_id,
        "width": 32,
        "height": 24,
        "duration": None,
    }
    caption = job.payload["text"]
    assert caption.startswith("<b>🔻 هشدار عراق درباره درگیری</b>\n\n<blockquote>وزیر خارجه عراق")
    assert visible(caption) <= CAPTION_LIMIT
    assert caption.count("https://t.me/proxy?") == 3 and "vless://" in caption and caption.endswith("@ipoxxo")
    (post,) = await rows(engine, "SELECT id, media_kind, content_candidate_id, content_item_id FROM posts")
    assert (post.media_kind, post.content_candidate_id, post.content_item_id) == ("photo", cid, None)
    assert (await rows(engine, "SELECT status FROM content_candidates WHERE id = :i", i=cid))[
        0
    ].status == "used"
    assert await seeds_used(engine) == 0  # the old content bank was not touched
    log = (await rows(engine, "SELECT reasons FROM actions_log WHERE action = 'posts.queued'"))[0].reasons
    assert log["context"]["media"] == "photo" and log["context"]["content_candidate_id"] == cid

    await deliver(engine, job.id, message_id=100, at=now + timedelta(seconds=5))
    await set_node(engine, nodes[0], state="burned")
    assert await refresh(engine, now + timedelta(minutes=20)) == 1
    edit = (await jobs(engine, "edit_post"))[-1]
    assert edit.payload["caption"] is True and edit.payload["post_id"] > 0
    assert "🚫" in edit.payload["text"] and visible(edit.payload["text"]) <= CAPTION_LIMIT
    assert edit.payload["text"].startswith("<b>🔻 هشدار عراق درباره درگیری</b>")  # the content stays


async def test_the_links_always_fit_the_caption_and_the_content_gives_way(
    engine: AsyncEngine, vault: Vault
) -> None:
    await ready_fleet(engine, 5)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    await configure(engine, max_reality=5)
    long_body = "متن بلند خبر. " * 70  # far more than a caption can hold
    await approved(engine, body=long_body[:1000])
    assert await publish(engine, slot_time(1)) == "queued"
    job = (await jobs(engine, "send_post"))[-1]
    caption = job.payload["text"]
    assert "media" in job.payload and visible(caption) <= CAPTION_LIMIT
    assert caption.count("https://t.me/proxy?") == 5  # every proxy word stays
    links = await rows(engine, "SELECT kind FROM post_links ORDER BY position")
    kept = sum(1 for r in links if r.kind == "reality")
    dropped = await rows(
        engine, "SELECT reasons FROM actions_log WHERE action = 'posts.caption_links_dropped'"
    )
    if kept < 5:  # five configs do not fit 1024 characters: the last ones went, and the log says so
        context = dropped[0].reasons["context"]
        assert context["caption_limit"] == CAPTION_LIMIT
        assert [d["kind"] for d in context["dropped"]] == ["reality"] * (5 - kept)
    assert caption.count("vless://") == kept >= 1
    content = caption.split("\n\n", 1)[1].split("</blockquote>")[0]
    assert content.endswith("…")  # the body was shortened, not a link


async def test_text_fallbacks_without_media(engine: AsyncEngine, vault: Vault) -> None:
    await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    plain, _ = await approved(engine, kind=None, title="📰 خبر بی عکس")
    assert await publish(engine, slot_time(1)) == "queued"
    job = (await jobs(engine, "send_post"))[-1]
    assert "media" not in job.payload and job.payload["text"].startswith("<b>📰 خبر بی عکس</b>")
    await deliver(engine, job.id, message_id=1, at=slot_time(1))
    failed, _ = await approved(
        engine,
        kind="photo",
        title="📰 عکسش نیامد",
        body="عکس این خبر از منبع نرسید و پست بی عکس رفت تا کانال خالی نماند و خبر هم به موقع برسد.",
        failed=True,
    )
    assert await publish(engine, slot_time(2)) == "queued"
    job = (await jobs(engine, "send_post"))[-1]
    assert "media" not in job.payload and "عکسش نیامد" in job.payload["text"]
    await deliver(engine, job.id, message_id=2, at=slot_time(2))
    skipped = await rows(engine, "SELECT reasons FROM actions_log WHERE action = 'posts.media_skipped'")
    assert skipped[0].reasons["reasons"] == ["media_failed"]
    assert {r.content_candidate_id for r in await rows(engine, "SELECT content_candidate_id FROM posts")} == {
        plain,
        failed,
    }
    assert {r.media_kind for r in await rows(engine, "SELECT media_kind FROM posts")} == {None}
    # with no approved candidate left, the old content bank is the fallback, as before
    assert await publish(engine, slot_time(3)) == "queued"
    assert await seeds_used(engine) == 1


async def test_the_bot_outbox_uploads_the_photo_and_edits_the_caption(
    engine: AsyncEngine, vault: Vault
) -> None:
    await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    await approved(engine, kind="video")
    now = slot_time(1)
    assert await publish(engine, now) == "queued"
    job = (await jobs(engine, "send_post"))[-1]
    assert job.payload["media"]["kind"] == "video" and job.payload["media"]["duration"] == 12
    # The real outbox step: the bot's request goes to a refused local port (nothing sent).
    assert await outbox.process_one(engine, vault, closed_port_bot()) == "retry"
    (row,) = await rows(engine, "SELECT last_error, done_at FROM outbox WHERE id = :i", i=job.id)
    assert row.last_error.startswith("sendVideo: network error") and row.done_at is None
    # Its file gone: the job fails before anything is sent.
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM content_media"))
        await conn.execute(text("UPDATE outbox SET not_before = now() - interval '1 minute'"))
        await conn.execute(text("DELETE FROM telegram_pacing"))
    assert await outbox.process_one(engine, vault, closed_port_bot()) == "failed"
    (row,) = await rows(engine, "SELECT last_error FROM outbox WHERE id = :i", i=job.id)
    assert row.last_error == "failed: media_missing"


async def test_a_caption_edit_goes_as_editmessagecaption(engine: AsyncEngine, vault: Vault) -> None:
    await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    await approved(engine)
    now = slot_time(1)
    assert await publish(engine, now) == "queued"
    send = (await jobs(engine, "send_post"))[-1]
    await deliver(engine, send.id, message_id=100, at=now)
    cp = (await rows(engine, "SELECT id FROM channel_posts"))[0].id
    async with engine.begin() as conn:
        edit = await BOT.queue_edit(conn, channel_post_id=cp, text_html="<b>📡 x</b>", caption=True)
        await conn.execute(text("DELETE FROM telegram_pacing"))
    assert await outbox.process_one(engine, vault, closed_port_bot()) == "retry"
    (row,) = await rows(engine, "SELECT last_error FROM outbox WHERE id = :i", i=edit)
    assert row.last_error.startswith("editMessageCaption: network error")


async def test_a_media_post_the_operator_cannot_send_goes_to_the_bot_unchanged(
    engine: AsyncEngine, vault: Vault
) -> None:
    await ready_fleet(engine, 2)
    await setup_telegram(engine, vault)
    await quiet(engine, [], interval=0)
    await approved(engine)
    assert await publish_with(engine, slot_time(1), OPERATOR) == "queued"
    job = (await jobs(engine))[-1]
    payload = dict(job.payload)
    view = operator_outbox.Job(id=job.id, kind=job.kind, payload=payload, attempts=0)
    async with engine.begin() as conn:
        if job.kind == operator.SEND:  # the account is not connected here: the bot took it already
            target = operator_outbox.Target(role="test", chat_id=-1009876543210, username=None)
            assert (
                await operator_outbox.hand_to_bot(conn, view, target, "not_connected", slot_time(1))
                == "handed_to_bot"
            )
    (after,) = [j for j in await jobs(engine) if j.id == job.id]
    assert after.kind == "send_post" and after.payload == payload and "media" in after.payload
    (post,) = await rows(engine, "SELECT poster, media_kind FROM posts")
    assert (post.poster, post.media_kind) == ("bot", "photo")


async def publish_with(engine: AsyncEngine, now: datetime, poster: Poster) -> str:
    async with engine.begin() as conn:

        async def harvest() -> links.Harvest:
            return await links.harvest(conn, PROVIDERS)

        return await publisher.publish_due(conn, now, harvest, poster)
