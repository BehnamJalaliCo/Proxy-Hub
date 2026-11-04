"""The sponsor module on the REAL database (decision D10).

* The live proxies come from :class:`test_sponsor_support.TelemtUsersProvider` over the real
  ``nodes``/``telemt_users`` tables with real sealed secrets. It lists exactly what team mtproto's
  ``proxies_to_register`` lists (a proxy named by a ``pending``/``registered`` registration, or
  whose Telemt user has a tag, is not listed), and a tag lands in the node's real desired state.
* What a conversation ended with (:class:`proxyhub.sponsor.driver.FlowResult`) is fed to the
  recording step as data: nothing plays @MTProxybot. A real conversation attempt goes to a local
  port nobody listens on (a real refusal), so no message is sent and none is logged. The driver's
  own tests use a chat in which nobody ever answers (no reply is ever made up).
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
    registration_rows,
    regs,
    set_node_state,
    user_name,
    user_tags,
)
from test_telegram_support import all_logs

from proxyhub.agent_api.desired import build_desired_state
from proxyhub.mtproxy import store as account_store
from proxyhub.mtproxy import userclient
from proxyhub.mtproxy.userclient import (
    LIVE_SECRETS,
    ApiCredentials,
    BotChat,
    BotMessage,
    ClientOptions,
    Endpoint,
    MtpError,
    OperatorSession,
)
from proxyhub.sponsor import driver, pacing, service, store
from proxyhub.sponsor.conversation import (
    PromoteJob,
    PromotionSet,
    Registered,
    RegisterJob,
    Send,
    Step,
    Stuck,
)
from proxyhub.sponsor.driver import FlowResult, Timing
from proxyhub.sponsor.pacing import PaceStop
from proxyhub.sponsor.service import Deps, Work
from proxyhub.vault import SecretValue, Vault

pytestmark = [pytest.mark.db]

BOT_ID = 93_372_553
TAG = "ecfe4482355b04af36e816044653e999"
NOON = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


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


async def listed(engine: AsyncEngine, provider: TelemtUsersProvider) -> list[str]:
    async with engine.connect() as conn:
        return [p.key for p in await provider.proxies_to_register(conn)]


def key_of(node: uuid.UUID, user: str | None = None) -> str:
    return f"mtproto:{node}:{user or user_name(node)}"


async def work_for(engine: AsyncEngine, provider: TelemtUsersProvider, reg_id: uuid.UUID) -> Work:
    """The job the reconciler would build for this proxy (the secret from the provider)."""
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


async def candidate(engine: AsyncEngine, provider: TelemtUsersProvider, vault: Vault) -> uuid.UUID:
    """One pass that finds the proxy; the id of its row (a candidate: no registration yet)."""
    await service.run_once(deps(engine, vault, provider))
    (found,) = [r["id"] for r in await regs(engine) if r["registration_id"] is None]
    return found  # type: ignore[no-any-return]


async def register(
    engine: AsyncEngine, vault: Vault, provider: TelemtUsersProvider, **kw: object
) -> uuid.UUID:
    """A proxy with its tag from the bot (what a finished registration conversation records)."""
    reg_id = await candidate(engine, provider, vault)
    work = await work_for(engine, provider, reg_id)
    outcome = FlowResult(result=Registered(tag=TAG), last_step=Step.SENT_SECRET)
    assert await service._record(deps(engine, vault, provider, **kw), work, outcome) == "registered"
    return reg_id


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


async def test_a_live_proxy_is_a_candidate_with_no_registration_row_until_it_has_a_tag(
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
        ("203.0.113.11", 8443, user_name(ready), "pending")
    ]
    assert rows[0]["proxy_key"] == key_of(ready) and rows[0]["registration_id"] is None
    # No mtproxy_registrations row: team mtproto's provider keeps listing the proxy with its secret.
    assert await registration_rows(engine) == []
    assert await listed(engine, provider) == [key_of(ready)]
    assert [a["action"] for a in await actions(engine)] == ["sponsor.proxy_found"]
    await assert_absent_from_database(engine, secret)
    assert secret not in logs.getvalue()


async def test_a_pass_without_a_conversation_or_a_failed_try_never_hides_a_proxy(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    """Two new proxies first seen while nobody can talk, then while the hour's budget is spent,
    then one conversation per pass (each fails: Telegram is unreachable): both stay listed, both
    are tried, and both are tried again after their backoff."""
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    a = await create_proxy_node(engine, seal_key, ipv4="203.0.113.31")
    b = await create_proxy_node(engine, seal_key, ipv4="203.0.113.32")
    both = sorted([key_of(a), key_of(b)])
    clock = [NOON]
    d = deps(engine, vault, provider, clock=lambda: clock[0])
    assert await service.run_once(d) == "no_account"
    assert sorted(await listed(engine, provider)) == both and await registration_rows(engine) == []
    await seed_connected(engine, vault, port=closed_port(), mtproxybot_id=BOT_ID)  # Telegram unreachable
    async with engine.begin() as conn:
        spent = tuple(
            NOON - timedelta(minutes=30) + timedelta(seconds=10 * i) for i in range(pacing.HOURLY_CAP)
        )
        await pacing.save(conn, pacing.Pace(sent=spent))
    assert await service.run_once(d) == "paced"
    async with engine.begin() as conn:
        await pacing.save(conn, pacing.Pace())
    assert [await service.run_once(d) for _ in range(2)] == ["failed:telegram_unavailable"] * 2
    assert sorted(r["attempts"] for r in await regs(engine)) == [1, 1]
    assert sorted(await listed(engine, provider)) == both
    assert await service.run_once(d) == "idle"  # both wait for their backoff
    clock[0] += timedelta(minutes=6)
    assert [await service.run_once(d) for _ in range(2)] == ["failed:telegram_unavailable"] * 2
    rows = await regs(engine)
    assert [r["attempts"] for r in rows] == [2, 2] and {r["state"] for r in rows} == {"pending"}
    assert await registration_rows(engine) == [] and sorted(await listed(engine, provider)) == both


# --- recording what a conversation ended with ----------------------------------------------------------


async def test_a_tag_from_the_bot_is_the_registration_and_lands_in_the_nodes_desired_state(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.13")
    reg_id = await register(engine, vault, provider)
    (row,) = await regs(engine)
    assert row["id"] == reg_id and row["state"] == "registered" and row["ad_tag"] == TAG
    assert row["attempts"] == 0 and row["tag_applied_at"] is not None
    (written,) = await registration_rows(engine)
    assert written["id"] == row["registration_id"] and written["secret_name"] == user_name(node)
    assert written["state"] == "registered" and written["ad_tag"] == TAG
    assert await user_tags(engine, node) == {user_name(node): TAG}
    async with AsyncSession(engine) as session:
        desired = await build_desired_state(session, seal_key, node)
    assert desired["telemt"]["users"][0]["ad_tag"] == TAG  # what the node's agent will apply
    logged = await actions(engine)
    assert [a["action"] for a in logged][-2:] == ["sponsor.registered", "sponsor.tag_applied"]
    assert TAG not in json.dumps([a["reasons"] for a in logged])  # logs carry the short form only
    # Registered now: the provider no longer lists it, and the next job is its sponsor.
    assert await listed(engine, provider) == []
    (reg,) = await load(engine)
    nodes = {node: "ready"}
    chosen = service.choose([reg], live={}, nodes=nodes, wanted="ipoxxo", default="ipoxxo",
                            campaign_channels=set(), now=datetime.now(UTC))  # fmt: skip
    assert chosen is not None and chosen.job == PromoteJob(host="203.0.113.13", port=8443, channel="ipoxxo")
    d = deps(engine, vault, provider)
    assert (
        await service._record(d, chosen, FlowResult(result=PromotionSet(channel="ipoxxo"))) == "promotion_set"
    )
    (row,) = await regs(engine)
    assert row["sponsor_channel"] == "ipoxxo" and row["promotion_set_at"] is not None
    (reg,) = await load(engine)
    assert service.choose([reg], live={}, nodes=nodes, wanted="ipoxxo", default="ipoxxo",
                          campaign_channels=set(), now=datetime.now(UTC)) is None  # fmt: skip


async def test_a_tag_the_node_could_not_take_is_tried_again(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    """The provider never lists a registered proxy again, so the module itself tries the tag
    again (every TAG_RETRY) until the node's desired state carries it."""
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.14")
    clock = [NOON]
    d = deps(engine, vault, provider, clock=lambda: clock[0])
    provider.fail_apply = True
    await register(engine, vault, provider, clock=lambda: clock[0])
    (row,) = await regs(engine)
    assert row["ad_tag"] == TAG and row["state"] == "registered"
    assert row["tag_applied_at"] is None and row["tag_failed_at"] == NOON
    assert await user_tags(engine, node) == {user_name(node): None}
    assert await listed(engine, provider) == []  # the registration row hides it from the provider
    provider.fail_apply = False
    clock[0] += timedelta(minutes=5)
    assert await service.run_once(d) == "no_account"
    assert await user_tags(engine, node) == {user_name(node): None}  # not before TAG_RETRY
    clock[0] = NOON + service.TAG_RETRY
    assert await service.run_once(d) == "no_account"
    assert await user_tags(engine, node) == {user_name(node): TAG}
    (row,) = await regs(engine)
    assert row["tag_applied_at"] == clock[0] and row["tag_failed_at"] is None
    assert [a["action"] for a in await actions(engine)][-3:] == [
        "sponsor.registered",
        "sponsor.tag_apply_failed",
        "sponsor.tag_applied",
    ]
    clock[0] += timedelta(hours=1)
    assert await service.run_once(d) == "no_account"
    assert provider.applied == [(key_of(node), TAG)]  # once, not on every pass


async def test_a_tag_for_a_proxy_that_went_away_is_kept_on_record_and_reaches_no_node(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.15")
    reg_id = await candidate(engine, provider, vault)
    work = await work_for(engine, provider, reg_id)
    await set_node_state(engine, node, "deleted")
    assert await service.run_once(deps(engine, vault, provider)) == "no_account"
    assert await regs(engine) == []  # nothing was registered for it: the candidate is dropped
    assert [a["action"] for a in await actions(engine)][-1] == "sponsor.candidate_dropped"
    outcome = FlowResult(result=Registered(tag=TAG), last_step=Step.SENT_SECRET)
    assert await service._record(deps(engine, vault, provider), work, outcome) == "registered"
    (row,) = await regs(engine)
    assert row["state"] == "retired" and row["state_reason"] == "proxy_gone" and row["ad_tag"] == TAG
    assert provider.applied == [] and await user_tags(engine, node) == {user_name(node): None}


async def test_a_row_written_elsewhere_for_the_same_address_and_user_takes_the_tag(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.16")
    other = uuid.uuid4()
    async with engine.begin() as conn:  # e.g. an old failed attempt, not by this module
        await conn.execute(
            text(
                "INSERT INTO mtproxy_registrations (id, host, port, secret_name, state) "
                "VALUES (:id, '203.0.113.16', 8443, :u, 'failed')"
            ),
            {"id": other, "u": user_name(node)},
        )
    await register(engine, vault, provider)
    (row,) = await regs(engine)
    assert row["registration_id"] == other and row["state"] == "registered" and row["ad_tag"] == TAG
    assert [r["id"] for r in await registration_rows(engine)] == [other]
    assert await user_tags(engine, node) == {user_name(node): TAG}


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
    first_id, second_id = [r["id"] for r in sorted(await regs(engine), key=lambda r: r["host"])]
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
    # The same proxy stuck again (no retry in between): no second alert.
    assert (
        await service._record(d, await work_for(engine, provider, first_id), stuck) == "stuck:address_refused"
    )
    assert len(await alerts(engine)) == 1
    # One stuck proxy does not stop the others ...
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


async def test_retry_on_a_stuck_proxy_without_a_tag_leads_to_a_new_attempt(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.33")
    reg_id = await candidate(engine, provider, vault)
    await seed_connected(engine, vault, port=closed_port(), mtproxybot_id=BOT_ID)
    d = deps(engine, vault, provider)
    stuck = FlowResult(
        result=Stuck(step=Step.SENT_ADDRESS, reason="address_refused"),
        last_step=Step.SENT_ADDRESS,
        last_bot_text="Hmm.",
    )
    assert (
        await service._record(d, await work_for(engine, provider, reg_id), stuck) == "stuck:address_refused"
    )
    (row,) = await regs(engine)
    assert row["state"] == "failed" and row["registration_id"] is None
    assert await service.run_once(d) == "idle"  # a stuck proxy waits for the owner
    await service.retry(engine, reg_id, actor="admin:owner", now=datetime.now(UTC))
    assert await listed(engine, provider) == [key_of(node)]  # still listed, with its secret ...
    assert await service.run_once(d) == "failed:telegram_unavailable"  # ... so it is tried again
    (row,) = await regs(engine)
    assert row["state"] == "pending" and row["attempts"] == 1 and row["stuck_reason"] is None


async def test_an_alert_that_had_nobody_to_go_to_is_sent_once_the_owner_is_paired(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.34")
    reg_id = await candidate(engine, provider, vault)
    d = deps(engine, vault, provider)
    stuck = FlowResult(result=Stuck(step=Step.SENT_NEWPROXY, reason="unexpected_reply"))
    assert (
        await service._record(d, await work_for(engine, provider, reg_id), stuck) == "stuck:unexpected_reply"
    )
    (row,) = await regs(engine)
    assert row["alerted_at"] is None and await alerts(engine) == []  # no chat paired: nobody to tell
    assert await service.run_once(d) == "no_account"
    assert await alerts(engine) == []
    await pair_owner(engine)
    assert await service.run_once(d) == "no_account"
    (alert,) = await alerts(engine)
    assert "203.0.113.34:8443" in alert and "پس از فرمان" in alert
    (row,) = await regs(engine)
    assert row["alerted_at"] is not None
    assert await service.run_once(d) == "no_account"
    assert len(await alerts(engine)) == 1  # once


async def test_failures_back_off_and_a_silent_bot_becomes_stuck(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    await pair_owner(engine)
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.17")
    reg_id = await candidate(engine, provider, vault)
    clock = [NOON]
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
    reg_id = await candidate(engine, provider, vault)
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


async def test_the_conversation_checks_the_kill_switch_again_under_the_account_lock(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    await create_proxy_node(engine, seal_key, ipv4="203.0.113.35")
    reg_id = await candidate(engine, provider, vault)
    await seed_connected(engine, vault, port=closed_port(), mtproxybot_id=BOT_ID)
    work = await work_for(engine, provider, reg_id)
    assert isinstance(await service._converse(deps(engine, vault, provider), work), FlowResult)
    await kill_switch(engine, True)  # switched ON after the pass chose its work
    assert await service._converse(deps(engine, vault, provider), work) == "kill_switch"


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


async def test_a_replacement_node_is_a_new_registration_and_the_old_one_retires(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    """Team mtproto names users per node, so the node that replaces a burnt one at the same slot
    name is registered on its own; the old entry retires with its node."""
    await kill_switch(engine, False)
    old = await create_proxy_node(engine, seal_key, ipv4="203.0.113.21")
    provider = TelemtUsersProvider(seal_key, host_of={old: "slot3.example.com"})
    await register(engine, vault, provider)
    await set_node_state(engine, old, "deleted")
    new = await create_proxy_node(engine, seal_key, ipv4="203.0.113.22")
    provider.host_of[new] = "slot3.example.com"
    assert await service.run_once(deps(engine, vault, provider)) == "no_account"
    rows = await regs(engine)
    assert [(r["host"], r["secret_name"], r["state"], r["state_reason"]) for r in rows] == [
        ("slot3.example.com", user_name(old), "retired", "node_gone"),
        ("slot3.example.com", user_name(new), "pending", None),
    ]
    assert rows[1]["proxy_key"] == key_of(new) and rows[1]["registration_id"] is None


async def test_the_same_user_at_the_same_address_gets_its_registration_back(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    """A provider that lists the same user name at the same address again (per-slot names, say)
    gets the existing entry and its tag: no second registration in @MTProxybot."""
    await kill_switch(engine, False)
    old = await create_proxy_node(engine, seal_key, ipv4="203.0.113.23", users=("slot3",))
    provider = TelemtUsersProvider(seal_key, host_of={old: "slot3.example.com"})
    reg_id = await register(engine, vault, provider)
    await set_node_state(engine, old, "deleted")
    async with engine.begin() as conn:  # the old node's proxy is gone with it
        await conn.execute(text("DELETE FROM telemt_users WHERE node_id = :n"), {"n": old})
    new = await create_proxy_node(engine, seal_key, ipv4="203.0.113.24", users=("slot3",))
    provider.host_of[new] = "slot3.example.com"
    d = deps(engine, vault, provider)
    assert await service.run_once(d) == "no_account"  # the old entry retires with its node ...
    assert [r["state"] for r in await regs(engine)] == ["retired"]
    assert await service.run_once(d) == "no_account"  # ... and the new node takes it back
    (row,) = await regs(engine)
    assert row["id"] == reg_id and row["state"] == "registered" and row["proxy_key"] == key_of(new, "slot3")
    assert await user_tags(engine, new) == {"slot3": TAG}
    assert len(await registration_rows(engine)) == 1
    assert [a["action"] for a in await actions(engine)][-2:] == [
        "sponsor.registration_reused",
        "sponsor.tag_applied",
    ]


async def test_addresses_and_deleted_nodes(engine: AsyncEngine, vault: Vault, seal_key: bytes) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    waiting = await create_proxy_node(engine, seal_key, ipv4="203.0.113.25")
    done = await create_proxy_node(engine, seal_key, ipv4="203.0.113.26")
    d = deps(engine, vault, provider)
    await service.run_once(d)
    by_key = {r["proxy_key"]: r["id"] for r in await regs(engine)}
    work = await work_for(engine, provider, by_key[key_of(done)])
    await service._record(d, work, FlowResult(result=Registered(tag=TAG)))
    # Nothing registered yet: a candidate follows its proxy's new address (no domain: the IP).
    await set_node_state(engine, waiting, "ready", ipv4="203.0.113.27")
    # Registered: the provider does not list it again, so its entry keeps the registered address.
    await set_node_state(engine, done, "ready", ipv4="203.0.113.28")
    await service.run_once(d)
    rows = {r["proxy_key"]: r for r in await regs(engine)}
    assert rows[key_of(waiting)]["host"] == "203.0.113.27" and rows[key_of(waiting)]["state"] == "pending"
    assert rows[key_of(done)]["host"] == "203.0.113.26" and rows[key_of(done)]["state"] == "registered"
    assert "sponsor.candidate_moved" in [a["action"] for a in await actions(engine)]
    assert len(await registration_rows(engine)) == 1
    # Deleted nodes: the candidate is dropped (nothing to keep), the registration retires.
    await set_node_state(engine, waiting, "deleted")
    await set_node_state(engine, done, "deleted")
    await service.run_once(d)
    (row,) = await regs(engine)
    assert (row["host"], row["state"], row["state_reason"]) == ("203.0.113.26", "retired", "node_gone")


async def test_a_registration_listed_again_at_a_new_address_retires_and_the_proxy_registers_again(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    """The one way team mtproto's provider lists a proxy that has a registration again: its tag
    never reached the node and its promotion got stuck (the row is ``failed``, not ``registered``)."""
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.36")
    provider.fail_apply = True
    reg_id = await register(engine, vault, provider)
    stuck = FlowResult(result=Stuck(step=Step.SENT_CHANNEL, reason="channel_refused"))
    await service._record(deps(engine, vault, provider), await work_for(engine, provider, reg_id), stuck)
    assert await listed(engine, provider) == [key_of(node)]
    await set_node_state(engine, node, "ready", ipv4="203.0.113.37")
    await service.run_once(deps(engine, vault, provider))
    rows = await regs(engine)
    assert [(r["host"], r["state"], r["state_reason"], r["stuck_reason"]) for r in rows] == [
        ("203.0.113.36", "retired", "address_changed", None),
        ("203.0.113.37", "pending", None, None),
    ]


async def test_a_campaign_switches_the_wanted_channel_and_back(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await kill_switch(engine, False)
    provider = TelemtUsersProvider(seal_key)
    node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.29")
    reg_id = await register(engine, vault, provider)
    d = deps(engine, vault, provider)
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
    assert view["registrations"][0]["in_sync"] is False and view["registrations"][0]["tag_on_node"] is True
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
    clock = [NOON]
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
    clocks = {"a": [NOON], "b": [NOON]}

    def taker(name: str) -> object:
        async def sleep(seconds: float) -> None:
            clocks[name][0] += timedelta(seconds=seconds)
            await asyncio.sleep(0)

        return pacing.take_slot(engine, clock=lambda: clocks[name][0], sleep=sleep)

    got = await asyncio.gather(taker("a"), taker("b"))  # type: ignore[call-overload]
    assert sorted(got)[1] - sorted(got)[0] >= timedelta(seconds=5)


# --- the driver: no message without a slot ---------------------------------------------------------------


class Lines:
    """The driver's actions_log lines, kept in a list (the service writes them to actions_log)."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []

    async def sending(self, step: Step, shown: str) -> None:
        self.events.append(f"line:{shown}")

    async def pressing(self, step: Step, label: str) -> None:
        self.events.append(f"line:{label}")

    async def reply(self, step: Step, text: str, buttons: list[str]) -> None:
        self.events.append(f"reply:{text}")


class SilentChat:
    """A chat in which nobody ever answers: what the driver sees while @MTProxybot says nothing.
    It makes up no reply (decision D10). A message going out fails the test unless allowed."""

    def __init__(self, events: list[str], *, allow_send: bool = False) -> None:
        self.events = events
        self.allow_send = allow_send

    async def recent(self, limit: int = 10) -> list[BotMessage]:
        self.events.append("read")
        return []

    async def send(self, text: str, *, sensitive: bool = False) -> int | None:
        if not self.allow_send:
            raise AssertionError("a message went out without a pacing slot")
        self.events.append(f"send:sensitive={sensitive}")
        return None

    async def press(self, message_id: int, data: bytes) -> str | None:
        raise AssertionError("a button was pressed")


@pytest.mark.parametrize("stop", ["kill_switch", "hourly_cap", "hold"])
async def test_no_message_goes_out_without_a_pacing_slot(engine: AsyncEngine, stop: str) -> None:
    await kill_switch(engine, stop == "kill_switch")
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        if stop == "hourly_cap":
            full = tuple(
                now - timedelta(minutes=40) + timedelta(seconds=10 * i) for i in range(pacing.HOURLY_CAP)
            )
            await pacing.save(conn, pacing.Pace(sent=full))
        elif stop == "hold":  # Telegram asked the account to wait (FLOOD_WAIT)
            await pacing.hold(conn, now + timedelta(minutes=5), now)

    async def slot() -> object:
        return await pacing.take_slot(engine, clock=lambda: datetime.now(UTC))

    events: list[str] = []
    lines = Lines(events)
    for job in (
        RegisterJob(host="203.0.113.30", port=8443, secret_hex=secrets.token_hex(16)),
        PromoteJob(host="203.0.113.30", port=8443, channel="ipoxxo"),
    ):
        got = await driver.run(SilentChat(events), job, take_slot=slot, record=lines, timing=Timing())
        assert (got.error, got.result, got.messages_sent, got.last_step) == (stop, None, 0, None)
    assert events == ["read", "read"]  # the chat was read; nothing was logged, nothing went out


async def test_the_slot_comes_first_then_the_log_line_then_the_message(engine: AsyncEngine) -> None:
    await kill_switch(engine, False)
    events: list[str] = []

    async def slot() -> object:
        taken = await pacing.take_slot(engine, clock=lambda: datetime.now(UTC))
        events.append("slot")
        return taken

    async def no_wait(seconds: float) -> None:
        return None

    job = RegisterJob(host="203.0.113.30", port=8443, secret_hex=secrets.token_hex(16))
    timing = Timing(poll_s=1, settle_s=1, reply_timeout_s=3)
    got = await driver.run(
        SilentChat(events, allow_send=True),
        job,
        take_slot=slot,
        record=Lines(events),
        timing=timing,
        sleep=no_wait,
    )
    assert (got.error, got.messages_sent, got.last_step) == ("no_reply", 1, Step.SENT_NEWPROXY)
    assert events == ["read", "slot", "line:/newproxy", "send:sensitive=False", "read", "read", "read"]
    async with engine.connect() as conn:
        assert len((await pacing.load(conn)).sent) == 1


async def test_the_secret_goes_out_only_as_a_sensitive_message(engine: AsyncEngine) -> None:
    """The driver hands the secret step's ``sensitive`` flag to the real chat, which keeps the
    secret out of every log line before the request is even tried (here: a real client with no
    connection, so Telegram refuses)."""
    await kill_switch(engine, False)
    secret, plain = secrets.token_hex(16), f"203.0.113.{secrets.randbelow(200)}:8443"
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)

    clock = [NOON]

    async def sleep(seconds: float) -> None:  # test time: the 5 s gap passes at once
        clock[0] += timedelta(seconds=seconds)

    async def slot() -> object:
        return await pacing.take_slot(engine, clock=lambda: clock[0], sleep=sleep)

    events: list[str] = []
    with contextlib.ExitStack() as hold:  # what operator_session holds for one connection
        op = OperatorSession(userclient._new_client(creds, None, options), creds, options, hold)
        op._client._mtproxybot_id = BOT_ID
        chat = BotChat(op, types.InputPeerUser(user_id=BOT_ID, access_hash=1))
        for text_, sensitive in ((plain, False), (secret, True)):
            action = Send(
                text=text_,
                shown="[secret]" if sensitive else text_,
                then=Step.SENT_ADDRESS,
                sensitive=sensitive,
            )
            with pytest.raises(MtpError):
                await driver.perform(chat, action, take_slot=slot, record=Lines(events), secrets=[secret])
        assert secret in LIVE_SECRETS._live and plain not in LIVE_SECRETS._live
    assert secret not in LIVE_SECRETS._live  # released with the connection
    assert events == [f"line:{plain}", "line:[secret]"]


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
    lines: list[str] = []

    async def slot() -> object:
        slots.append(await pacing.take_slot(engine, clock=lambda: datetime.now(UTC)))
        return slots[-1]

    with contextlib.ExitStack() as hold:  # a real client that never connected: a real refusal
        op = OperatorSession(userclient._new_client(creds, None, options), creds, options, hold)
        op._client._mtproxybot_id = BOT_ID
        chat = BotChat(op, types.InputPeerUser(user_id=BOT_ID, access_hash=1))
        job = RegisterJob(host="203.0.113.30", port=8443, secret_hex=secrets.token_hex(16))
        got = await driver.run(chat, job, take_slot=slot, record=Lines(lines), timing=Timing())
    assert got.error == "telegram_unavailable" and got.messages_sent == 0 and got.last_step is None
    assert slots == [] and lines == []
    async with engine.connect() as conn:
        assert (await pacing.load(conn)).sent == ()
