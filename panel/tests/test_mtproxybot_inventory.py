"""@MTProxybot's own list: the inventory, the promotion check and the cleanup job, on the real
PostgreSQL and Redis (decision D10).

What a walk ended with (a :class:`~proxyhub.sponsor.driver.WalkResult` around a ListRead, MenuSeen,
Removed or WalkStuck that the pure state machines built from reply shapes as DATA) is fed to the
real job code, exactly where a walk over the real chat would hand it over. Where a step really
talks, Telegram is a local port nobody listens on (a real refusal): nothing imitates @MTProxybot.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from telethon import types
from test_fleet_support import ADMIN, CSRF, make_vault
from test_mtproxy_support import seed_connected
from test_mtproxy_units import closed_port
from test_sponsor_service import Lines, SilentChat
from test_sponsor_support import (
    actions,
    alerts,
    clean,
    connect_account,
    create_proxy_node,
    kill_switch,
    pair_owner,
    set_node_state,
    user_name,
)

from proxyhub.api.deps import require_admin
from proxyhub.mtproxy import userclient
from proxyhub.mtproxy.userclient import (
    ApiCredentials,
    BotButton,
    BotChat,
    BotMessage,
    ClientOptions,
    Endpoint,
    OperatorSession,
)
from proxyhub.sponsor import driver, inventory, pacing, store
from proxyhub.sponsor.botlist import (
    ListRead,
    ListWalk,
    MenuSeen,
    Removed,
    Screen,
    WalkStuck,
)
from proxyhub.sponsor.conversation import Observation, Step
from proxyhub.sponsor.driver import Timing, WalkResult
from proxyhub.sponsor.inventory import InventoryError
from proxyhub.sponsor.service import Deps
from proxyhub.sponsor.store import REGISTERED, RETIRED
from proxyhub.vault import SecretValue, Vault

pytestmark = [pytest.mark.db, pytest.mark.redis]

BOT_ID = 93_372_553
REAL_LIST_HEADER = "Here is the list of all proxies you created:"
LIVE_HOST = "t7aae9f0c.ipoxxo.online"
LIVE_TAG = "4f1c9a0b" + secrets.token_hex(12)
DEAD_TAG = secrets.token_hex(16)
DEAD_IP = "203.0.113.7"
MOVED_IP = "203.0.113.9"
UNKNOWN_IP = "198.51.100.1"
LIVE_LABEL = f"{LIVE_HOST} 4f1c9a0b"  # names the live registration's tag: an exact match
DEAD_LABEL = f"{DEAD_IP} aa11bb22"
MOVED_LABEL = f"{MOVED_IP} cc33dd44"
UNKNOWN_LABEL = f"{UNKNOWN_IP} ee55ff66"
ALL_LABELS = (LIVE_LABEL, DEAD_LABEL, MOVED_LABEL, UNKNOWN_LABEL)


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


def deps(engine: AsyncEngine, vault: Vault) -> Deps:
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=3)
    return Deps(engine=engine, vault=vault, provider=None, options=options, timing=Timing())


async def _reg(
    db: AsyncConnection,
    host: str,
    node: uuid.UUID | None,
    *,
    tag: str,
    state: str = REGISTERED,
    reason: str | None = None,
) -> uuid.UUID:
    """A registration as the sponsor module writes it (its own store code), for the node's user."""
    now = datetime.now(UTC)
    user = user_name(node) if node is not None else f"mt_{secrets.token_hex(8)}"
    key = f"mtproto:{node}:{user}" if node is not None else None
    flow = await store.insert_candidate(db, host=host, port=8443, user_name=user, proxy_key=key, now=now)
    reg_id = await store.create_registration(
        db,
        flow,
        host=host,
        port=8443,
        secret_name=user,
        ad_tag=tag,
        now=now,
        state=state,
        state_reason=reason,
    )
    if state == REGISTERED:  # in sync: @ipoxxo is its sponsor and the tag is on the node
        await store.update_reg(db, reg_id, now, sponsor_channel="ipoxxo")
        await store.update_flow(db, flow, now, tag_applied_at=now)
    return reg_id


async def seed(engine: AsyncEngine, seal_key: bytes) -> dict[str, uuid.UUID]:
    """A live registration (node ready, in sync), one whose node was deleted, one retired at an old
    address (its node lives on), and the account connected with the kill switch OFF."""
    live_node = await create_proxy_node(engine, seal_key, ipv4="203.0.113.50")
    dead_node = await create_proxy_node(engine, seal_key, ipv4=DEAD_IP)
    await set_node_state(engine, dead_node, "deleted")
    async with engine.begin() as db:
        ids = {
            "live": await _reg(db, LIVE_HOST, live_node, tag=LIVE_TAG),
            "dead": await _reg(db, DEAD_IP, dead_node, tag=DEAD_TAG, state=RETIRED, reason="node_gone"),
            "moved": await _reg(
                db, MOVED_IP, None, tag=secrets.token_hex(16), state=RETIRED, reason="address_changed"
            ),
        }
    await connect_account(engine, bot_id=BOT_ID)
    await kill_switch(engine, False)
    return ids


def list_read(*labels: str) -> ListRead:
    """The list walk's result for one page of /myproxies with these buttons (data, not a chat)."""
    rows = tuple((BotButton(label, f"p:{i}".encode()),) for i, label in enumerate(labels))
    page = BotMessage(id=300, outgoing=False, text=REAL_LIST_HEADER, edit_date=None, buttons=rows)
    result = ListWalk().next(Step.SENT_MYPROXIES, Observation(messages=(page,)))
    assert isinstance(result, ListRead)
    return result


SCREEN = Screen(
    stage="proxy_screen", text="Proxy <host:port>", buttons=(("Set promotion", "🗑 Delete proxy"),)
)


def seen_menu(*channels: str, remove_buttons: int = 1, names_proxy: bool = True) -> WalkResult:
    return WalkResult(
        result=MenuSeen(
            channels=channels,
            names_proxy=names_proxy,
            remove_buttons=remove_buttons,
            remove_named=remove_buttons == 1 and names_proxy,
            screens=(SCREEN,),
        ),
        last_step=Step.PRESSED_PROXY,
        messages_sent=2,
    )


async def prepare(engine: AsyncEngine, vault: Vault, *, paused: bool = False) -> inventory._Prepared:
    got = await inventory._prepare(deps(engine, vault), datetime.now(UTC), paused)
    assert isinstance(got, inventory._Prepared), got
    return got


async def record(engine: AsyncEngine, prepared: inventory._Prepared, outcome: WalkResult) -> str:
    async with engine.begin() as db:
        return await inventory._record(db, prepared, outcome, datetime.now(UTC))


async def view(engine: AsyncEngine) -> dict[str, Any]:
    return await inventory.overview(engine, wanted="ipoxxo")


async def job(engine: AsyncEngine) -> inventory.Job:
    async with engine.connect() as db:
        found = (await inventory.load_state(db)).job
    assert found is not None
    return found


async def read_the_list(engine: AsyncEngine, vault: Vault, *labels: str) -> None:
    prepared = await prepare(engine, vault)
    assert prepared.plan.kind in ("list", "relist")
    assert (
        await record(engine, prepared, WalkResult(result=list_read(*labels), messages_sent=1))
        == "mtproxybot:list"
    )


def by_address(v: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["address"]: e for e in v["entries"]}


# --- the inventory ----------------------------------------------------------------------------------------


async def test_the_inventory_maps_every_listed_proxy_and_keeps_no_secret(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    ids = await seed(engine, seal_key)
    queued = await inventory.request_job(
        engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC)
    )
    assert (queued.kind, queued.mode, queued.state) == ("inventory", "look", "queued")
    await read_the_list(engine, vault, *ALL_LABELS)
    v = await view(engine)
    entries = by_address(v)
    assert {a: (e["status"], e["removable"]) for a, e in entries.items()} == {
        LIVE_HOST: ("live", False),
        DEAD_IP: ("gone", True),
        MOVED_IP: ("retired", False),
        UNKNOWN_IP: ("unknown", False),
    }
    assert entries[LIVE_HOST]["exact"] and entries[LIVE_HOST]["registration"]["state"] == "registered"
    assert entries[DEAD_IP]["registration"]["node_state"] == "deleted"
    assert entries[UNKNOWN_IP]["registration"] is None
    assert v["counts"] == {
        "live": 1,
        "gone": 1,
        "retired": 1,
        "unknown": 1,
        "removable": 1,
        "promotion_ok": 0,
        "promotion_problem": 0,
        "promotion_unread": 1,
    }
    assert v["list"]["complete"] and v["list"]["total"] == 4 and v["cleanup_mode"] == "observe"
    async with engine.connect() as db:
        stored = await inventory.load_entries(db)
    assert {e.tag_registration_id for e in stored} == {ids["live"], None}
    dumped = json.dumps(v) + repr(stored)
    for leaked in (LIVE_TAG, DEAD_TAG, "4f1c9a0b", "aa11bb22", "cc33dd44", "ee55ff66"):
        assert leaked not in dumped, leaked
    assert entries[DEAD_IP]["label_shape"] == "<host> <hex>"
    logged = [a for a in await actions(engine) if a["action"] == "sponsor.mtproxybot_list_read"]
    assert logged and logged[0]["reasons"]["reasons"] == ["complete"]
    assert (
        logged[0]["reasons"]["context"] | {"total": 4, "gone": 1, "unknown": 1}
        == logged[0]["reasons"]["context"]
    )
    assert LIVE_TAG not in json.dumps([a["reasons"] for a in await actions(engine)])


async def test_old_registrations_whose_rows_are_gone_are_found_from_what_the_panel_knows(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    """P18: the rows of deleted nodes were removed by hand, so their registrations read "unknown".
    An IPv4 one of our servers had (ip_history) and an older registration of the live address
    (Hetzner gave the IPv4 again) are ours and dead; the live node's own address is not."""
    await seed(engine, seal_key)
    async with engine.begin() as db:
        await db.execute(
            text(
                "INSERT INTO ip_history (ip, prefix24, location, first_seen_at) "
                "VALUES ('203.0.113.77', '203.0.113.0/24', 'hel1', now())"
            )
        )
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(
        engine,
        vault,
        LIVE_LABEL,
        f"{LIVE_HOST} 0d0e0f10",
        "203.0.113.77 1a1b1c1d",
        UNKNOWN_LABEL,
        "203.0.113.50 2a2b2c2d",
    )
    got = [(e["address"], e["status"], e["removable"], e["note"]) for e in (await view(engine))["entries"]]
    assert got == [
        (LIVE_HOST, "live", False, None),
        (LIVE_HOST, "gone", True, "old_tag"),
        ("203.0.113.77", "gone", True, "no_record"),
        (UNKNOWN_IP, "unknown", False, None),
        ("203.0.113.50", "unknown", False, None),  # the live node's own IPv4
    ]
    prepared = await prepare(engine, vault)
    assert prepared.live_tags == frozenset({LIVE_TAG})


async def test_the_inventory_then_reads_the_sponsor_of_each_live_proxy(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, *ALL_LABELS)
    prepared = await prepare(engine, vault)
    # Only the live proxy's screen is opened (never a dead or an unknown one's).
    assert prepared.plan.kind == "menu" and prepared.plan.entry is not None
    assert prepared.plan.entry.address == LIVE_HOST and prepared.plan.cost == 2
    assert await record(engine, prepared, seen_menu("ipoxxo")) == "mtproxybot:menu"
    v = await view(engine)
    live = by_address(v)[LIVE_HOST]
    assert (live["sponsor_channel"], live["promotion"]) == ("ipoxxo", "ok") and v["counts"][
        "promotion_ok"
    ] == 1
    assert v["screens"][-1]["about"] == LIVE_HOST and v["screens"][-1]["text"] == "Proxy <host:port>"
    assert await inventory.step(deps(engine, vault)) == "mtproxybot:done"
    assert (await job(engine)).state == "done"
    assert await inventory.step(deps(engine, vault)) == "idle"


@pytest.mark.parametrize(
    ("outcome", "promotion", "note"),
    [
        (seen_menu("customer_ch"), "other", "shown"),
        (seen_menu(), "not_shown", "not_shown"),
        (seen_menu("ipoxxo", "other"), "several", "several"),
        (
            WalkResult(
                result=WalkStuck(step=Step.SENT_MYPROXIES, reason="proxy_not_in_list", screens=(SCREEN,)),
                last_step=Step.SENT_MYPROXIES,
            ),
            "stuck",
            "stuck:proxy_not_in_list",
        ),
    ],
)
async def test_a_live_proxy_without_the_wanted_sponsor_is_flagged(
    engine: AsyncEngine, vault: Vault, seal_key: bytes, outcome: WalkResult, promotion: str, note: str
) -> None:
    await seed(engine, seal_key)
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, *ALL_LABELS)
    await record(engine, await prepare(engine, vault), outcome)
    v = await view(engine)
    assert by_address(v)[LIVE_HOST]["promotion"] == promotion
    async with engine.connect() as db:
        (live,) = [e for e in await inventory.load_entries(db) if e.host == LIVE_HOST]
    assert live.menu_note == note
    # A screen that could not be read does not stop the (read-only) inventory.
    assert (await job(engine)).state == "running"
    assert await inventory.step(deps(engine, vault)) == "mtproxybot:done"


# --- the cleanup: observe first ---------------------------------------------------------------------------


async def test_the_first_cleanup_only_observes_one_dead_proxy(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    now = datetime.now(UTC)
    with pytest.raises(InventoryError) as caught:  # removal needs an observation first
        await inventory.set_removal(engine, True, actor="cli", now=now)
    assert caught.value.code == "mtproxybot_not_observed"
    queued = await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=now)
    assert queued.mode == "observe"
    await read_the_list(engine, vault, *ALL_LABELS)
    prepared = await prepare(engine, vault)
    assert prepared.plan.kind == "observe" and prepared.plan.entry is not None
    assert prepared.plan.entry.address == DEAD_IP  # never the live, retired or unknown one
    assert await record(engine, prepared, seen_menu()) == "mtproxybot:observed"
    v = await view(engine)
    assert v["observation"]["result"] == "remove_button_found" and v["observation"]["address"] == DEAD_IP
    assert by_address(v)[DEAD_IP]["removal_state"] == "observed"
    assert await inventory.step(deps(engine, vault)) == "mtproxybot:done"
    state = await inventory.set_removal(engine, True, actor="cli", now=now)
    assert state.removal_enabled
    assert (await view(engine))["cleanup_mode"] == "remove"
    names = [a["action"] for a in await actions(engine)]
    assert "sponsor.mtproxybot_observed" in names and "sponsor.mtproxybot_removal_setting" in names
    assert "sponsor.bot_pressed" not in names  # nothing went to Telegram in this test


@pytest.mark.parametrize(
    ("outcome", "result"),
    [
        (seen_menu(remove_buttons=0), "no_remove_button"),
        (seen_menu(remove_buttons=2), "several_remove_buttons"),
        (seen_menu(names_proxy=False), "menu_does_not_name_proxy"),
    ],
)
async def test_removal_stays_off_after_an_observation_that_did_not_match(
    engine: AsyncEngine, vault: Vault, seal_key: bytes, outcome: WalkResult, result: str
) -> None:
    await seed(engine, seal_key)
    await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, *ALL_LABELS)
    await record(engine, await prepare(engine, vault), outcome)
    assert (await view(engine))["observation"]["result"] == result
    with pytest.raises(InventoryError):
        await inventory.set_removal(engine, True, actor="cli", now=datetime.now(UTC))


async def test_nothing_to_observe_ends_the_cleanup(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, LIVE_LABEL, UNKNOWN_LABEL, MOVED_LABEL)
    assert await inventory.step(deps(engine, vault)) == "mtproxybot:done"
    assert (await job(engine)).reason == "nothing_to_remove"


# --- the cleanup: removal ---------------------------------------------------------------------------------


async def enable_removal(engine: AsyncEngine) -> None:
    async with engine.begin() as db:
        state = await inventory._locked_state(db)
        observation = {"result": inventory.OBSERVED_OK, "address": DEAD_IP}
        await inventory.save_state(db, inventory.BotListState(removal_enabled=True, observation=observation))
        assert state.job is None


async def test_removal_removes_only_dead_proxies_then_reads_the_list_again(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await enable_removal(engine)
    queued = await inventory.request_job(
        engine, inventory.CLEANUP, actor="admin:owner", now=datetime.now(UTC)
    )
    assert queued.mode == "remove"
    await read_the_list(engine, vault, *ALL_LABELS)
    prepared = await prepare(engine, vault)
    assert prepared.plan.kind == "remove" and prepared.plan.entry is not None
    assert prepared.plan.entry.address == DEAD_IP and prepared.plan.cost == 5
    removed = Removed(confirmed=True, screens=(SCREEN,))
    assert await record(engine, prepared, WalkResult(result=removed, messages_sent=4)) == "mtproxybot:removed"
    assert (await job(engine)).removed == 1
    relist = await prepare(engine, vault)
    assert relist.plan.kind == "relist"
    await record(engine, relist, WalkResult(result=list_read(LIVE_LABEL, MOVED_LABEL, UNKNOWN_LABEL)))
    final = await job(engine)
    assert final.relisted and final.state == "running"
    assert await inventory.step(deps(engine, vault)) == "mtproxybot:done"
    v = await view(engine)
    assert set(by_address(v)) == {LIVE_HOST, MOVED_IP, UNKNOWN_IP}  # the live and unknown ones stay
    logged = {a["action"]: a for a in await actions(engine)}
    assert logged["sponsor.mtproxybot_removed"]["target"] == DEAD_IP
    reads = [a for a in await actions(engine) if a["action"] == "sponsor.mtproxybot_list_read"]
    assert len(reads) == 2


async def test_a_removal_that_meets_an_unknown_screen_stops_everything_and_alerts_once(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await pair_owner(engine)
    await enable_removal(engine)
    await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, *ALL_LABELS)
    prepared = await prepare(engine, vault)
    screens = (SCREEN, Screen(stage="after_remove", text="Sure?", buttons=(("Yes, I'm sure", "No"),)))
    stuck = WalkStuck(step=Step.PRESSED_REMOVE, reason="no_confirm_button", screens=screens)
    got = await record(engine, prepared, WalkResult(result=stuck, last_step=Step.PRESSED_REMOVE))
    assert got == "mtproxybot:stuck:no_confirm_button"
    final = await job(engine)
    assert (final.state, final.reason, final.step) == ("stuck", "no_confirm_button", "pressed_remove")
    v = await view(engine)
    dead = by_address(v)[DEAD_IP]
    assert (dead["removal_state"], dead["removal_reason"]) == ("stuck", "no_confirm_button")
    assert dead["removal_reason_fa"] and v["job"]["reason_fa"]
    assert v["screens"][-1]["buttons"] == [["Yes, I'm sure", "No"]]
    sent = await alerts(engine)
    assert len(sent) == 1 and DEAD_IP in sent[0] and "متوقف" in sent[0]
    assert await inventory.step(deps(engine, vault)) == "idle"  # nothing more, until the owner asks again


async def test_an_interrupted_removal_is_never_resumed_but_an_early_failure_is_tried_again(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await enable_removal(engine)
    await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, *ALL_LABELS)
    # Telegram failed while the dead proxy's button was being opened: nothing was taken away yet.
    early = WalkResult(error="telegram_unavailable", attempted_step=Step.PRESSED_PROXY, messages_sent=1)
    assert (
        await record(engine, await prepare(engine, vault), early) == "mtproxybot:failed:telegram_unavailable"
    )
    assert (await job(engine)).failures == 1 and (await job(engine)).state == "running"
    # A pace stop is nobody's fault: not counted.
    paced = WalkResult(error="hourly_cap", attempted_step=None)
    await record(engine, await prepare(engine, vault), paced)
    assert (await job(engine)).failures == 1
    # Telegram failed after the removal button may have reached the bot: stop, never resume.
    late = WalkResult(
        error="flood_wait",
        retry_after_s=120,
        attempted_step=Step.PRESSED_CONFIRM,
        last_step=Step.PRESSED_REMOVE,
    )
    got = await record(engine, await prepare(engine, vault), late)
    assert got == "mtproxybot:stuck:interrupted:flood_wait"
    final = await job(engine)
    assert final.state == "stuck" and final.step == "pressed_confirm"
    v = await view(engine)
    assert by_address(v)[DEAD_IP]["removal_state"] == "interrupted"
    async with engine.connect() as db:  # Telegram's wait holds every message of the account
        assert (await pacing.load(db)).hold_until is not None
    assert await inventory.step(deps(engine, vault)) == "idle"


async def test_a_request_the_operator_client_refused_stops_the_job_and_is_never_retried(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await enable_removal(engine)
    await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, *ALL_LABELS)
    refused = WalkResult(
        error="request_not_allowed", last_step=Step.PRESSED_PROXY, attempted_step=Step.PRESSED_PROXY
    )
    got = await record(engine, await prepare(engine, vault), refused)
    assert got == "mtproxybot:stuck:request_not_allowed"
    assert (await job(engine)).state == "stuck"
    assert by_address(await view(engine))[DEAD_IP]["removal_state"] == "stuck"
    assert await inventory.step(deps(engine, vault)) == "idle"
    # The same for the read-only inventory: a refusal is a stop, not a failure to try again.
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC))
    got = await record(engine, await prepare(engine, vault), WalkResult(error="request_not_allowed"))
    assert got == "mtproxybot:stuck:request_not_allowed" and (await job(engine)).state == "stuck"


async def test_three_failures_in_a_row_end_the_job(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC))
    for _ in range(inventory.MAX_FAILURES):
        await record(engine, await prepare(engine, vault), WalkResult(error="no_reply", messages_sent=1))
    final = await job(engine)
    assert (final.state, final.reason, final.failures) == ("failed", "no_reply", 3)


async def test_a_removal_that_happened_after_a_stop_is_still_recorded(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await enable_removal(engine)
    await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, *ALL_LABELS)
    prepared = await prepare(engine, vault)
    await inventory.stop_job(engine, actor="admin:owner", now=datetime.now(UTC))
    await record(engine, prepared, WalkResult(result=Removed(confirmed=True, screens=())))
    final = await job(engine)
    assert (final.state, final.reason, final.removed) == ("stopped", "owner", 0)
    assert by_address(await view(engine))[DEAD_IP]["removal_state"] == "removed"


async def test_removal_waits_while_paused_and_needs_a_complete_list(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await enable_removal(engine)
    await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=datetime.now(UTC))
    await read_the_list(engine, vault, *ALL_LABELS)
    assert await inventory._prepare(deps(engine, vault), datetime.now(UTC), True) == "paused"
    async with engine.begin() as db:  # the list it read was not complete
        state = await inventory._locked_state(db)
        assert state.last_list is not None
        incomplete = inventory.ListInfo(
            read_at=state.last_list.read_at, pages=8, complete=False, note="too_many_pages", total=4
        )
        await inventory.save_state(
            db, inventory.BotListState(removal_enabled=True, job=state.job, last_list=incomplete)
        )
    assert await inventory.step(deps(engine, vault)) == "mtproxybot:stopped"
    assert (await job(engine)).reason == "list_incomplete"


# --- one job at a time, and only when the account may talk --------------------------------------------------


async def test_jobs_need_the_account_the_kill_switch_off_and_come_one_at_a_time(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    now = datetime.now(UTC)
    await kill_switch(engine, True)
    with pytest.raises(InventoryError) as caught:
        await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=now)
    assert caught.value.code == "mtproxybot_kill_switch"
    await kill_switch(engine, False)
    with pytest.raises(InventoryError) as caught:
        await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=now)
    assert caught.value.code == "mtproxybot_no_account"
    await connect_account(engine, bot_id=BOT_ID)
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=now)
    with pytest.raises(InventoryError) as caught:
        await inventory.request_job(engine, inventory.CLEANUP, actor="admin:owner", now=now)
    assert caught.value.code == "mtproxybot_job_running"
    await inventory.stop_job(engine, actor="admin:owner", now=now)
    with pytest.raises(InventoryError) as caught:
        await inventory.stop_job(engine, actor="admin:owner", now=now)
    assert caught.value.code == "mtproxybot_no_job"
    with pytest.raises(InventoryError):
        await inventory.request_job(engine, "delete_everything", actor="admin:owner", now=now)


async def test_a_step_waits_for_the_kill_switch_the_account_and_the_budget(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    d = deps(engine, vault)
    assert await inventory.step(d) == "idle"
    await seed(engine, seal_key)
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC))
    await kill_switch(engine, True)
    assert await inventory.step(d) == "kill_switch"
    await kill_switch(engine, False)
    await connect_account(engine, bot_id=None)
    assert await inventory.step(d) == "no_account"
    await connect_account(engine, bot_id=BOT_ID)
    now = datetime.now(UTC)
    async with engine.begin() as db:  # the hour holds less than the step plus the reserve
        used = pacing.HOURLY_CAP - inventory.RESERVE - 1
        await pacing.save(db, pacing.Pace(sent=tuple(now - timedelta(minutes=30) for _ in range(used))))
    assert await inventory.step(d) == "paced"
    assert (await job(engine)).state == "queued"


async def test_a_real_step_with_telegram_unreachable_sends_nothing(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    await seed(engine, seal_key)
    await seed_connected(engine, vault, port=closed_port(), mtproxybot_id=BOT_ID)
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC))
    assert await inventory.step(deps(engine, vault)) == "mtproxybot:failed:telegram_unavailable"
    current = await job(engine)
    assert (current.state, current.failures, current.last_error) == ("running", 1, "telegram_unavailable")
    names = [a["action"] for a in await actions(engine)]
    assert "sponsor.bot_sent" not in names and "sponsor.mtproxybot_failed" in names
    async with engine.connect() as db:
        assert (await pacing.load(db)).sent == ()


async def test_the_sponsor_loop_runs_a_step_only_when_it_has_nothing_else_to_do(
    engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    from test_sponsor_support import TelemtUsersProvider

    from proxyhub.reconcilers.sponsor import SponsorReconciler

    await seed(engine, seal_key)
    await seed_connected(engine, vault, port=closed_port(), mtproxybot_id=BOT_ID)
    loop = SponsorReconciler(
        vault_factory=lambda: vault,
        provider=lambda: TelemtUsersProvider(seal_key),
        options=ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=3),
    )
    assert await loop.step(engine) == "idle"  # no job: the pass stays idle
    await inventory.request_job(engine, inventory.INVENTORY, actor="admin:owner", now=datetime.now(UTC))
    assert await loop.step(engine) == "mtproxybot:failed:telegram_unavailable"
    await kill_switch(engine, True)
    assert await loop.step(engine) == "kill_switch"


# --- the API and the CLI ----------------------------------------------------------------------------------


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


async def test_the_owner_starts_and_stops_jobs_over_the_api(
    client: AsyncClient, engine: AsyncEngine, vault: Vault, seal_key: bytes
) -> None:
    from proxyhub.api import routes_sponsor

    for route in routes_sponsor.router.routes:
        assert isinstance(route, APIRoute)
        if "mtproxybot" in route.path:
            assert require_admin in {d.call for d in route.dependant.dependencies}
    r = await client.post("/api/sponsor/mtproxybot/inventory")
    assert r.status_code == 409 and r.json()["error"] == "mtproxybot_kill_switch" and r.json()["message_fa"]
    await seed(engine, seal_key)
    r = await client.post("/api/sponsor/mtproxybot/cleanup")
    assert r.status_code == 200, r.text
    body = r.json()["mtproxybot"]
    assert (
        body["job"]["kind"] == "cleanup" and body["job"]["mode"] == "observe" and not body["removal_enabled"]
    )
    r = await client.post("/api/sponsor/mtproxybot/inventory")
    assert r.status_code == 409 and r.json()["error"] == "mtproxybot_job_running"
    await read_the_list(engine, vault, *ALL_LABELS)
    got = (await client.get("/api/sponsor")).json()["mtproxybot"]
    assert {e["status"] for e in got["entries"]} == {"live", "gone", "retired", "unknown"}
    text_ = json.dumps(got)
    for leaked in (LIVE_TAG, DEAD_TAG, "aa11bb22"):
        assert leaked not in text_
    r = await client.post("/api/sponsor/mtproxybot/stop")
    assert r.status_code == 200 and r.json()["mtproxybot"]["job"]["state"] == "stopped"
    r = await client.post("/api/sponsor/mtproxybot/stop")
    assert r.status_code == 409 and r.json()["error"] == "mtproxybot_no_job"


async def test_the_cli_reports_and_refuses_removal_without_an_observation(
    engine: AsyncEngine, vault: Vault, seal_key: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    from proxyhub.sponsor.__main__ import _run

    await seed(engine, seal_key)
    assert await _run(argparse.Namespace(cmd="removal", value="on")) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"] == "mtproxybot_not_observed" and refused["message_fa"]
    assert await _run(argparse.Namespace(cmd="cleanup")) == 0
    assert json.loads(capsys.readouterr().out)["job"]["mode"] == "observe"
    await read_the_list(engine, vault, *ALL_LABELS)
    assert await _run(argparse.Namespace(cmd="status")) == 0
    report = json.loads(capsys.readouterr().out)
    assert {e["address"]: e["status"] for e in report["entries"]}[DEAD_IP] == "gone"
    assert LIVE_TAG not in json.dumps(report)
    assert await _run(argparse.Namespace(cmd="stop")) == 0
    capsys.readouterr()
    assert await _run(argparse.Namespace(cmd="wait", timeout=1.0)) == 0
    assert json.loads(capsys.readouterr().out)["job"]["state"] == "stopped"
    async with engine.connect() as db:
        rows = (await db.execute(text("SELECT count(*) FROM mtproxybot_entries"))).scalar_one()
    assert rows == 4


# --- the walk runner: the slot first, and what it tried ------------------------------------------------


class SilentThenClosed:
    """Nobody answers (no reply is made up, decision D10), and a message goes out through a REAL
    operator client whose network is closed: Telegram's own refusal."""

    def __init__(self, real: BotChat) -> None:
        self.real = real

    async def recent(self, limit: int = 10) -> list[BotMessage]:
        return []

    async def send(self, text: str, *, sensitive: bool = False) -> int | None:
        return await self.real.send(text, sensitive=sensitive)

    async def press(self, message_id: int, data: bytes) -> str | None:
        return await self.real.press(message_id, data)


async def _no_wait(seconds: float) -> None:
    return None


async def test_a_walk_takes_a_slot_logs_and_sends_and_says_what_it_tried(engine: AsyncEngine) -> None:
    await kill_switch(engine, False)
    events: list[str] = []

    async def slot() -> object:
        taken = await pacing.take_slot(engine, clock=lambda: datetime.now(UTC))
        events.append("slot")
        return taken

    got = await driver.walk(
        SilentChat(events, allow_send=True),
        ListWalk(),
        take_slot=slot,
        record=Lines(events),
        timing=Timing(poll_s=1, settle_s=1, reply_timeout_s=3),
        max_actions=4,
        sleep=_no_wait,
    )
    assert (got.error, got.messages_sent, got.last_step, got.attempted_step) == (
        "no_reply",
        1,
        Step.SENT_MYPROXIES,
        Step.SENT_MYPROXIES,
    )
    assert events == ["read", "slot", "line:/myproxies", "send:sensitive=False", "read", "read", "read"]
    await kill_switch(engine, True)
    events.clear()
    stopped = await driver.walk(
        SilentChat(events), ListWalk(), take_slot=slot, record=Lines(events), timing=Timing(), max_actions=4
    )
    assert (stopped.error, stopped.attempted_step, stopped.messages_sent) == ("kill_switch", None, 0)
    assert events == ["read"]


async def test_a_message_telegram_refused_still_counts_as_tried(engine: AsyncEngine) -> None:
    await kill_switch(engine, False)
    creds = ApiCredentials(api_id=123456, api_hash=SecretValue(secrets.token_hex(16)))
    options = ClientOptions(endpoint=Endpoint("127.0.0.1", closed_port()), deadline_s=2)
    lines: list[str] = []

    async def slot() -> object:
        return await pacing.take_slot(engine, clock=lambda: datetime.now(UTC))

    with contextlib.ExitStack() as hold:
        op = OperatorSession(userclient._new_client(creds, None, options), creds, options, hold)
        op._client._mtproxybot_id = BOT_ID
        chat = SilentThenClosed(BotChat(op, types.InputPeerUser(user_id=BOT_ID, access_hash=1)))
        got = await driver.walk(
            chat, ListWalk(), take_slot=slot, record=Lines(lines), timing=Timing(), max_actions=4
        )
    # The slot was taken and the line written: the message may have reached the bot.
    assert (got.error, got.last_step, got.attempted_step, got.messages_sent) == (
        "telegram_unavailable",
        None,
        Step.SENT_MYPROXIES,
        0,
    )
    assert lines == ["line:/myproxies"]
