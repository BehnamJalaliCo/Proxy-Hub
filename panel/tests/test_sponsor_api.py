"""/api/sponsor over real HTTP (ASGI transport) with a real PostgreSQL and Redis (decision D10).

Nothing here talks to Telegram: the API only reads and writes the sponsor rows; the reconciler does
the talking. The tests prove the owner-only protection, the campaign rules, «ثبت دوباره», and that
no response ever carries a proxy secret or a full tag.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF, make_vault
from test_sponsor_support import (
    TelemtUsersProvider,
    actions,
    clean,
    create_proxy_node,
    kill_switch,
    regs,
    set_node_state,
)

from proxyhub.api.deps import require_admin
from proxyhub.sponsor import provider as provider_module
from proxyhub.sponsor import service
from proxyhub.sponsor.conversation import Registered, RegisterJob, Step, Stuck
from proxyhub.sponsor.driver import FlowResult
from proxyhub.sponsor.service import Deps, Work
from proxyhub.sponsor.store import load_reg

pytestmark = [pytest.mark.db, pytest.mark.redis]

BASE = "/api/sponsor"
PATHS = {
    ("GET", BASE),
    ("PUT", f"{BASE}/settings"),
    ("POST", f"{BASE}/registrations/{{reg_id}}/retry"),
    ("POST", f"{BASE}/campaigns"),
    ("PUT", f"{BASE}/campaigns/{{campaign_id}}"),
    ("POST", f"{BASE}/campaigns/{{campaign_id}}/cancel"),
    ("DELETE", f"{BASE}/campaigns/{{campaign_id}}"),
}
TAG = "ecfe4482355b04af36e816044653e999"


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean(eng)
    yield eng
    await clean(eng)
    await eng.dispose()


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    from proxyhub.api.app import app, lifespan

    app.dependency_overrides[require_admin] = lambda: ADMIN
    try:
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://panel.test", headers=CSRF
            ) as c:
                yield c
    finally:
        app.dependency_overrides.pop(require_admin, None)


def assert_error(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    assert body["error"] == code and body["message_fa"], body
    assert r.headers["cache-control"] == "no-store"
    return body


def campaign(**kw: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    body = {
        "customer": "فروشگاه نمونه",
        "channel": "https://t.me/customer_one",
        "starts_at": (now + timedelta(hours=3)).isoformat(),
        "ends_at": (now + timedelta(hours=27)).isoformat(),
        "price": "120.50",
        "currency": "eur",
        "paid": True,
    }
    body.update(kw)
    return body


# --- protection ----------------------------------------------------------------------------------------


async def test_every_route_requires_admin_and_csrf(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_sponsor
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_sponsor.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            found.add((method, route.path))
            assert require_admin in {d.call for d in route.dependant.dependencies}, route.path
    assert found == PATHS
    some = "00000000-0000-0000-0000-000000000001"
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(PATHS):
                url = path.replace("{reg_id}", some).replace("{campaign_id}", some)
                assert_error(await c.request(method, url, json={}), 401, "unauthenticated")
                if method != "GET":
                    r = await c.request(method, url, json={}, headers={"X-Requested-With": "nope"})
                    assert_error(r, 403, "csrf")


# --- overview and settings -----------------------------------------------------------------------------


async def test_a_fresh_panel_shows_the_defaults(client: AsyncClient) -> None:
    r = await client.get(BASE)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["settings"] == {"default_channel": "ipoxxo", "lead_minutes": 120}
    assert body["kill_switch"] is True and body["provider_ready"] is (
        provider_module.MTPROTO_PROVIDER is not None
    )
    assert body["account"] == {"state": "none", "connected": False, "mtproxybot_ok": False}
    assert body["pacing"] == {"sent_last_hour": 0, "hourly_cap": 30, "min_gap_s": 5, "hold_until": None}
    assert body["wanted_channel"] == "ipoxxo" and body["registrations"] == [] and body["campaigns"] == []
    assert body["paused"] is False and body["pause_after_stuck"] == 2


async def test_settings_take_a_public_channel_and_a_lead(client: AsyncClient, engine: AsyncEngine) -> None:
    r = await client.put(
        f"{BASE}/settings", json={"default_channel": "https://t.me/IPOXXO", "lead_minutes": 90}
    )
    assert r.status_code == 200 and r.json()["settings"] == {"default_channel": "ipoxxo", "lead_minutes": 90}
    bad = await client.put(
        f"{BASE}/settings", json={"default_channel": "https://t.me/+abcDEF", "lead_minutes": 9}
    )
    assert_error(bad, 400, "channel_private")
    assert_error(
        await client.put(f"{BASE}/settings", json={"default_channel": "ipoxxo", "lead_minutes": 361}),
        400,
        "lead_format",
    )
    assert_error(
        await client.put(f"{BASE}/settings", json={"default_channel": "ipoxxo"}), 422, "invalid_request"
    )
    changed = [a for a in await actions(engine) if a["action"] == "sponsor.settings_changed"]
    assert len(changed) == 1 and changed[0]["actor"] == "admin:owner"


# --- campaigns -----------------------------------------------------------------------------------------


async def test_campaign_create_edit_cancel_and_delete(client: AsyncClient, engine: AsyncEngine) -> None:
    r = await client.post(f"{BASE}/campaigns", json=campaign())
    assert r.status_code == 200, r.text
    body = r.json()
    (c,) = body["campaigns"]
    assert (
        c["id"] == body["id"] and c["channel"] == "customer_one" and c["link"] == "https://t.me/customer_one"
    )
    assert (
        c["price"] == "120.50" and c["currency"] == "EUR" and c["paid"] is True and c["state"] == "scheduled"
    )
    assert c["customer"] == "فروشگاه نمونه" and c["effective"] is False
    assert datetime.fromisoformat(c["switch_at"]) == datetime.fromisoformat(c["starts_at"]) - timedelta(
        hours=2
    )
    # Another campaign in the same window is refused; back to back is fine.
    assert_error(
        await client.post(f"{BASE}/campaigns", json=campaign(channel="@other_one")), 409, "campaign_overlap"
    )
    after = campaign(
        channel="@other_one",
        starts_at=c["ends_at"],
        ends_at=(datetime.fromisoformat(c["ends_at"]) + timedelta(hours=5)).isoformat(),
        price=None,
        currency=None,
        customer=None,
    )
    r = await client.post(f"{BASE}/campaigns", json=after)
    assert r.status_code == 200, r.text
    second = next(x for x in r.json()["campaigns"] if x["channel"] == "other_one")
    assert second["customer"] == "@other_one" and second["price"] is None
    # Edit: the end moves; a private link is refused.
    new_end = (datetime.fromisoformat(c["ends_at"]) - timedelta(hours=2)).isoformat()
    r = await client.put(
        f"{BASE}/campaigns/{c['id']}", json=campaign(starts_at=c["starts_at"], ends_at=new_end)
    )
    assert r.status_code == 200, r.text
    edited = next(x for x in r.json()["campaigns"] if x["id"] == c["id"])
    assert datetime.fromisoformat(edited["ends_at"]) == datetime.fromisoformat(new_end)
    bad = campaign(starts_at=c["starts_at"], ends_at=new_end, channel="https://t.me/joinchat/AAAA")
    assert_error(await client.put(f"{BASE}/campaigns/{c['id']}", json=bad), 400, "channel_private")
    # Cancel, then neither edit nor cancel again; a scheduled one can be deleted, a cancelled one not.
    assert (await client.post(f"{BASE}/campaigns/{c['id']}/cancel")).json()["campaigns"]
    assert_error(await client.post(f"{BASE}/campaigns/{c['id']}/cancel"), 409, "campaign_closed")
    assert_error(await client.put(f"{BASE}/campaigns/{c['id']}", json=campaign()), 409, "campaign_closed")
    assert_error(await client.delete(f"{BASE}/campaigns/{c['id']}"), 409, "campaign_started")
    r = await client.delete(f"{BASE}/campaigns/{second['id']}")
    assert r.status_code == 200 and [x["id"] for x in r.json()["campaigns"]] == [c["id"]]
    assert_error(await client.delete(f"{BASE}/campaigns/{second['id']}"), 404, "not_found")
    assert_error(await client.delete(f"{BASE}/campaigns/not-a-uuid"), 404, "not_found")
    logged = [a["action"] for a in await actions(engine)]
    for action in ("created", "updated", "cancelled", "deleted"):
        assert f"sponsor.campaign_{action}" in logged


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ({"channel": "https://t.me/+AbCdEf123"}, 400, "channel_private"),
        ({"channel": "not a channel"}, 400, "channel_format"),
        ({"channel": "@ipoxxo"}, 400, "channel_is_default"),
        ({"price": "-1"}, 400, "price_format"),
        ({"price": "12.345"}, 400, "price_format"),
        ({"price": "abc"}, 400, "price_format"),
        ({"price": "NaN"}, 400, "price_format"),
        ({"currency": "euro!"}, 400, "currency_format"),
        ({"customer": "x" * 201}, 400, "customer_format"),
        ({"starts_at": "2026-12-01T10:00:00"}, 422, "invalid_request"),  # no time zone
        ({"ends_at": "2020-01-01T00:00:00Z", "starts_at": "2019-12-31T00:00:00Z"}, 400, "campaign_in_past"),
        ({"paid": "yes"}, 422, "invalid_request"),
        ({"extra": 1}, 422, "invalid_request"),
    ],
)
async def test_campaign_input_is_checked(
    client: AsyncClient, change: dict[str, Any], status: int, code: str
) -> None:
    assert_error(await client.post(f"{BASE}/campaigns", json=campaign(**change)), status, code)


# --- registrations ------------------------------------------------------------------------------------


async def test_registrations_show_state_and_the_masked_last_reply_and_can_be_retried(
    client: AsyncClient, engine: AsyncEngine, tmp_path: Path
) -> None:
    await kill_switch(engine, False)
    seal_key = secrets.token_bytes(32)
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.40")
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.41")
    d = Deps(engine=engine, vault=make_vault(tmp_path), provider=provider)
    assert await service.run_once(d) == "no_account"
    async with engine.connect() as conn:
        live = {p.key: p for p in await provider.proxies_to_register(conn)}
    first, second = sorted(await regs(engine), key=lambda r: r["host"])  # found in the same pass

    async def work(row: dict[str, Any]) -> Work:
        async with engine.connect() as conn:
            reg = await load_reg(conn, row["id"])
        assert reg is not None
        job = RegisterJob(host=reg.host, port=reg.port, secret_hex=live[str(reg.proxy_key)].secret_hex)
        return Work(reg=reg, job=job, key=reg.proxy_key, priority=1)

    await service._record(
        d, await work(first), FlowResult(result=Registered(tag=TAG), last_step=Step.SENT_SECRET)
    )
    bot_text = "Invalid secret.\nsecret=[secret]"
    stuck = Stuck(step=Step.SENT_SECRET, reason="no_tag_in_reply", hint="invalid_input")
    await service._record(
        d, await work(second), FlowResult(result=stuck, last_step=Step.SENT_SECRET, last_bot_text=bot_text)
    )
    r = await client.get(BASE)
    body = r.json()
    by_host = {x["host"]: x for x in body["registrations"]}
    ok, bad = by_host["203.0.113.40"], by_host["203.0.113.41"]
    assert ok["state"] == "registered" and ok["has_tag"] is True and ok["tag_hint"] == "ecfe…e999"
    assert ok["sponsor_channel"] is None and ok["wanted_channel"] == "ipoxxo" and ok["in_sync"] is False
    assert ok["stuck"] is None and ok["node_state"] == "ready" and ok["in_use"] is True
    assert ok["tag_on_node"] is True and bad["tag_on_node"] is None and bad["has_tag"] is False
    assert bad["state"] == "failed" and bad["stuck"]["bot_text"] == bot_text
    assert bad["stuck"]["reason"] == "no_tag_in_reply" and bad["stuck"]["reason_fa"]
    assert bad["stuck"]["hint_fa"] and bad["stuck"]["step_fa"] == "پس از فرستادن secret"
    assert body["stuck_count"] == 1 and body["paused"] is False
    # Nothing secret in the whole response: no proxy secret, no full tag.
    text = r.text
    assert TAG not in text
    for proxy in live.values():
        assert proxy.secret_hex not in text
    # «ثبت دوباره»: the stuck one goes back to pending; the registered one gets its sponsor set again.
    r = await client.post(f"{BASE}/registrations/{bad['id']}/retry")
    again = {x["host"]: x for x in r.json()["registrations"]}["203.0.113.41"]
    assert again["state"] == "pending" and again["stuck"] is None and again["attempts"] == 0
    r = await client.post(f"{BASE}/registrations/{ok['id']}/retry")
    assert {x["host"]: x for x in r.json()["registrations"]}["203.0.113.40"]["state"] == "registered"
    assert_error(await client.post(f"{BASE}/registrations/{secrets.token_hex(4)}/retry"), 404, "not_found")
    missing = "00000000-0000-0000-0000-000000000009"
    assert_error(await client.post(f"{BASE}/registrations/{missing}/retry"), 404, "not_found")
    retried = [a for a in await actions(engine) if a["action"] == "sponsor.retry_requested"]
    assert len(retried) == 2 and all(a["actor"] == "admin:owner" for a in retried)
    assert all(TAG not in json.dumps(a["reasons"]) for a in await actions(engine))


async def test_a_retired_registration_cannot_be_retried(
    client: AsyncClient, engine: AsyncEngine, tmp_path: Path
) -> None:
    await kill_switch(engine, False)
    seal_key = secrets.token_bytes(32)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.42")
    d = Deps(engine=engine, vault=make_vault(tmp_path), provider=provider)
    await service.run_once(d)
    (row,) = await regs(engine)
    async with engine.connect() as conn:
        (proxy,) = await provider.proxies_to_register(conn)
        reg = await load_reg(conn, row["id"])
    assert reg is not None
    work = Work(reg=reg, job=RegisterJob(host=reg.host, port=reg.port, secret_hex=proxy.secret_hex),
                key=reg.proxy_key, priority=1)  # fmt: skip
    await service._record(d, work, FlowResult(result=Registered(tag=TAG), last_step=Step.SENT_SECRET))
    await set_node_state(engine, node, "deleted")
    await service.run_once(d)
    (row,) = await regs(engine)
    assert row["state"] == "retired"
    assert_error(await client.post(f"{BASE}/registrations/{row['id']}/retry"), 409, "registration_retired")
    body = (await client.get(BASE)).json()
    (view,) = body["registrations"]
    assert view["in_use"] is False and view["wanted_channel"] is None and view["tag_on_node"] is None
