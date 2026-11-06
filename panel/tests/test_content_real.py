"""The real sources through the production fetcher (decision D51): a public channel's preview page on
t.me and a real news feed, with the production guard (system resolver, public addresses only, the
default certificate store, port 443). They skip when the network is not reachable from here."""

from __future__ import annotations

import pytest

from proxyhub.content import clean, media
from proxyhub.content.net import FetchError, SafeHttp
from proxyhub.content.rss import RssFeed
from proxyhub.content.telegram_preview import TelegramPreview

# Not reachable from this network: no route, or the site refuses this network's address (a feed
# may answer 403 to some cloud ranges).
UNREACHABLE = {
    "dns_failed",
    "connect_failed",
    "timeout",
    "tls_failed",
    "blocked_address",
    "http_403",
    "http_429",
}
CHANNEL = "bbcpersian"
FEED = "https://feeds.bbci.co.uk/persian/rss.xml"


async def _or_skip(coro: object) -> object:
    try:
        return await coro  # type: ignore[misc]
    except FetchError as exc:
        if exc.code in UNREACHABLE or exc.code.startswith("http_5"):
            pytest.skip(f"the source is not reachable from here ({exc.code})")
        raise


async def test_a_real_public_channel_preview() -> None:
    async with SafeHttp() as http:
        items = await _or_skip(TelegramPreview().fetch(http, CHANNEL))
        assert isinstance(items, list) and len(items) >= 5
        assert all(i.url == f"https://t.me/{CHANNEL}/{i.external_id}" for i in items)
        assert all(i.published_at is not None for i in items)
        assert any(i.views for i in items)
        assert any(i.text for i in items)
        photos = [i for i in items if i.media is not None and i.media.kind == "photo" and i.media.url]
        if photos:  # the newest photo, downloaded and checked like the engine does
            assert photos[-1].media is not None and photos[-1].media.url is not None
            fetched = await _or_skip(http.get(photos[-1].media.url, limit=media.PHOTO_MAX_BYTES))
            info = media.check_for("photo", fetched.body)  # type: ignore[attr-defined]
            assert info.mime in ("image/jpeg", "image/png") and info.width
        for item in items:  # the source's own handle never survives the cleaning
            assert "@bbcpersian" not in clean.clean_source_text(item.text, [CHANNEL]).lower()


async def test_a_real_news_feed() -> None:
    async with SafeHttp() as http:
        items = await _or_skip(RssFeed().fetch(http, FEED))
        assert isinstance(items, list) and len(items) >= 5
        assert all(i.external_id and i.text for i in items)
        assert all(i.url is None or i.url.startswith("https://") for i in items)
