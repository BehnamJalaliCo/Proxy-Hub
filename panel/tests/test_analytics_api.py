"""/api/analytics/* over real HTTP (ASGI) with a real PostgreSQL and Redis, plus the rule-7 checks:
no analytics table has a column for an IP or a person, and no analytics answer carries an IP.

Telegram is never imitated (D10): link creation runs against a local port nobody listens on.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_analytics_support import (
    CHANNEL_MAIN,
    TB,
    add_account,
    add_node,
    add_offer,
    chat_member_update,
    clean_analytics_tables,
    random_link,
)
from test_analytics_units import usage_block
from test_fleet_support import ADMIN, CSRF, assert_absent_from_database, clean_fleet_tables, make_vault
from test_telegram_support import (
    all_logs,
    assert_no_token,
    clean_telegram_tables,
    closed_port_bot,
    random_token,
)

from proxyhub.analytics import sampling
from proxyhub.analytics.usage import UsageAgg, store_usage_agg
from proxyhub.api.deps import require_admin
from proxyhub.api.routes_analytics import get_bot_factory, get_vault
from proxyhub.db.models import Base
from proxyhub.reconcilers.telegram import handle_update
from proxyhub.telegram import store
from proxyhub.telegram.client import Update
from proxyhub.vault import SecretValue, Vault

PATHS = {
    ("GET", "/api/analytics/summary"),
    ("GET", "/api/analytics/series"),
    ("GET", "/api/analytics/usage"),
    ("GET", "/api/analytics/channel-stats"),
    ("GET", "/api/analytics/capacity"),
    ("GET", "/api/analytics/links"),
    ("POST", "/api/analytics/links"),
    ("PUT", "/api/analytics/links/{link_id}"),
    ("GET", "/api/analytics/settings"),
    ("PUT", "/api/analytics/settings"),
}

# --- rule 7 on the schema (no database needed) ----------------------------------------------------

PERSON_OR_IP_COLUMNS = re.compile(
    r"^(ip|ips|ipv4|ipv6|ip_address|addr|address|remote_addr|client_ip|first_name|last_name|full_name|"
    r"username|user_name|phone|phone_number|language_code|is_premium|email)$"
)


def test_analytics_tables_have_no_column_for_an_ip_or_a_person() -> None:
    import proxyhub.db.models_analytics  # noqa: F401 - registers the tables

    tables = [t for name, t in Base.metadata.tables.items() if name.startswith("analytics_")]
    assert len(tables) == 11
    for table in tables:
        for column in table.columns:
            assert not PERSON_OR_IP_COLUMNS.match(column.name), f"{table.name}.{column.name}"
            assert not isinstance(column.type, INET), f"{table.name}.{column.name}"
    events = Base.metadata.tables["analytics_member_events"]
    assert [c.name for c in events.columns] == [
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


_TOKEN = re.compile(r"[0-9A-Fa-f:.]{3,}")


def _is_ip(token: str) -> bool:
    try:
        ipaddress.ip_address(token.strip(".:") if token.count(":") < 2 else token)
    except ValueError:
        return False
    return True


def ip_like_strings(value: Any) -> list[str]:
    """Every string in a JSON value that is, or contains, a valid IPv4 or IPv6 address."""
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and (_is_ip(item) or any(_is_ip(t) for t in _TOKEN.findall(item))):
            found.append(item)
    return found


def test_the_ip_detector_works() -> None:
    assert ip_like_strings({"a": ["x 10.1.2.3 y"], "b": "2001:db8::1", "c": {"fe80::1": 1}}) == [
        "fe80::1",
        "2001:db8::1",
        "x 10.1.2.3 y",
    ]
    assert (
        ip_like_strings({"at": "2026-11-04T10:00:00Z", "v": "1.5", "day": "2026-11-04", "t": "10:00:00 x"})
        == []
    )


# --- HTTP -----------------------------------------------------------------------------------------


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
    await clean_fleet_tables(eng)


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


@pytest.fixture
async def client(engine: AsyncEngine, vault: Vault) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_vault] = lambda: vault
    app.dependency_overrides[get_bot_factory] = closed_port_bot
    try:
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        for dep in (require_admin, get_vault, get_bot_factory):
            app.dependency_overrides.pop(dep, None)


@pytest.mark.db
@pytest.mark.redis
async def test_every_analytics_route_requires_the_owner(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_analytics
    from proxyhub.api.app import app, lifespan

    found = {
        (m, r.path) for r in routes_analytics.router.routes if isinstance(r, APIRoute) for m in r.methods
    }
    assert found == PATHS
    async with lifespan(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
        ) as c:
            for method, path in sorted(PATHS):
                r = await c.request(method, path.replace("{link_id}", "1"), json={})
                assert r.status_code == 401, (method, path, r.status_code)
                assert r.json()["error"] == "unauthenticated"


async def seed(engine: AsyncEngine) -> None:
    """A small, realistic month: one node with traffic, a channel with members and joins, usage counters."""
    now = datetime.now(UTC)
    acc = await add_account(engine)
    await add_offer(engine, acc)
    node_id = await add_node(engine, acc, 701, name="hel-1", created_at=now - timedelta(minutes=40))
    async with engine.connect() as conn:
        (node,) = await sampling.sampleable_nodes(conn)
    from test_analytics_db import store_sample

    await store_sample(
        engine, node, sampling.minute(now - timedelta(minutes=30)), out_bytes=2 * 10**11, cpu=[40.0]
    )
    await store_sample(
        engine, node, sampling.minute(now - timedelta(minutes=15)), out_bytes=3 * 10**11, cpu=[60.0]
    )
    async with engine.begin() as conn:
        await store.save_channel(
            conn, "main", {"chat_id": CHANNEL_MAIN, "username": "@ipoxxo", "title": "IPOXXO"}
        )
        await sampling.store_channel_sample(conn, "main", CHANNEL_MAIN, sampling.minute(now), 4321)
        await store_usage_agg(conn, node_id, UsageAgg.model_validate(usage_block()), now)
        for i in range(3):
            raw = chat_member_update(
                100 + i,
                chat_id=CHANNEL_MAIN,
                user_id=8000 + i,
                old="left",
                new="member",
                date=int(now.timestamp()) - 30,
            )
            await handle_update(conn, Update.from_result(raw), now)


@pytest.mark.db
@pytest.mark.redis
async def test_read_routes_with_real_data_and_no_ip_anywhere(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    await seed(engine)
    answers: dict[str, Any] = {}
    for path in (
        "summary",
        "series?days=14",
        "usage?days=7",
        "channel-stats",
        "capacity",
        "links",
        "settings",
    ):
        r = await client.get(f"/api/analytics/{path}")
        assert r.status_code == 200, (path, r.text)
        assert r.headers["cache-control"] == "no-store"
        answers[path] = r.json()
    summary = answers["summary"]
    assert summary["channel"]["main"]["members"] == 4321 and summary["channel"]["main"]["joins_today"] in (
        0,
        3,
    )
    assert summary["servers"][0]["name"] == "hel-1" and summary["servers"][0]["traffic_pct"] == pytest.approx(
        3 * 10**11 * 100 / (20 * TB)
    )
    assert summary["thresholds"]["traffic_warn_pct"] == 80
    assert len(answers["series?days=14"]["days"]) == 14
    assert answers["usage?days=7"]["has_data"] is True
    assert answers["capacity"][0]["signal"] == "ok"
    assert ip_like_strings(answers) == []
    bad = await client.get("/api/analytics/series?days=0")
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_request"


@pytest.mark.db
@pytest.mark.redis
async def test_creating_a_tracking_link_needs_a_channel_and_a_bot_and_leaks_no_token(
    client: AsyncClient, engine: AsyncEngine, vault: Vault
) -> None:
    r = await client.post("/api/analytics/links", json={"name": "ad-1", "purpose": "تبلیغ"})
    assert r.status_code == 409 and r.json()["error"] == "main_channel_not_set" and r.json()["message_fa"]
    async with engine.begin() as conn:
        await store.save_channel(
            conn, "main", {"chat_id": CHANNEL_MAIN, "username": "@ipoxxo", "title": "IPOXXO"}
        )
    r = await client.post("/api/analytics/links", json={"name": "ad-1", "purpose": ""})
    assert r.status_code == 409 and r.json()["error"] == "bot_not_set"
    token = random_token()
    async with engine.begin() as conn:
        await store.save_bot(
            conn,
            vault,
            info=store.BotInfo(id=8123456789, username="proxyhub_test_bot"),
            token=SecretValue(token),
            now=datetime.now(UTC),
        )
        # A new bot clears the channels (it must be made admin again): register the channel again.
        await store.save_channel(
            conn, "main", {"chat_id": CHANNEL_MAIN, "username": "@ipoxxo", "title": "IPOXXO"}
        )
    with all_logs() as logs:
        r = await client.post("/api/analytics/links", json={"name": "ad-1", "purpose": "کانال الف"})
    assert r.status_code == 502 and r.json()["error"] == "telegram_unavailable"
    assert_no_token(token, r.text, logs.getvalue())
    await assert_absent_from_database(engine, token)
    await assert_absent_from_database(engine, token.split(":")[1])
    assert (await client.get("/api/analytics/links")).json() == []
    for body in ({"name": ""}, {"name": "x" * 33}, {"name": "ok", "purpose": "p" * 201}):
        r = await client.post("/api/analytics/links", json=body)
        assert r.status_code == 422 and r.json()["error"] == "invalid_link_name", body
    r = await client.post("/api/analytics/links", json={"name": "ok", "extra": 1})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


@pytest.mark.db
@pytest.mark.redis
async def test_rename_keeps_the_history_and_there_is_no_delete(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    links = [random_link(), random_link()]
    async with engine.begin() as conn:
        for i, link in enumerate(links):
            await conn.execute(
                text(
                    "INSERT INTO analytics_tracking_links "
                    "(chat_id, name, purpose, invite_link, telegram_name, created_by) "
                    "VALUES (:c, :n, '', :l, :n, 'admin:owner')"
                ),
                {"c": CHANNEL_MAIN, "n": f"camp-{i}", "l": link},
            )
        await store.save_channel(
            conn, "main", {"chat_id": CHANNEL_MAIN, "username": "@ipoxxo", "title": "IPOXXO"}
        )
        raw = chat_member_update(
            1,
            chat_id=CHANNEL_MAIN,
            user_id=5555,
            old="left",
            new="member",
            date=1793800000,
            invite_link=links[0],
            link_name="camp-0",
        )
        await handle_update(conn, Update.from_result(raw), datetime.now(UTC))
    listed = (await client.get("/api/analytics/links")).json()
    first = next(x for x in listed if x["name"] == "camp-0")
    r = await client.put(
        f"/api/analytics/links/{first['id']}", json={"name": "کانال الف — آبان", "purpose": "۲ پست"}
    )
    assert r.status_code == 200 and r.json()["name"] == "کانال الف — آبان" and r.json()["joins"] == 1
    assert r.json()["invite_link"] == links[0] and r.json()["renamed_at"] is not None
    r = await client.put(f"/api/analytics/links/{first['id']}", json={"name": "camp-1"})
    assert r.status_code == 409 and r.json()["error"] == "link_name_taken"
    r = await client.put("/api/analytics/links/999999", json={"name": "x"})
    assert r.status_code == 404 and r.json()["error"] == "link_not_found"
    r = await client.request("DELETE", f"/api/analytics/links/{first['id']}")
    assert r.status_code == 405
    async with engine.connect() as conn:
        actions = (
            (await conn.execute(text("SELECT action FROM actions_log WHERE actor = 'admin:owner'")))
            .scalars()
            .all()
        )
    assert list(actions) == ["analytics.tracking_link_renamed"]


@pytest.mark.db
@pytest.mark.redis
async def test_thresholds_settings(
    client: AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    assert (await client.get("/api/analytics/settings")).json() == {
        "traffic_warn_pct": 80,
        "cpu_warn_pct": 80,
        "cpu_window_min": 60,
        "alerts_enabled": True,
    }
    new = {"traffic_warn_pct": 70, "cpu_warn_pct": 90, "cpu_window_min": 30, "alerts_enabled": False}
    r = await client.put("/api/analytics/settings", json=new)
    assert r.status_code == 200 and r.json() == new
    assert (await client.get("/api/analytics/settings")).json() == new
    for bad in ({**new, "traffic_warn_pct": 0}, {**new, "extra": True}, {"traffic_warn_pct": 70}, [1]):
        r = await client.put("/api/analytics/settings", json=bad)
        assert r.status_code == 422 and r.json()["error"] == "invalid_settings", bad
    summary = (await client.get("/api/analytics/summary")).json()
    assert summary["thresholds"] == new
    secret_word = secrets.token_hex(4)
    assert secret_word not in caplog.text
