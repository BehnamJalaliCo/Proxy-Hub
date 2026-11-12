# ruff: noqa: E501
"""The content engine on a real PostgreSQL (decision D51): migration 0027, sources, a full round over
a real local HTTPS site (feeds, photos, videos), de-duplication, the owner's review, expiry and
clean-up. Sources here are RSS feeds on the local site (proxyhub/content/rss.py, the real plugin);
the Telegram preview plugin's parsing is tested in test_content_units.py and on the real t.me in
test_content_real.py."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_content_support import LocalSite, Page, clean_content_tables, mp4, png, site_http

from proxyhub.content import candidates, sources
from proxyhub.content.candidates import CandidateInvalid
from proxyhub.content.engine import Engine
from proxyhub.content.items import FetchedItem, MediaRef, SourceInvalid
from proxyhub.content.rss import RssFeed

pytestmark = pytest.mark.db

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_content_tables(eng)
    yield eng
    await clean_content_tables(eng)
    await eng.dispose()


def feed(*items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
        "<channel><title>t</title>" + "".join(items) + "</channel></rss>"
    ).encode()


def item(ident: str, title: str, body: str, when: datetime, extra: str = "") -> str:
    return (
        f"<item><title>{title}</title><description><![CDATA[{body}]]></description>"
        f"<link>https://news.example/{ident}</link><guid>{ident}</guid>"
        f"<pubDate>{format_datetime(when)}</pubDate>{extra}</item>"
    )


NEWS = (
    "وزیر خارجه عراق نسبت به ورود گروه‌های مسلح نزدیک به ایران به هرگونه درگیری نظامی احتمالی میان "
    "ایران و آمریکا هشدار داد و گفت چنین اقدامی عراق را به تونلی تاریک می‌کشاند. @source_channel"
)
SUNSET = (
    "غروب آفتاب در خلیج فارس از نگاه یک عکاس محلی که این تصویر را برای همه فرستاده است و "
    "رنگ‌های آسمان در آن بسیار دیدنی است."
)
OTHER_NEWS = (
    "تیم ملی فوتبال ایران در دیدار دوستانه برابر ازبکستان با دو گل به پیروزی رسید و سرمربی تیم از "
    "عملکرد بازیکنان جوان و تازه‌وارد راضی بود."
)


async def add(engine: AsyncEngine, ref: str, label: str | None = None, kind: str = "rss") -> sources.Source:
    async with engine.begin() as conn:
        return await sources.create(conn, kind=kind, ref=ref, label=label, now=T0)


def make_engine(engine: AsyncEngine, site: LocalSite, clock: list[datetime]) -> Engine:
    return Engine(
        engine=engine,
        clock=lambda: clock[0],
        http_factory=lambda: site_http(site),
        plugins={"rss": RssFeed()},
    )


async def all_candidates(engine: AsyncEngine) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = (await conn.execute(text("SELECT * FROM content_candidates ORDER BY id"))).mappings().all()
    return {str(r["external_id"]): r for r in found}


# --- migration 0027 -------------------------------------------------------------------------------


async def test_migration_0027_round_trip_checks_and_no_drift(migrated_db: str) -> None:
    from alembic import command

    from proxyhub.cli import _alembic_config, downgrade, migrate

    engine = create_async_engine(migrated_db)
    try:
        async with engine.begin() as conn:
            source = (
                await conn.execute(
                    text(
                        "INSERT INTO content_sources (kind, ref, label) VALUES ('rss', 'https://a.example/f', 'a') RETURNING id"
                    )
                )
            ).scalar_one()
            insert = (
                "INSERT INTO content_candidates (source_id, source_kind, source_label, external_id, status, "
                "media_state, created_at, updated_at) VALUES (:s, 'rss', 'a', :e, :st, :ms, now(), now())"
            )
            await conn.execute(text(insert), {"s": source, "e": "1", "st": "pending", "ms": "none"})
            for constraint, values in (
                ("uq_content_candidates_source_external", {"e": "1", "st": "pending", "ms": "none"}),
                ("ck_content_candidates_status", {"e": "2", "st": "posted", "ms": "none"}),
                ("ck_content_candidates_media_state", {"e": "3", "st": "pending", "ms": "lost"}),
            ):
                with pytest.raises(IntegrityError, match=constraint):
                    async with conn.begin_nested():
                        await conn.execute(text(insert), {"s": source, **values})
            with pytest.raises(IntegrityError, match="ck_content_sources_kind"):
                async with conn.begin_nested():
                    await conn.execute(
                        text("INSERT INTO content_sources (kind, ref, label) VALUES ('instagram', 'x', 'x')")
                    )
            with pytest.raises(IntegrityError, match="ck_content_media_size"):
                async with conn.begin_nested():
                    await conn.execute(
                        text(
                            "INSERT INTO content_media (candidate_id, role, mime, size, sha256, data) "
                            "SELECT id, 'main', 'image/png', 99, 'x', '\\x00'::bytea FROM content_candidates"
                        )
                    )
        await asyncio.to_thread(downgrade, "0026_post_link_operator")
        async with engine.connect() as conn:
            tables = (
                (await conn.execute(text("SELECT tablename FROM pg_tables WHERE tablename LIKE 'content_%'")))
                .scalars()
                .all()
            )
            assert sorted(tables) == ["content_items"]
            cols = (
                (
                    await conn.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'posts'")
                    )
                )
                .scalars()
                .all()
            )
            assert "media_kind" not in cols and "content_candidate_id" not in cols
        await asyncio.to_thread(migrate, "head")
        await asyncio.to_thread(command.check, _alembic_config())  # the models and the migrations agree
    finally:
        await asyncio.to_thread(migrate, "head")
        await clean_content_tables(engine)
        await engine.dispose()


# --- sources --------------------------------------------------------------------------------------


async def test_sources_are_checked_unique_and_capped(engine: AsyncEngine) -> None:
    tg = await add(engine, "https://t.me/s/BBCPersian", kind="telegram")
    assert (tg.kind, tg.ref, tg.label, tg.link) == (
        "telegram",
        "bbcpersian",
        "@bbcpersian",
        "https://t.me/bbcpersian",
    )
    rss = await add(engine, "https://News.example/feed.xml", "خبرگزاری")
    assert (rss.ref, rss.label) == ("https://news.example/feed.xml", "خبرگزاری")
    async with engine.begin() as conn:
        with pytest.raises(sources.SourceExists):
            await sources.create(conn, kind="telegram", ref="@bbcpersian", label=None, now=T0)
        for kind, ref, label, code in (
            ("instagram", "x", None, "kind_invalid"),
            ("telegram", "https://evil.example/x", None, "telegram_name_invalid"),
            ("rss", "http://news.example/f", None, "feed_url_invalid"),
            ("rss", "https://192.168.1.1/f", None, "feed_url_invalid"),
            ("rss", "https://news.example/f2", "x" * 81, "label_invalid"),
            ("rss", "https://news.example/f2", "a\x00b", "label_invalid"),
        ):
            with pytest.raises(SourceInvalid, match=code):
                await sources.create(conn, kind=kind, ref=ref, label=label, now=T0)
        off = await sources.update_source(conn, tg.id, enabled=False, label="بی‌بی‌سی", now=T0)
        assert off is not None and not off.enabled and off.label == "بی‌بی‌سی"
        assert [s.id for s in await sources.due(conn, T0, 10)] == [rss.id]  # a disabled source is not read
        for n in range(sources.MAX_SOURCES - 2):
            await sources.create(conn, kind="rss", ref=f"https://news.example/f{n}", label=None, now=T0)
        with pytest.raises(sources.TooManySources):
            await sources.create(conn, kind="rss", ref="https://news.example/one-more", label=None, now=T0)


async def test_backoff_doubles_up_to_six_hours() -> None:
    assert [sources.backoff(n).total_seconds() / 60 for n in (1, 2, 3, 4, 8, 20)] == [5, 10, 20, 40, 360, 360]


# --- a full round over a real local HTTPS site ----------------------------------------------------


async def test_a_round_reads_feeds_stores_candidates_and_downloads_photos(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    photo = png(32, 24, seed=1)
    async with LocalSite(tmp_path) as site:
        site.pages[("news.example", "/a.xml")] = Page(
            body=feed(
                item("1", "عراق", NEWS, T0 - timedelta(hours=1), '<enclosure url="https://cdn.example/1.png" type="image/png"/>'),
                item("2", "فوتبال", OTHER_NEWS, T0 - timedelta(hours=2),
                     '<media:content url="https://cdn.example/2.mp4" type="video/mp4"/><media:thumbnail url="https://cdn.example/2.png"/>'),
                item("old", "کهنه", OTHER_NEWS + " دیروز", T0 - timedelta(days=3)),
                item("3", "ناامن", "عکس از شبکه‌ی داخلی", T0, '<enclosure url="https://evil.example/x.png" type="image/png"/>'),
            ),
            headers={"Content-Type": "application/rss+xml"},
        )  # fmt: skip
        site.pages[("news.example", "/b.xml")] = Page(
            body=feed(item("9", "همان خبر", "🔴 فوری | " + NEWS, T0 - timedelta(minutes=30))),
            headers={"Content-Type": "text/xml"},
        )
        site.pages[("cdn.example", "/1.png")] = Page(body=photo, headers={"Content-Type": "image/png"})
        site.pages[("cdn.example", "/2.png")] = Page(
            body=png(16, 9, seed=2), headers={"Content-Type": "image/png"}
        )
        site.pages[("cdn.example", "/2.mp4")] = Page(
            body=mp4(640, 360, 12), headers={"Content-Type": "video/mp4"}
        )
        a = await add(engine, "https://news.example/a.xml", "الف")
        b = await add(engine, "https://news.example/b.xml", "ب")
        broken = await add(engine, "https://news.example/missing.xml", "خراب")
        clock = [T0]
        eng = make_engine(engine, site, clock)
        result = await eng.run_round()
        assert result.read == {a.id: "ok:3", b.id: "ok:0", broken.id: "http_404"}
        found = await all_candidates(engine)
        assert set(found) == {"1", "2", "3", "9"}  # the 3-day-old entry is not kept
        one, two, three, dup = found["1"], found["2"], found["3"], found["9"]
        assert one["status"] == "pending" and one["title"] == "عراق"
        assert "@source_channel" not in one["body"] and one["body"].startswith("وزیر خارجه عراق")
        assert (dup["status"], dup["duplicate_of"]) == ("duplicate", one["id"])  # same news, other source
        assert one["media_state"] == "ready" and two["media_state"] == "waiting"  # a video waits for approval
        assert (three["media_state"], three["media_error"]) == ("failed", "blocked_address")
        async with engine.connect() as conn:
            media = await candidates.media_rows(conn, [one["id"], two["id"]])
            assert media[one["id"]]["main"].mime == "image/png" and media[one["id"]]["main"].width == 32
            assert media[two["id"]]["thumb"].width == 16 and "main" not in media[two["id"]]
            assert await candidates.media_data(conn, media[one["id"]]["main"].id) == ("image/png", photo)
            assert (await candidates.also_in(conn, [one["id"]])) == {one["id"]: ["ب"]}
            state = {s.id: s for s in await sources.list_all(conn)}
        assert state[a.id].last_error is None and state[a.id].next_fetch_at == T0 + timedelta(minutes=5)
        assert state[broken.id].last_error == "http_404" and state[broken.id].failures == 1
        assert ("evil.example", "/x.png") not in {(h, p) for h, p, _ in site.requests}

        # Nothing is due a minute later; the broken source waits its back-off.
        clock[0] = T0 + timedelta(minutes=1)
        assert (await eng.run_round()).read == {}
        # Approved: the video is downloaded at the next round and checked as MP4.
        async with engine.begin() as conn:
            await candidates.set_status(conn, two["id"], "approved", clock[0])
        result = await eng.run_round()
        assert result.media == {two["id"]: "ok"}
        async with engine.connect() as conn:
            got = await candidates.get(conn, two["id"])
            assert got is not None and got.media_state == "ready"
            row = await candidates.post_media(conn, got)
            assert row is not None and (row.mime, row.duration, row.width) == ("video/mp4", 12, 640)

        # A later read only refreshes what it knew; the same photo from another source is a duplicate.
        clock[0] = T0 + timedelta(minutes=6)
        site.pages[("news.example", "/b.xml")] = Page(
            body=feed(
                item(
                    "10",
                    "عکس روز",
                    SUNSET,
                    T0,
                    '<enclosure url="https://cdn.example/1.png" type="image/png"/>',
                )
            ),
            headers={"Content-Type": "text/xml"},
        )
        await eng.run_round()
        ten = (await all_candidates(engine))["10"]
        assert (ten["status"], ten["duplicate_of"]) == ("duplicate", one["id"])


async def test_a_long_link_or_a_failing_store_never_stops_the_round(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's promise: one source never holds up the others, an unknown failure is recorded
    as ``internal``. A link longer than its column is stored as no link; a store that fails records
    the error on its source and the round goes on (other sources, then the media)."""
    long_link = "https://news.example/" + "خ" * 180  # 201 characters in a feed, 1101 once encoded
    async with LocalSite(tmp_path) as site:
        site.pages[("news.example", "/a.xml")] = Page(
            body=feed(
                f"<item><title>طولانی</title><description>{NEWS}</description><link>{long_link}</link>"
                f"<guid>long</guid><pubDate>{format_datetime(T0)}</pubDate></item>"
            ),
            headers={"Content-Type": "application/rss+xml"},
        )
        site.pages[("news.example", "/b.xml")] = Page(
            body=feed(
                item(
                    "b1",
                    "فوتبال",
                    OTHER_NEWS,
                    T0,
                    '<enclosure url="https://cdn.example/b.png" type="image/png"/>',
                )
            ),
            headers={"Content-Type": "application/rss+xml"},
        )
        site.pages[("cdn.example", "/b.png")] = Page(
            body=png(8, 8, seed=7), headers={"Content-Type": "image/png"}
        )
        a = await add(engine, "https://news.example/a.xml", "الف")
        b = await add(engine, "https://news.example/b.xml", "ب")
        clock = [T0]
        eng = make_engine(engine, site, clock)
        result = await eng.run_round()
        assert result.read == {a.id: "ok:1", b.id: "ok:1"}
        found = await all_candidates(engine)
        assert found["long"]["url"] is None and found["long"]["status"] == "pending"
        async with engine.connect() as conn:
            state = {s.id: s for s in await sources.list_all(conn)}
        assert all(
            s.last_error is None and s.next_fetch_at == T0 + timedelta(minutes=5) for s in state.values()
        )

        # The first source's store fails (a bug of ours): it is recorded, the second is still read.
        real_store = candidates.store_items

        async def store(db: Any, source: sources.Source, items: Any, now: datetime) -> candidates.Stored:
            if source.id == a.id:
                raise RuntimeError("boom")
            return await real_store(db, source, items, now)

        monkeypatch.setattr(candidates, "store_items", store)
        site.pages[("news.example", "/b.xml")] = Page(
            body=feed(
                item(
                    "b2",
                    "عکس روز",
                    SUNSET,
                    T0,
                    '<enclosure url="https://cdn.example/b2.png" type="image/png"/>',
                )
            ),
            headers={"Content-Type": "application/rss+xml"},
        )
        site.pages[("cdn.example", "/b2.png")] = Page(
            body=png(8, 8, seed=8), headers={"Content-Type": "image/png"}
        )
        clock[0] = T0 + timedelta(minutes=5)
        result = await eng.run_round()
        assert result.read == {a.id: "internal", b.id: "ok:1"}
        b2 = (await all_candidates(engine))["b2"]
        assert result.media == {b2["id"]: "ok"}  # the media loop still ran
        async with engine.connect() as conn:
            state = {s.id: s for s in await sources.list_all(conn)}
        assert (state[a.id].last_error, state[a.id].failures) == ("internal", 1)
        assert state[a.id].next_fetch_at == clock[0] + sources.backoff(1)
        assert state[b.id].last_error is None


async def test_an_unknown_download_error_costs_a_try_not_the_round(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """A download that fails in a way the panel does not know (a bug, a library's own error) counts
    as one of the file's 3 tries, and the files after it are still downloaded."""
    source = await add(engine, "https://news.example/a.xml")
    async with engine.begin() as conn:
        await candidates.store_items(
            conn,
            source,
            [
                FetchedItem("old", None, T0 - timedelta(hours=1), OTHER_NEWS, media=MediaRef("photo", "https://cdn.example/old.png")),
                FetchedItem("new", None, T0, NEWS, media=MediaRef("photo", "https://cdn.example/bad.png")),
            ],
            T0 - timedelta(minutes=1),
        )  # fmt: skip
    async with LocalSite(tmp_path) as site:
        site.pages[("cdn.example", "/old.png")] = Page(
            body=png(8, 8, seed=3), headers={"Content-Type": "image/png"}
        )
        clock = [T0]
        eng = make_engine(engine, site, clock)
        real = eng.http_factory

        class Failing:
            def __init__(self) -> None:
                self.inner = real()

            async def __aenter__(self) -> Failing:
                await self.inner.__aenter__()
                return self

            async def __aexit__(self, *exc: object) -> None:
                await self.inner.__aexit__(None, None, None)

            async def get(self, url: str, **kwargs: Any) -> Any:
                if url.endswith("/bad.png"):
                    raise ValueError("not a FetchError")
                return await self.inner.get(url, **kwargs)

        eng.http_factory = Failing  # type: ignore[assignment]
        found = await all_candidates(engine)
        old, new = found["old"]["id"], found["new"]["id"]
        result = await eng.run_round()
        assert result.media == {new: "internal", old: "ok"}
        for n in (2, 3):
            clock[0] = T0 + timedelta(minutes=n)
            assert (await eng.run_round()).media == {new: "internal"}
        bad = (await all_candidates(engine))["new"]
        assert (bad["media_state"], bad["media_tries"], bad["media_error"]) == ("failed", 3, "internal")
        clock[0] = T0 + timedelta(minutes=4)
        assert (await eng.run_round()).media == {}


async def _photo_item(
    engine: AsyncEngine, source: sources.Source, ext: str, body: str, data: bytes, at: datetime
) -> int:
    """A candidate with a photo, its download saved (the engine's own path)."""
    from proxyhub.content.media import check_for

    async with engine.begin() as conn:
        await candidates.store_items(
            conn, source, [FetchedItem(ext, None, at, body, media=MediaRef("photo", f"https://cdn.example/{ext}.png"))], at
        )  # fmt: skip
        cid = int(
            (
                await conn.execute(
                    text("SELECT id FROM content_candidates WHERE source_id = :s AND external_id = :e"),
                    {"s": source.id, "e": ext},
                )
            ).scalar_one()
        )
        task = candidates.MediaTask(cid, "photo", f"https://cdn.example/{ext}.png")
        await candidates.save_media(conn, task, check_for("photo", data), data, at)
    return cid


async def _status(engine: AsyncEngine, cid: int) -> tuple[str, int | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, duplicate_of FROM content_candidates WHERE id = :i"), {"i": cid}
            )
        ).one()
    return str(row.status), row.duplicate_of


async def test_a_posted_photo_is_still_known_after_its_file_is_gone(engine: AsyncEngine) -> None:
    """The «same photo» gate (D52): a forward of a posted photo with a short caption (too short for
    the text gate) is still a duplicate after the posted file was removed."""
    a = await add(engine, "https://a.example/f.xml", "الف")
    b = await add(engine, "https://b.example/f.xml", "ب")
    photo = png(20, 20, seed=11)
    first = await _photo_item(engine, a, "1", "عکس روز از تهران", photo, T0)
    async with engine.begin() as conn:
        await candidates.set_status(conn, first, "approved", T0)
        await candidates.mark_used(conn, first, T0)
        assert await candidates.drop_media(conn, T0 + timedelta(hours=2)) == 1
        assert await candidates.media_bytes_total(conn) == 0
    copy = await _photo_item(engine, b, "9", "عکس روز از تهران", photo, T0 + timedelta(hours=3))
    assert await _status(engine, copy) == ("duplicate", first)


async def test_a_sources_own_banner_on_other_news_is_not_a_duplicate(engine: AsyncEngine) -> None:
    source = await add(engine, "https://a.example/f.xml", "الف")
    other = await add(engine, "https://b.example/f.xml", "ب")
    banner = png(20, 20, seed=12)
    first = await _photo_item(engine, source, "1", NEWS, banner, T0)
    second = await _photo_item(engine, source, "2", OTHER_NEWS, banner, T0 + timedelta(minutes=5))
    assert await _status(engine, second) == ("pending", None)  # the channel's banner, other news
    repost = await _photo_item(engine, source, "3", "عکس", banner, T0 + timedelta(minutes=6))
    assert await _status(engine, repost) == ("pending", None)
    # A repost with the same words (none here) is a duplicate; so is the same photo from another source.
    again = await _photo_item(engine, source, "4", "عکس", banner, T0 + timedelta(minutes=7))
    assert await _status(engine, again) == ("duplicate", repost)
    carried = await _photo_item(
        engine, other, "5", "متن دیگری درباره‌ی همین عکس", banner, T0 + timedelta(minutes=8)
    )
    assert await _status(engine, carried) == ("duplicate", first)
    async with engine.connect() as conn:
        assert await candidates.also_in(conn, [first, repost]) == {
            first: ["ب"]
        }  # its own repost is not «another source»


UNDATED = [
    "بارش برف سنگین در استان‌های شمالی راه چند روستا را بست و نیروهای امدادی اعزام شدند",
    "پژوهشگران دانشگاه از ساخت باتری تازه‌ای خبر دادند که شارژ آن تنها ده دقیقه طول میکشد",
    "یک فیلم کوتاه ایرانی در جشنواره‌ی برلین جایزه‌ی ویژه‌ی هیئت داوران را از آن خود کرد",
    "بانک مرکزی اعلام کرد نرخ تورم نقطه‌ای برای دومین ماه پیاپی کاهش یافته است",
]


async def _read_once(
    engine: AsyncEngine, source: sources.Source, items: list[FetchedItem], at: datetime
) -> candidates.Stored:
    """One successful read, as the engine stores it (the source's own row, then record_ok)."""
    async with engine.begin() as conn:
        current = await sources.get(conn, source.id)
        assert current is not None
        stored = await candidates.store_items(conn, current, items, at)
        await sources.record_ok(conn, source.id, at, len(items))
    return stored


async def test_undated_entries_are_not_fresh_news_on_a_first_read(engine: AsyncEngine) -> None:
    """A feed whose entries carry no readable date: its backlog (any age) is never queued as news;
    an entry that appears later is."""
    source = await add(engine, "https://news.example/undated.xml")
    backlog = [FetchedItem(str(n), None, None, UNDATED[n]) for n in range(3)]
    first = await _read_once(engine, source, backlog, T0)
    assert (first.new, first.skipped) == (0, 3)
    later = T0 + timedelta(minutes=5)
    second = await _read_once(engine, source, [*backlog, FetchedItem("3", None, None, UNDATED[3])], later)
    assert (second.new, second.seen) == (1, 3)
    found = await all_candidates(engine)
    assert {k: v["status"] for k, v in found.items()} == {
        "0": "expired",
        "1": "expired",
        "2": "expired",
        "3": "pending",
    }
    assert found["3"]["created_at"] == later
    # A dated entry on a first read behaves as before: fresh ones are queued, old ones skipped.
    dated = await add(engine, "https://news.example/dated.xml")
    got = await _read_once(
        engine,
        dated,
        [FetchedItem("a", None, T0, OTHER_NEWS), FetchedItem("b", None, T0 - timedelta(days=3), SUNSET)],
        T0,
    )
    assert (got.new, got.skipped) == (1, 1)


async def test_an_undated_entry_still_in_the_feed_never_comes_back_as_new(engine: AsyncEngine) -> None:
    source = await add(engine, "https://news.example/undated.xml")
    await _read_once(engine, source, [], T0)  # the source has been read before
    entry = FetchedItem("static", None, None, UNDATED[0])
    assert (await _read_once(engine, source, [entry], T0)).new == 1
    cid = int((await all_candidates(engine))["static"]["id"])
    async with engine.begin() as conn:
        await candidates.set_status(conn, cid, "approved", T0)
        await candidates.mark_used(conn, cid, T0)
    reread = T0 + timedelta(days=10)
    assert (await _read_once(engine, source, [entry], reread)).seen == 1
    async with engine.begin() as conn:
        assert await candidates.prune(conn, T0 + candidates.KEEP_ENDED + timedelta(minutes=1)) == 0
    again = await _read_once(engine, source, [entry], T0 + candidates.KEEP_ENDED + timedelta(minutes=2))
    assert (again.new, again.seen) == (0, 1)  # still known: not posted a second time
    async with engine.begin() as conn:  # gone from the feed: pruned KEEP_ENDED after its last read
        last = T0 + candidates.KEEP_ENDED + timedelta(minutes=2)
        assert await candidates.prune(conn, last + candidates.KEEP_ENDED + timedelta(minutes=1)) == 1


async def test_a_source_removed_takes_only_its_unreviewed_candidates(engine: AsyncEngine) -> None:
    source = await add(engine, "https://news.example/a.xml")
    items = [
        FetchedItem(external_id=str(n), url=None, published_at=T0, text=f"{OTHER_NEWS} {n}{'!' * n}")
        for n in range(3)
    ]
    async with engine.begin() as conn:
        stored = await candidates.store_items(conn, source, items, T0)
        assert stored.new == 1 and stored.duplicates == 2  # near-identical texts
        ids = sorted(
            int(r) for r in (await conn.execute(text("SELECT id FROM content_candidates"))).scalars()
        )
        await candidates.set_status(conn, ids[0], "approved", T0)
        assert await sources.remove(conn, source.id) == 2
        left = (await conn.execute(text("SELECT id, source_id, source_label FROM content_candidates"))).all()
        assert [(r.id, r.source_id) for r in left] == [(ids[0], None)] and left[
            0
        ].source_label == source.label


async def test_a_proxy_advert_is_never_stored_and_news_keeps_none_of_its_proxies(engine: AsyncEngine) -> None:
    """The owner (2026-11-07): a proxy channel's news is used, its proxies are not; an advert never
    reaches a review, so it cannot count against its source."""
    source = await add(engine, "https://news.example/proxy.xml")
    items = [
        FetchedItem("1", None, T0, "پروکسی ۱ • پروکسی ۲ • پروکسی ۳\n🔥 پروکسی‌های جدید پرسرعت"),
        FetchedItem("2", None, T0, "اتصال • اتصال • اتصال", hidden_proxy_links=3),
        FetchedItem(
            "3",
            None,
            T0,
            "پرسپولیس در دیدار امشب لیگ برتر با دو گل از سد استقلال گذشت و صدرنشین شد.\n"
            "پروکسی ۱ • پروکسی ۲\nvless://00000000-0000-0000-0000-000000000000@198.51.100.7:443#de",
            hidden_proxy_links=2,
        ),
    ]
    async with engine.begin() as conn:
        stored = await candidates.store_items(conn, source, items, T0)
        assert (stored.new, stored.skipped, stored.proxy_posts) == (1, 2, 2)
        (row,) = (await conn.execute(text("SELECT external_id, source_text FROM content_candidates"))).all()
    assert row.external_id == "3"
    assert row.source_text == "پرسپولیس در دیدار امشب لیگ برتر با دو گل از سد استقلال گذشت و صدرنشین شد."


# --- the review -----------------------------------------------------------------------------------


async def test_the_owner_edits_approves_rejects_and_the_publisher_takes_in_order(engine: AsyncEngine) -> None:
    source = await add(engine, "https://news.example/a.xml", "الف")
    items = [
        FetchedItem("1", "https://news.example/1", T0 - timedelta(hours=1), NEWS, views=50),
        FetchedItem("2", None, T0 - timedelta(minutes=5), OTHER_NEWS, views=900),
        FetchedItem(
            "3", None, T0, "خبر سوم با عکس", media=MediaRef(kind="photo", url="https://cdn.example/3.png")
        ),
    ]
    async with engine.begin() as conn:
        await candidates.store_items(conn, source, items, T0)
        by_new = await candidates.queue(conn, "pending", "new", T0)
        assert [c.url for c in by_new] == [None, None, "https://news.example/1"]  # newest first
        assert [c.views for c in await candidates.queue(conn, "pending", "views", T0)][:2] == [900, 50]
        assert (await candidates.queue(conn, "pending", "hot", T0))[0].views == 900
        first = by_new[-1].id  # the oldest
        edited = await candidates.edit(conn, first, " 🔻 عنوان\nتازه ", "متن <b>کوتاه</b>", T0)
        assert edited is not None and edited.title == "🔻 عنوان تازه" and edited.body == "متن <b>کوتاه</b>"
        assert edited.content_html() == "<b>🔻 عنوان تازه</b>\n\nمتن &lt;b&gt;کوتاه&lt;/b&gt;"
        for title, body in (("x" * 121, ""), ("", ""), ("a", "b" * 1001)):
            with pytest.raises(CandidateInvalid, match="caption_invalid"):
                await candidates.edit(conn, first, title, body, T0)
        second = by_new[-2].id
        await candidates.set_status(conn, second, "approved", T0 + timedelta(minutes=1))
        await candidates.set_status(conn, first, "approved", T0 + timedelta(minutes=2))
        photo_id = by_new[0].id
        await candidates.set_status(conn, photo_id, "approved", T0)  # but its photo is not downloaded
        nxt = await candidates.next_for_post(conn, T0 + timedelta(minutes=3))
        assert nxt is not None and nxt.id == second  # approved first goes first; a waiting photo waits
        await candidates.mark_used(conn, second, T0 + timedelta(minutes=3))
        with pytest.raises(CandidateInvalid, match="not_editable"):
            await candidates.set_status(conn, second, "pending", T0)
        with pytest.raises(CandidateInvalid, match="not_editable"):
            await candidates.edit(conn, second, "a", "b", T0)
        with pytest.raises(CandidateInvalid, match="status_invalid"):
            await candidates.set_status(conn, first, "used", T0)
        _, before = await candidates.set_status(conn, first, "rejected", T0)
        assert before == "approved"
        assert await candidates.next_for_post(conn, T0 + timedelta(minutes=4)) is None
        assert (await candidates.counts(conn))["used"] == 1


async def test_a_rejected_item_moved_back_gets_its_file_again(engine: AsyncEngine) -> None:
    """The file of a rejected item is dropped; moving it back must not post the news without it."""
    from proxyhub.content.media import check_for

    source = await add(engine, "https://news.example/a.xml")
    async with engine.begin() as conn:
        await candidates.store_items(
            conn,
            source,
            [
                FetchedItem("p", None, T0, NEWS, media=MediaRef("photo", "https://cdn.example/p.png")),
                FetchedItem("v", None, T0, OTHER_NEWS, media=MediaRef("video", "https://cdn.example/v.mp4")),
            ],
            T0,
        )
    found = await all_candidates(engine)
    photo, video = int(found["p"]["id"]), int(found["v"]["id"])
    picture = png(8, 8, seed=21)
    photo_task = candidates.MediaTask(photo, "photo", "https://cdn.example/p.png")
    video_task = candidates.MediaTask(video, "video", "https://cdn.example/v.mp4")
    clip = mp4(640, 360, 12)
    async with engine.begin() as conn:
        await candidates.save_media(conn, photo_task, check_for("photo", picture), picture, T0)
        await candidates.set_status(conn, video, "approved", T0)
        await candidates.save_media(conn, video_task, check_for("video", clip), clip, T0)
        await candidates.set_status(conn, photo, "rejected", T0)
        await candidates.set_status(conn, video, "rejected", T0)  # the owner changed their mind
        assert await candidates.drop_media(conn, T0 + timedelta(minutes=1)) == 2
        for cid in (photo, video):
            got = await candidates.get(conn, cid)
            assert got is not None and got.media_state == "waiting"
        await candidates.set_status(conn, photo, "pending", T0 + timedelta(minutes=2))
        await candidates.set_status(conn, video, "approved", T0 + timedelta(minutes=2))
        tasks = {(t.role, t.url) for t in await candidates.media_tasks(conn, 6)}
        assert tasks == {("photo", "https://cdn.example/p.png"), ("video", "https://cdn.example/v.mp4")}
        await candidates.set_status(conn, photo, "approved", T0 + timedelta(minutes=3))
        nxt = await candidates.next_for_post(conn, T0 + timedelta(minutes=3))
        assert nxt is None  # both wait for their files, as a fresh candidate does
        await candidates.save_media(
            conn, photo_task, check_for("photo", picture), picture, T0 + timedelta(minutes=4)
        )
        nxt = await candidates.next_for_post(conn, T0 + timedelta(minutes=4))
        assert nxt is not None and nxt.id == photo and await candidates.post_media(conn, nxt) is not None


async def test_a_stale_rejected_item_cannot_go_back_to_the_queue(engine: AsyncEngine) -> None:
    source = await add(engine, "https://news.example/a.xml")
    async with engine.begin() as conn:
        await candidates.store_items(conn, source, [FetchedItem("1", None, T0, NEWS)], T0)
    cid = int((await all_candidates(engine))["1"]["id"])
    async with engine.begin() as conn:
        await candidates.set_status(conn, cid, "rejected", T0)
        late = T0 + candidates.MAX_AGE + timedelta(minutes=1)
        for status in ("pending", "approved"):
            with pytest.raises(CandidateInvalid, match="not_editable"):
                await candidates.set_status(conn, cid, status, late)
        got = await candidates.get(conn, cid)
        assert got is not None and got.status == "rejected"
        moved, before = await candidates.set_status(conn, cid, "pending", T0 + timedelta(hours=1))
        assert moved is not None and (before, moved.status) == ("rejected", "pending")


async def test_the_publisher_and_an_owners_reject_never_both_win(engine: AsyncEngine) -> None:
    """The publisher locks the approved candidate it takes until the post is queued: an owner's
    reject at that moment waits and then finds it posted; a candidate the owner is changing right
    now is passed over. A rejected candidate is never posted and marked used."""
    source = await add(engine, "https://news.example/a.xml")
    async with engine.begin() as conn:
        await candidates.store_items(
            conn, source, [FetchedItem("1", None, T0, NEWS), FetchedItem("2", None, T0, OTHER_NEWS)], T0
        )
    found = await all_candidates(engine)
    first, second = int(found["1"]["id"]), int(found["2"]["id"])
    async with engine.begin() as conn:
        await candidates.set_status(conn, first, "approved", T0)
        await candidates.set_status(conn, second, "approved", T0 + timedelta(minutes=1))
    now = T0 + timedelta(minutes=2)

    # The publisher holds the row: the owner's reject waits (here: times out) ...
    async with engine.connect() as publisher, engine.connect() as owner:
        taken = await candidates.next_for_post(publisher, now, lock=True)
        assert taken is not None and taken.id == first
        await owner.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError):
            await candidates.set_status(owner, first, "rejected", now)
        await owner.rollback()
        await candidates.mark_used(publisher, first, now)
        await publisher.commit()
        # ... and then finds it posted.
        with pytest.raises(CandidateInvalid, match="not_editable"):
            await candidates.set_status(owner, first, "rejected", now)
        await owner.rollback()

    # The owner holds a row (a reject not yet committed): the publisher takes nothing it holds.
    async with engine.begin() as conn:
        third_source = await sources.create(
            conn, kind="rss", ref="https://news.example/b.xml", label=None, now=T0
        )
        await candidates.store_items(conn, third_source, [FetchedItem("3", None, T0, SUNSET)], T0)
    third = int((await all_candidates(engine))["3"]["id"])
    async with engine.begin() as conn:
        await candidates.set_status(conn, third, "approved", T0 + timedelta(minutes=3))
    async with engine.connect() as publisher, engine.connect() as owner:
        await candidates.set_status(owner, second, "rejected", now)  # not committed yet
        taken = await candidates.next_for_post(publisher, now, lock=True)
        assert taken is not None and taken.id == third
        await owner.commit()
        await publisher.rollback()
    # A post of a candidate that is no longer approved is refused (its step rolls back).
    async with engine.begin() as conn:
        with pytest.raises(CandidateInvalid, match="not_editable"):
            await candidates.mark_used(conn, second, now)


async def test_old_candidates_expire_and_their_files_go(engine: AsyncEngine) -> None:
    source = await add(engine, "https://news.example/a.xml")
    async with engine.begin() as conn:
        await candidates.store_items(
            conn,
            source,
            [
                FetchedItem("1", None, T0, OTHER_NEWS, media=MediaRef("photo", "https://cdn.example/1.png")),
                FetchedItem("2", None, T0, NEWS),
            ],
            T0,
        )
        tasks = await candidates.media_tasks(conn, 5)
        assert [(t.role, t.url) for t in tasks] == [("photo", "https://cdn.example/1.png")]
        from proxyhub.content.media import check_for

        data = png(8, 8)
        await candidates.save_media(conn, tasks[0], check_for("photo", data), data, T0)
        assert await candidates.media_bytes_total(conn) == len(data)
        later = T0 + candidates.MAX_AGE + timedelta(minutes=1)
        assert await candidates.expire(conn, later) == 2
        assert await candidates.drop_media(conn, later) == 1
        assert await candidates.media_bytes_total(conn) == 0
        assert await candidates.prune(conn, later) == 0
        assert await candidates.prune(conn, later + candidates.KEEP_ENDED + timedelta(minutes=1)) == 2
