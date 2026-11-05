"""The production path of a DNS-01 challenge record, on real Cloudflare and the real public DoH
resolvers (decision D10). Runs on the panel server, like tests/test_net_cloudflare_real.py:
``PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE`` (0600) and ``PROXYHUB_CLOUDFLARE_TEST_ZONE``.

The record is created under the managed-record rule (purpose ``acme``: our marker AND our row),
seen live by Cloudflare's and Google's DoH resolvers, and removed again; nothing else in the zone is
touched. No certificate is ordered here (Let's Encrypt production is not a test service; Pebble
covers the ACME conversation in tests/test_acme_real.py).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_net_support import clean_net_tables

from proxyhub.acme.doh import DEFAULT_RESOLVERS, DohResolver, wait_for_txt
from proxyhub.acme.protocol import Challenge
from proxyhub.acme.service import CloudflareChallengeDns
from proxyhub.cloudflare import service
from proxyhub.cloudflare.client import CloudflareClient
from proxyhub.cloudflare.records import has_marker
from proxyhub.config import read_secret_file
from proxyhub.vault import SecretValue

TOKEN_FILE = os.environ.get("PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE")
TEST_ZONE = os.environ.get("PROXYHUB_CLOUDFLARE_TEST_ZONE", "")

pytestmark = [pytest.mark.cloudflare, pytest.mark.db]


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_net_tables(eng)
    yield eng
    await clean_net_tables(eng)
    await eng.dispose()


def _token() -> SecretValue:
    assert TOKEN_FILE, "PROXYHUB_CLOUDFLARE_TEST_TOKEN_FILE"
    path = Path(TOKEN_FILE)
    assert path.stat().st_mode & 0o077 == 0, "the token file must be 0600"
    return SecretValue(read_secret_file(path))


async def test_real_challenge_record_is_published_seen_over_doh_and_removed(engine: AsyncEngine) -> None:
    token = _token()
    host = f"ph-test-{secrets.token_hex(4)}.{TEST_ZONE}"
    challenge = Challenge(
        identifier=host, record_name=f"_acme-challenge.{host}", value=secrets.token_urlsafe(32)
    )
    async with CloudflareClient(token) as cf:
        check = await service.check_token(cf)
        zone = next(z for z in check.zones if z.name == TEST_ZONE)
        async with engine.begin() as conn:
            await service.store_zones(conn, check.zones, check.dns_edit, datetime.now(UTC))
        dns = CloudflareChallengeDns(engine, cf, zone.id)
        try:
            await dns.publish([challenge])
            [record] = [r for r in await cf.list_dns_records(zone.id) if r.name == challenge.record_name]
            assert record.type == "TXT" and has_marker(record.comment) and not record.proxied
            async with engine.connect() as conn:
                [row] = await service.owned_rows(conn, zone.id, "acme")
            assert row.cf_record_id == record.id and row.name == challenge.record_name
            resolvers = [DohResolver(url) for url in DEFAULT_RESOLVERS]
            try:
                seen = await wait_for_txt(resolvers, challenge.record_name, challenge.value, timeout_s=120)
            finally:
                for r in resolvers:
                    await r.__aexit__(None, None, None)
            assert seen, "the public resolvers never saw the challenge record"
        finally:
            assert await dns.clear()
        assert [r for r in await cf.list_dns_records(zone.id) if r.name == challenge.record_name] == []
        async with engine.connect() as conn:
            assert await service.owned_rows(conn, zone.id, "acme") == []
