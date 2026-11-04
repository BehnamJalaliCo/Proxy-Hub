"""The sponsor module on the REAL database (decision D10).

* The live proxies come from :class:`test_sponsor_support.TelemtUsersProvider` over the real
  ``nodes``/``telemt_users`` tables with real sealed secrets, and a tag lands in the node's real
  desired state.
* What a conversation ended with (:class:`proxyhub.sponsor.driver.FlowResult`) is fed to the
  recording step as data: nothing plays @MTProxybot. The one real conversation attempt here goes
  to a local port nobody listens on (a real refusal), so no message is sent and none is logged.
* Pacing, the kill switch, the account lock and the owner alert use the real tables and locks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from telethon import types
from test_fleet_support import assert_absent_from_database, make_vault
from test_mtproxy_support import seed_connected
from test_mtproxy_units import closed_port
from test_sponsor_support import (
    TelemtUsersProvider,
    actions,
    alerts,
    clean,
    connect_account,
    create_proxy_node,
    kill_switch,
    pair_owner,
    regs,
    set_node_state,
    user_tags,
)
from test_telegram_support import all_logs

from proxyhub.agent_api.desired import build_desired_state
from proxyhub.mtproxy import store as account_store
from proxyhub.mtproxy import userclient
from proxyhub.mtproxy.userclient import ApiCredentials, BotChat, ClientOptions, Endpoint, OperatorSession
from proxyhub.sponsor import driver, pacing, service, store
from proxyhub.sponsor.conversation import PromoteJob, PromotionSet, Registered, RegisterJob, Step, Stuck
from proxyhub.sponsor.driver import FlowResult, Timing
from proxyhub.sponsor.pacing import PaceStop
from proxyhub.sponsor.service import Deps, Work
from proxyhub.vault import SecretValue, Vault

pytestmark = [pytest.mark.db]

BOT_ID = 93_372_553
TAG = "ecfe4482355b04af36e816044653e999"


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean(eng)
    yield eng
    await clean(eng)
    await eng.dispose()


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    return make_vault(tmp_path)


@pytest.fixture
def seal_key() -> bytes:
    return secrets.token_bytes(32)


def deps(engine: AsyncEngine, vault: Vault, provider: object, **kw: object) -> Deps:
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=3)
    return Deps(engine=engine, vault=vault, provider=provider, options=options, **kw)  # type: ignore[arg-type]


async def load(engine: AsyncEngine) -> list[store.Reg]:
    async with engine.connect() as conn:
        return await store.load_regs(conn)


async def work_for(engine: AsyncEngine, provider: TelemtUsersProvider, reg_id: uuid.UUID) -> Work:
    """The job the reconciler would build for this registration (secret from the provider)."""
    async with engine.connect() as conn:
        live = {p.key: p for p in await provider.proxies_to_register(conn)}
        reg = await store.load_reg(conn, reg_id)
    assert reg is not None
    if reg.ad_tag is None:
        proxy = live[str(reg.proxy_key)]
        job: RegisterJob | PromoteJob = RegisterJob(host=reg.host, port=reg.port, secret_hex=proxy.secret_hex)
    else:
        job = PromoteJob(host=reg.host, port=reg.port, channel="ipoxxo")
    return Work(reg=reg, job=job, key=reg.proxy_key, priority=1)


# --- the kill switch, the provider, the account -----------------------------------------------------------


async def test_the_kill_switch_stops_the_whole_module(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.10")
    await connect_account(engine)
    # No fleet row at all: the kill switch is ON by default (an unreadable switch counts as ON too).
    assert await service.run_once(deps(engine, vault, provider)) == "kill_switch"
    await kill_switch(engine, True)
    assert await service.run_once(deps(engine, vault, provider)) == "kill_switch"
    assert await regs(engine) == [] and await actions(engine) == []


async def test_without_a_provider_only_campaign_states_move(engine: AsyncEngine, vault: Vault) -> None:
    await kill_switch(engine, False)
    now = datetime.now(UTC)
    data = service.CampaignInput(
        customer="Shop", channel="@customer_one", starts_at=now + timedelta(minutes=30),
        ends_at=now + timedelta(hours=5), price=None, currency=None, paid=True,
    )  # fmt: skip
    campaign_id = await service.create_campaign(engine, data, actor="admin:o", now=now)
    assert await service.run_once(deps(engine, vault, None)) == "no_provider"
    async with engine.connect() as conn:
        campaign = await store.load_campaign(conn, campaign_id)
    assert campaign is not None and campaign.state == "active"  # 60 minutes of lead began already
    assert [a["action"] for a in await actions(engine)] == [
        "sponsor.campaign_created",
        "sponsor.campaign_active",
    ]


async def test_a_failing_provider_does_not_stop_the_campaigns(engine: AsyncEngine, vault: Vault) -> None:
    class Broken(TelemtUsersProvider):
        async def proxies_to_register(self, db: object) -> list[object]:  # type: ignore[override]
            await db.execute(text("SELECT * FROM no_such_table"))  # type: ignore[attr-defined]
            return []

    await kill_switch(engine, False)
    now = datetime.now(UTC)
    data = service.CampaignInput(
        customer=None, channel="@customer_one", starts_at=now + timedelta(minutes=10),
        ends_at=now + timedelta(hours=2), price=None, currency=None, paid=False,
    )  # fmt: skip
    campaign_id = await service.create_campaign(engine, data, actor="admin:o", now=now)
    assert await service.run_once(deps(engine, vault, Broken(secrets.token_bytes(32)))) == "no_account"
    async with engine.connect() as conn:
        campaign = await store.load_campaign(conn, campaign_id)
    assert campaign is not None and campaign.state == "active"


async def test_live_proxies_get_rows_and_nothing_talks_without_an_account(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    ready = await create_proxy_node(engine, seal_key, ipv4="203.0.113.11")
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.12", state="booting")
    async with engine.connect() as conn:
        secret = (await provider.proxies_to_register(conn))[0].secret_hex
    with all_logs(logging.DEBUG) as logs:
        assert await service.run_once(deps(engine, vault, provider)) == "no_account"
        await connect_account(engine, bot_id=None)  # @MTProxybot never resolved: still no account
        assert await service.run_once(deps(engine, vault, provider)) == "no_account"
    rows = await regs(engine)
    assert [(r["host"], r["port"], r["secret_name"], r["state"]) for r in rows] == [
        ("203.0.113.11", 8443, "s1", "pending")
    ]
    assert rows[0]["proxy_key"] == f"mtproto:{ready}:s1"
    assert [a["action"] for a in await actions(engine)] == ["sponsor.proxy_found"]
    await assert_absent_from_database(engine, secret)
    assert secret not in logs.getvalue()


# --- recording what a conversation ended with ----------------------------------------------------------


async def _registered_row(engine: AsyncEngine, provider: TelemtUsersProvider, vault: Vault) -> uuid.UUID:
    await service.run_once(deps(engine, vault, provider))  # creates the pending row
    return (await regs(engine))[0]["id"]  # type: ignore[no-any-return]


async def test_a_tag_from_the_bot_lands_in_the_nodes_desired_state(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.13")
    reg_id = await _registered_row(engine, provider, vault)
    work = await work_for(engine, provider, reg_id)
    d = deps(engine, vault, provider)
    outcome = FlowResult(result=Registered(tag=TAG), last_step=Step.SENT_SECRET, messages_sent=3)
    assert await service._record(d, work, outcome) == "registered"
    (row,) = await regs(engine)
    assert row["state"] == "registered" and row["ad_tag"] == TAG and row["attempts"] == 0
    assert await user_tags(engine, node) == {"s1": TAG}
    async with AsyncSession(engine) as session:
        desired = await build_desired_state(session, seal_key, node)
    assert desired["telemt"]["users"][0]["ad_tag"] == TAG  # what the node's agent will apply
    logged = await actions(engine)
    assert [a["action"] for a in logged][-2:] == ["sponsor.registered", "sponsor.tag_applied"]
    assert TAG not in json.dumps([a["reasons"] for a in logged])  # logs carry the short form only
    # The proxy now has a tag, so the provider no longer lists it, and the next job is its sponsor.
    async with engine.connect() as conn:
        assert await provider.proxies_to_register(conn) == []
    (reg,) = await load(engine)
    nodes = {node: "ready"}
    chosen = service.choose([reg], live={}, nodes=nodes, wanted="ipoxxo", default="ipoxxo",
                            campaign_channels=set(), now=datetime.now(UTC))  # fmt: skip
    assert chosen is not None and chosen.job == PromoteJob(host="203.0.113.13", port=8443, channel="ipoxxo")
    assert (
        await service._record(d, chosen, FlowResult(result=PromotionSet(channel="ipoxxo"))) == "promotion_set"
    )
    (row,) = await regs(engine)
    assert row["sponsor_channel"] == "ipoxxo"
    (reg,) = await load(engine)
    assert reg.promotion_set_at is not None
    assert service.choose([reg], live={}, nodes=nodes, wanted="ipoxxo", default="ipoxxo",
                          campaign_channels=set(), now=datetime.now(UTC)) is None  # fmt: skip


async def test_a_tag_is_kept_even_if_the_node_cannot_take_it_now(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.14")
    reg_id = await _registered_row(engine, provider, vault)
    work = await work_for(engine, provider, reg_id)
    async with engine.begin() as conn:  # the Telemt user disappeared meanwhile
        await conn.execute(text("DELETE FROM telemt_users WHERE node_id = :n"), {"n": node})
    outcome = FlowResult(result=Registered(tag=TAG), last_step=Step.SENT_SECRET)
    assert await service._record(deps(engine, vault, provider), work, outcome) == "registered"
    (row,) = await regs(engine)
    assert row["ad_tag"] == TAG and row["state"] == "registered"
    assert [a["action"] for a in await actions(engine)][-2:] == [
        "sponsor.registered",
        "sponsor.tag_apply_failed",
    ]


async def test_a_reply_the_panel_does_not_understand_alerts_once_and_two_pause_everything(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    await pair_owner(engine)
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.15")
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.16")
    await service.run_once(deps(engine, vault, provider))
    await seed_connected(engine, vault, port=closed_port(), mtproxybot_id=BOT_ID)  # Telegram unreachable
    first_id, second_id = [r["id"] for r in await regs(engine)]
    d = deps(engine, vault, provider)
    secret = (await work_for(engine, provider, first_id)).job.secret_hex  # type: ignore[union-attr]
    bot_text = "Sorry, this proxy is already registered.\n<b>[secret]</b>"
    stuck = FlowResult(
        result=Stuck(step=Step.SENT_ADDRESS, reason="address_refused", hint="already_registered"),
        last_step=Step.SENT_ADDRESS,
        last_bot_text=bot_text,
    )
    assert (
        await service._record(d, await work_for(engine, provider, first_id), stuck) == "stuck:address_refused"
    )
    rows = {r["id"]: r for r in await regs(engine)}
    assert rows[first_id]["state"] == "failed" and rows[first_id]["last_bot_text"] == bot_text
    assert rows[first_id]["alerted_at"] is not None
    (alert,) = await alerts(engine)
    assert "203.0.113.15:8443" in alert and "&lt;b&gt;[secret]&lt;/b&gt;" in alert  # escaped, masked
    assert (
        "پس از فرستادن نشانی و پورت" in alert and "از قبل ثبت شده" in alert and "برای هیچ پروکسی" not in alert
    )
    # The same registration stuck again (no retry in between): no second alert.
    assert (
        await service._record(d, await work_for(engine, provider, first_id), stuck) == "stuck:address_refused"
    )
    assert len(await alerts(engine)) == 1
    # One stuck registration does not stop the others ...
    assert await service.run_once(d) == "failed:telegram_unavailable"
    rows = {r["id"]: r for r in await regs(engine)}
    assert rows[second_id]["last_error"] == "telegram_unavailable"
    # ... two do: the bot probably says something new.
    second = await work_for(engine, provider, second_id)
    assert await service._record(d, second, stuck) == "stuck:address_refused"
    assert len(await alerts(engine)) == 2 and "برای هیچ پروکسی" in (await alerts(engine))[1]
    assert await service.run_once(d) == "paused"
    # «ثبت دوباره» brings one back; the module talks again.
    await service.retry(engine, first_id, actor="admin:owner", now=datetime.now(UTC))
    rows = {r["id"]: r for r in await regs(engine)}
    assert rows[first_id]["state"] == "pending" and rows[first_id]["last_bot_text"] is None
    assert rows[first_id]["alerted_at"] is None and rows[first_id]["attempts"] == 0
    assert await service.run_once(d) == "failed:telegram_unavailable"
    assert any(
        a["action"] == "sponsor.retry_requested" and a["actor"] == "admin:owner"
        for a in await actions(engine)
    )
    await assert_absent_from_database(engine, secret)


async def test_failures_back_off_and_a_silent_bot_becomes_stuck(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    await pair_owner(engine)
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.17")
    reg_id = await _registered_row(engine, provider, vault)
    clock = [datetime(2026, 11, 4, 12, 0, tzinfo=UTC)]
    d = deps(engine, vault, provider, clock=lambda: clock[0])

    async def record(error: str, **kw: object) -> str:
        return await service._record(
            d, await work_for(engine, provider, reg_id), FlowResult(error=error, **kw)
        )  # type: ignore[arg-type]

    assert await record("telegram_unavailable") == "failed:telegram_unavailable"
    (row,) = await regs(engine)
    assert row["attempts"] == 1 and row["next_attempt_at"] == clock[0] + timedelta(minutes=5)
    assert await record("flood_wait", retry_after_s=3600, rpc="FLOOD_WAIT") == "failed:flood_wait"
    (row,) = await regs(engine)
    assert row["attempts"] == 2 and row["next_attempt_at"] == clock[0] + timedelta(seconds=3605)
    async with engine.connect() as conn:  # and no message to anyone until then
        assert (await pacing.load(conn)).hold_until == clock[0] + timedelta(seconds=3605)
    assert await record("kill_switch") == "failed:kill_switch"  # nobody's fault: not an attempt
    assert await record("hourly_cap") == "failed:hourly_cap"
    (row,) = await regs(engine)
    assert row["attempts"] == 2
    assert await record("no_reply", last_step=Step.SENT_NEWPROXY) == "failed:no_reply"
    assert await record("no_reply", last_step=Step.SENT_NEWPROXY) == "stuck:no_reply"
    (row,) = await regs(engine)
    assert row["state"] == "failed" and row["stuck_reason"] == "no_reply"
    assert len(await alerts(engine)) == 1
    failed = [a for a in await actions(engine) if a["action"] == "sponsor.flow_failed"]
    assert len(failed) == 5 and all(a["allowed"] is False for a in failed)
    assert failed[1]["reasons"]["context"]["telegram_error"] == "FLOOD_WAIT"


async def test_telegram_refusing_the_chat_is_not_retried_blindly(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    await pair_owner(engine)
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.18")
    reg_id = await _registered_row(engine, provider, vault)
    work = await work_for(engine, provider, reg_id)
    d = deps(engine, vault, provider)
    for code in ("mtproxybot_changed", "telegram_refused"):
        await service.retry(engine, reg_id, actor="admin:o", now=datetime.now(UTC))
        assert await service._record(d, work, FlowResult(error=code, rpc="PEER_FLOOD")) == f"stuck:{code}"
    assert len(await alerts(engine)) == 2


# --- the real conversation step, without Telegram ------------------------------------------------------


async def test_a_real_attempt_with_telegram_unreachable_sends_and_logs_nothing(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.19")
    api, session, _ = await seed_connected(engine, vault, port=closed_port(), mtproxybot_id=BOT_ID)
    async with engine.connect() as conn:
        secret = (await provider.proxies_to_register(conn))[0].secret_hex
    with all_logs(logging.DEBUG) as logs:
        assert await service.run_once(deps(engine, vault, provider)) == "failed:telegram_unavailable"
    (row,) = await regs(engine)
    assert row["attempts"] == 1 and row["last_error"] == "telegram_unavailable"
    sent = [a for a in await actions(engine) if a["action"].startswith("sponsor.bot_")]
    assert sent == []  # nothing reached the bot, so nothing is logged as sent
    async with engine.connect() as conn:
        assert (await pacing.load(conn)).sent == ()  # no message slot was taken
    text_ = logs.getvalue()
    for needle in (secret, api.api_hash.reveal(), session.reveal()):
        assert needle not in text_
        await assert_absent_from_database(engine, needle)


async def test_paced_busy_and_idle(engine: AsyncEngine, vault: Vault, seal_key: bytes) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    await connect_account(engine)
    assert await service.run_once(deps(engine, vault, provider)) == "idle"  # no proxy at all
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.20")
    now = datetime.now(UTC)
    async with engine.begin() as conn:  # 28 messages this hour: a registration needs 3
        sent = tuple(now - timedelta(minutes=50) + timedelta(seconds=30 * i) for i in range(28))
        await pacing.save(conn, pacing.Pace(sent=sent))
    assert await service.run_once(deps(engine, vault, provider)) == "paced"
    async with engine.begin() as conn:  # Telegram asked the account to wait
        await pacing.save(conn, pacing.Pace())
        await pacing.hold(conn, now + timedelta(minutes=5), now)
    assert await service.run_once(deps(engine, vault, provider)) == "paced"
    async with engine.begin() as conn:
        await pacing.save(conn, pacing.Pace())
    async with account_store.account_lock(engine, wait_s=1):  # the session check or a login holds it
        assert await service.run_once(deps(engine, vault, provider, lock_wait_s=0.3)) == "busy"
    (row,) = await regs(engine)
    assert row["attempts"] == 0  # neither counted as a failure


# --- registrations follow the nodes -------------------------------------------------------------------


async def test_a_replacement_node_at_the_same_slot_name_reuses_the_registration(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    old = await create_proxy_node(engine, seal_key, ipv4="203.0.113.21")
    provider = TelemtUsersProvider(seal_key, host_of={old: "slot3.example.com"})
    reg_id = await _registered_row(engine, provider, vault)
    d = deps(engine, vault, provider)
    await service._record(d, await work_for(engine, provider, reg_id), FlowResult(result=Registered(tag=TAG)))
    # The node burns; a new one answers at the same slot name (the DNS record moved), same user.
    await set_node_state(engine, old, "deleted")
    new = await create_proxy_node(engine, seal_key, ipv4="203.0.113.22")
    provider.host_of[new] = "slot3.example.com"
    assert await service.run_once(d) == "no_account"
    (row,) = await regs(engine)
    assert row["id"] == reg_id and row["state"] == "registered" and row["proxy_key"] == f"mtproto:{new}:s1"
    assert await user_tags(engine, new) == {"s1": TAG}  # no new registration, the tag is reused
    assert [a["action"] for a in await actions(engine)][-1] == "sponsor.tag_applied"


async def test_an_address_change_registers_again_and_a_deleted_node_retires(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.23")
    await service.run_once(deps(engine, vault, provider))
    await set_node_state(engine, node, "ready", ipv4="203.0.113.24")  # no domain: the IP is the host
    await service.run_once(deps(engine, vault, provider))
    rows = await regs(engine)
    assert [(r["host"], r["state"], r["state_reason"]) for r in rows] == [
        ("203.0.113.23", "retired", "address_changed"),
        ("203.0.113.24", "pending", None),
    ]
    await set_node_state(engine, node, "deleted")
    await service.run_once(deps(engine, vault, provider))
    assert [r["state"] for r in await regs(engine)] == ["retired", "retired"]
    assert (await regs(engine))[1]["state_reason"] == "node_gone"


async def test_a_campaign_switches_the_wanted_channel_and_back(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.25")
    reg_id = await _registered_row(engine, provider, vault)
    d = deps(engine, vault, provider)
    await service._record(d, await work_for(engine, provider, reg_id), FlowResult(result=Registered(tag=TAG)))
    await service._record(
        d, await work_for(engine, provider, reg_id), FlowResult(result=PromotionSet("ipoxxo"))
    )
    now = datetime.now(UTC)
    data = service.CampaignInput(
        customer=None, channel="https://t.me/customer_one", starts_at=now + timedelta(minutes=30),
        ends_at=now + timedelta(hours=3), price=None, currency=None, paid=False,
    )  # fmt: skip
    campaign_id = await service.create_campaign(engine, data, actor="admin:o", now=now)
    view = await service.overview(engine, now=now, provider_ready=True)
    assert view["wanted_channel"] == "customer_one" and view["active_campaign_id"] == str(campaign_id)
    assert view["registrations"][0]["in_sync"] is False
    (reg,) = await load(engine)
    nodes = {node: "ready"}
    chosen = service.choose([reg], live={}, nodes=nodes, wanted="customer_one", default="ipoxxo",
                            campaign_channels={"customer_one"}, now=now)  # fmt: skip
    assert chosen is not None and chosen.priority == 0 and chosen.job.channel == "customer_one"  # type: ignore[union-attr]
    await service._record(d, chosen, FlowResult(result=PromotionSet("customer_one")))
    view = await service.overview(engine, now=now, provider_ready=True)
    assert (
        view["registrations"][0]["in_sync"] is True and view["registrations"][0]["visible_from"] is not None
    )
    # The owner cancels: ipoxxo is wanted again, and the switch back comes first.
    await service.cancel_campaign(engine, campaign_id, actor="admin:o", now=now)
    view = await service.overview(engine, now=now, provider_ready=True)
    assert view["wanted_channel"] == "ipoxxo" and view["registrations"][0]["in_sync"] is False
    (reg,) = await load(engine)
    back = service.choose([reg], live={}, nodes=nodes, wanted="ipoxxo", default="ipoxxo",
                          campaign_channels={"customer_one"}, now=now)  # fmt: skip
    assert back is not None and back.priority == 0 and back.job.channel == "ipoxxo"  # type: ignore[union-attr]


# --- pacing on the real database ------------------------------------------------------------------------


async def test_message_slots_keep_the_gap_the_hourly_cap_and_the_kill_switch(engine: AsyncEngine) -> None:
    await kill_switch(engine, False)
    clock = [datetime(2026, 11, 4, 12, 0, tzinfo=UTC)]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:  # test time: the clock moves when we wait
        slept.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    async def take() -> datetime:
        return await pacing.take_slot(engine, clock=lambda: clock[0], sleep=sleep)

    first = await take()
    second = await take()
    assert second - first == timedelta(seconds=5) and slept == [5.0]
    clock[0] += timedelta(seconds=60)
    for _ in range(pacing.HOURLY_CAP - 2):
        await take()
    with pytest.raises(PaceStop) as caught:
        await take()
    assert caught.value.reason == "hourly_cap"
    async with engine.connect() as conn:
        assert len((await pacing.load(conn)).sent) == pacing.HOURLY_CAP
    clock[0] += timedelta(hours=1)
    await take()  # the hour has passed
    async with engine.connect() as conn:
        assert len((await pacing.load(conn)).sent) == 1  # old times are dropped
    async with engine.begin() as conn:  # FLOOD_WAIT: nothing goes out until the hold has passed
        await pacing.hold(conn, clock[0] + timedelta(minutes=3), clock[0])
    with pytest.raises(PaceStop) as caught:
        await take()
    assert caught.value.reason == "hold"
    clock[0] += timedelta(minutes=3)
    await take()
    async with engine.connect() as conn:
        assert (await pacing.load(conn)).hold_until is None  # a passed hold is dropped
    await kill_switch(engine, True)
    with pytest.raises(PaceStop) as caught:
        await take()
    assert caught.value.reason == "kill_switch"


async def test_two_workers_never_share_a_slot(engine: AsyncEngine) -> None:
    await kill_switch(engine, False)
    base = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    clocks = {"a": [base], "b": [base]}

    def taker(name: str) -> object:
        async def sleep(seconds: float) -> None:
            clocks[name][0] += timedelta(seconds=seconds)
            await asyncio.sleep(0)

        return pacing.take_slot(engine, clock=lambda: clocks[name][0], sleep=sleep)

    got = await asyncio.gather(taker("a"), taker("b"))  # type: ignore[call-overload]
    assert sorted(got)[1] - sorted(got)[0] >= timedelta(seconds=5)


# --- the reconciler -----------------------------------------------------------------------------------


async def test_the_reconciler_runs_under_its_own_lock_and_without_a_vault_does_nothing(
    engine: AsyncEngine, vault: Vault
) -> None:
    from proxyhub.reconcilers.sponsor import SponsorReconciler
    from proxyhub.vault import VaultKeyError
    from proxyhub.worker import reconcilers

    assert any(isinstance(r, SponsorReconciler) for r in reconcilers())

    def no_vault() -> Vault:
        raise VaultKeyError("no master key")

    broken = SponsorReconciler(vault_factory=no_vault, provider=lambda: None)
    assert await broken.try_run_once(engine) is True and broken.last_outcome == "vault_unavailable"
    ok = SponsorReconciler(vault_factory=lambda: vault, provider=lambda: None)
    assert await ok.try_run_once(engine) is True and ok.last_outcome == "kill_switch"
    await kill_switch(engine, False)
    assert await ok.try_run_once(engine) is True and ok.last_outcome == "no_provider"
    from proxyhub.reconcilers.base import lock_key

    async with engine.connect() as conn:  # another worker holds the loop's lock
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": lock_key("sponsor")})
        await conn.commit()
        assert await ok.try_run_once(engine) is False
        await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key("sponsor")})
        await conn.commit()
    with pytest.raises(NotImplementedError):
        async with engine.begin() as conn:
            await ok.run_once(conn)


def test_timing_defaults_are_human() -> None:
    timing = Timing()
    assert timing.poll_s >= 2 and timing.reply_timeout_s <= 60
    assert pacing.MIN_GAP >= timedelta(seconds=5) and pacing.HOURLY_CAP <= 30


async def test_the_driver_reads_before_it_takes_a_slot_and_sends_nothing_without_telegram(
    engine: AsyncEngine,
) -> None:
    await kill_switch(engine, False)
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)
    slots: list[object] = []
    lines: list[tuple[str, object]] = []

    class Lines:
        async def sent(self, step: Step, shown: str) -> None:
            lines.append(("sent", shown))

        async def pressed(self, step: Step, label: str, answer: str | None) -> None:
            lines.append(("pressed", label))

        async def reply(self, step: Step, text: str, buttons: list[str]) -> None:
            lines.append(("reply", text))

    async def slot() -> object:
        slots.append(await pacing.take_slot(engine, clock=lambda: datetime.now(UTC)))
        return slots[-1]

    with contextlib.ExitStack() as hold:  # a real client that never connected: a real refusal
        op = OperatorSession(userclient._new_client(creds, None, options), creds, options, hold)
        op._client._mtproxybot_id = BOT_ID
        chat = BotChat(op, types.InputPeerUser(user_id=BOT_ID, access_hash=1))
        job = RegisterJob(host="203.0.113.30", port=8443, secret_hex=secrets.token_hex(16))
        got = await driver.run(chat, job, take_slot=slot, record=Lines(), timing=Timing())
    assert got.error == "telegram_unavailable" and got.messages_sent == 0 and got.last_step is None
    assert slots == [] and lines == []
    async with engine.connect() as conn:
        assert (await pacing.load(conn)).sent == ()
