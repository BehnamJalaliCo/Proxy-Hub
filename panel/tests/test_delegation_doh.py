"""Lost domain registration / delegation (proxyhub/cloudflare/delegation.py).

Parser data: answers recorded from the REAL DoH JSON API of Cloudflare's resolver
(``https://cloudflare-dns.com/dns-query?name=<name>&type=NS``, ``accept: application/dns-json``) on
2026-11-04, byte for byte. The failure path talks to a local port nothing listens on. The pool
decisions run on the real database with the real rotation code. A real lookup is opt-in
(``PROXYHUB_GLOBALPING_TESTS=1``, like the other real public-API calls).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_live_support import closed_port
from test_net_support import clean_net_tables

from proxyhub.cloudflare import delegation as dg
from proxyhub.cloudflare.delegation import (
    DelegationRound,
    DohClient,
    NsAnswer,
    classify,
    delegation_restored,
    delegation_verdict,
    parse_doh,
)
from proxyhub.telegram import store as tg_store

# --- recorded real answers (2026-11-04) ----------------------------------------------------------

DISCORD = (
    '{"Status":0,"TC":false,"RD":true,"RA":true,"AD":true,"CD":false,"Question":[{"name":"discord.com",'
    '"type":2}],"Answer":[{"name":"discord.com","type":2,"TTL":85973,"data":"gabe.ns.cloudflare.com."},'
    '{"name":"discord.com","type":2,"TTL":85973,"data":"sima.ns.cloudflare.com."}]}'
)
EXAMPLE_COM = (
    '{"Status":0,"TC":false,"RD":true,"RA":true,"AD":true,"CD":false,"Question":[{"name":"example.com",'
    '"type":2}],"Answer":[{"name":"example.com","type":2,"TTL":76214,"data":"hera.ns.cloudflare.com."},'
    '{"name":"example.com","type":2,"TTL":76214,"data":"elliott.ns.cloudflare.com."}]}'
)
GOOGLE = (
    '{"Status":0,"TC":false,"RD":true,"RA":true,"AD":false,"CD":false,"Question":[{"name":"google.com",'
    '"type":2}],"Answer":[{"name":"google.com","type":2,"TTL":341616,"data":"ns4.google.com."},'
    '{"name":"google.com","type":2,"TTL":341616,"data":"ns1.google.com."},{"name":"google.com","type":2,'
    '"TTL":341616,"data":"ns2.google.com."},{"name":"google.com","type":2,"TTL":341616,'
    '"data":"ns3.google.com."}]}'
)
# Cloudflare's OWN zone uses its enterprise name servers, not the <name>.ns.cloudflare.com pair every
# zone of a normal account gets: for our pool this is "not delegated to our Cloudflare account".
CLOUDFLARE_COM = (
    '{"Status":0,"TC":false,"RD":true,"RA":true,"AD":true,"CD":false,"Question":[{"name":"cloudflare.com",'
    '"type":2}],"Answer":[{"name":"cloudflare.com","type":2,"TTL":86400,"data":"ns3.cloudflare.com."},'
    '{"name":"cloudflare.com","type":2,"TTL":86400,"data":"ns4.cloudflare.com."},{"name":"cloudflare.com",'
    '"type":2,"TTL":86400,"data":"ns5.cloudflare.com."},{"name":"cloudflare.com","type":2,"TTL":86400,'
    '"data":"ns6.cloudflare.com."},{"name":"cloudflare.com","type":2,"TTL":86400,"data":"ns7.cloudflare.com."}]}'
)
NXDOMAIN = (
    '{"Status":3,"TC":false,"RD":true,"RA":true,"AD":false,"CD":false,"Question":[{"name":'
    '"nonexistent-proxyhub-zz9q.com","type":2}],"Authority":[{"name":"com","type":6,"TTL":900,"data":'
    '"a.gtld-servers.net. nstld.verisign-grs.com. 1793821951 1800 900 604800 900"}]}'
)
NO_ANSWER = (
    '{"Status":0,"TC":false,"RD":true,"RA":true,"AD":true,"CD":false,"Question":[{"name":"www.example.com",'
    '"type":2}],"Authority":[{"name":"example.com","type":6,"TTL":1800,"data":"elliott.ns.cloudflare.com. '
    'dns.cloudflare.com. 2418004112 10000 2400 604800 1800"}]}'
)
SERVFAIL = (
    '{"Status":2,"TC":false,"RD":true,"RA":true,"AD":false,"CD":false,"Question":[{"name":"dnssec-failed.org",'
    '"type":2}],"Comment":["EDE(9): DNSKEY Missing no SEP matching the DS found for dnssec-failed.org."]}'
)
BAD_NAME_400 = '{"error":"Invalid query name `..bad`."}'  # HTTP 400 for a malformed name

RECORDED = [
    ("discord.com", DISCORD, "ok", ("gabe.ns.cloudflare.com", "sima.ns.cloudflare.com")),
    ("example.com", EXAMPLE_COM, "ok", ("elliott.ns.cloudflare.com", "hera.ns.cloudflare.com")),
    ("google.com", GOOGLE, "not_cloudflare", tuple(f"ns{i}.google.com" for i in (1, 2, 3, 4))),
    ("cloudflare.com", CLOUDFLARE_COM, "not_cloudflare", tuple(f"ns{i}.cloudflare.com" for i in range(3, 8))),
    ("nonexistent-proxyhub-zz9q.com", NXDOMAIN, "nxdomain", ()),
    ("www.example.com", NO_ANSWER, "no_answer", ()),
    ("dnssec-failed.org", SERVFAIL, "servfail", ()),
]


@pytest.mark.parametrize(("name", "raw", "result", "ns"), RECORDED, ids=[r[0] for r in RECORDED])
def test_recorded_real_answers_parse_and_classify(
    name: str, raw: str, result: str, ns: tuple[str, ...]
) -> None:
    answer = parse_doh(json.loads(raw), name)
    assert answer.nameservers == ns and answer.error is None
    assert classify(answer, None) == result


def test_a_different_cloudflare_pair_means_another_account() -> None:
    discord = parse_doh(json.loads(DISCORD), "discord.com")
    assert classify(discord, ["gabe.ns.cloudflare.com", "sima.ns.cloudflare.com"]) == "ok"
    assert classify(discord, ["sima.ns.cloudflare.com", "gabe.ns.cloudflare.com"]) == "ok"  # order free
    assert classify(discord, ["hera.ns.cloudflare.com", "elliott.ns.cloudflare.com"]) == "ns_changed"


def test_unusable_answers_are_errors_never_lost() -> None:
    assert parse_doh(json.loads(BAD_NAME_400), "x.com").error == "invalid_response"
    for raw in (None, [], "text", {"Status": True}, {"Status": "0"}):
        assert classify(parse_doh(raw, "x.com"), None) == "error"
    # NS records of another name, and garbage host names, are ignored.
    mixed = {
        "Status": 0,
        "Answer": [
            {"name": "other.com", "type": 2, "data": "a.ns.cloudflare.com."},
            {"name": "x.com", "type": 2, "data": "not a host"},
            {"name": "x.com", "type": 5, "data": "cname.example."},
        ],
    }
    assert classify(parse_doh(mixed, "x.com"), None) == "no_answer"
    assert classify(NsAnswer(5), None) == "error"  # REFUSED and the like decide nothing


# --- verdict ------------------------------------------------------------------------------------------

T = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def r(minutes: float, result: str) -> DelegationRound:
    return DelegationRound(T + timedelta(minutes=minutes), result)


def test_lost_needs_two_lost_lookups_in_a_row_30_minutes_apart() -> None:
    now = T + timedelta(minutes=60)
    assert delegation_verdict([], now)[0] == "unknown"
    assert delegation_verdict([r(0, "nxdomain")], now) == ("unknown", ["single_lookup", "nxdomain"])
    assert delegation_verdict([r(0, "nxdomain"), r(20, "nxdomain")], now)[1][0] == "lookups_too_close"
    assert delegation_verdict([r(0, "nxdomain"), r(31, "not_cloudflare")], now) == (
        "lost",
        ["not_cloudflare", "nxdomain"],
    )
    # Inconclusive lookups in between are skipped, never counted either way.
    rounds = [r(0, "nxdomain"), r(10, "servfail"), r(20, "error"), r(35, "ns_changed"), r(40, "no_answer")]
    assert delegation_verdict(rounds, now)[0] == "lost"
    # One good lookup ends it.
    assert delegation_verdict([r(0, "nxdomain"), r(31, "ok")], now) == ("ok", ["delegated_to_cloudflare"])
    assert delegation_verdict([r(0, "ok"), r(31, "nxdomain")], now)[1][0] == "not_confirmed"
    # Old lookups decide nothing.
    later = T + timedelta(hours=7)
    assert delegation_verdict([r(0, "nxdomain"), r(31, "nxdomain")], later)[1] == ["lookups_too_old"]
    assert delegation_verdict([r(0, "nxdomain"), r(400, "nxdomain")], later)[1][0] == "lookups_too_old"


def test_restored_needs_two_good_lookups_30_minutes_apart() -> None:
    now = T + timedelta(minutes=60)
    assert not delegation_restored([r(0, "ok")], now)
    assert not delegation_restored([r(0, "ok"), r(10, "ok")], now)
    assert not delegation_restored([r(0, "nxdomain"), r(31, "ok")], now)
    assert delegation_restored([r(0, "ok"), r(15, "servfail"), r(31, "ok")], now)
    assert not delegation_restored([r(0, "ok"), r(31, "ok")], T + timedelta(hours=7))


def test_lookup_schedule() -> None:
    assert dg.check_due(None, None, T)
    assert not dg.check_due(T, "ok", T + timedelta(minutes=59))
    assert dg.check_due(T, "ok", T + timedelta(hours=1))
    assert dg.check_due(T, "nxdomain", T + timedelta(minutes=30))
    assert dg.check_due(T, "error", T + timedelta(minutes=30))


async def test_an_unreachable_resolver_is_an_error_not_a_verdict() -> None:
    async with DohClient(f"http://127.0.0.1:{closed_port()}/dns-query") as doh:
        answer = await doh.ns("example.com")
        assert answer.error == "unreachable" and classify(answer, None) == "error"
        assert (await doh.ns("not a name")).error == "invalid_name"  # never sent


def test_alert_text_is_persian_and_escaped() -> None:
    text_ = dg.lost_alert_fa("a<b>.com", "nxdomain", True, "spare.com", 0)
    assert "a&lt;b&gt;.com" in text_ and "<b>spare.com</b>" in text_ and "NXDOMAIN" in text_
    assert "از Cloudflare پاک نمیشود" in text_ and "دامنه‌ی ذخیره‌ای نمانده" in text_
    assert "IP سرورها" in dg.lost_alert_fa("a.com", "not_cloudflare", True, None, 0)
    assert "دامنه‌ی ذخیره‌ی" in dg.lost_alert_fa("a.com", "ns_changed", False, None, 2)


# --- pool decisions on the real database ---------------------------------------------------------------

ZA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
ZB = "b1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
ZC = "c1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
CF_PAIR = ("gabe.ns.cloudflare.com", "sima.ns.cloudflare.com")
OK = NsAnswer(0, CF_PAIR)
GONE = NsAnswer(3)
MOVED = NsAnswer(0, ("ns1.parking.example", "ns2.parking.example"))


async def _clean(engine: AsyncEngine) -> None:
    await clean_net_tables(engine)  # delegation rows cascade with their zones
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM outbox"))
        await conn.execute(text("DELETE FROM settings WHERE key = 'telegram.owner'"))


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await _clean(eng)
    yield eng
    await _clean(eng)
    await eng.dispose()


async def add_zone(engine: AsyncEngine, zone_id: str, name: str, pool: str, *, created: datetime) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO cf_zones (id, name, status, pool_state, dns_edit, seen_at, created_at, "
                "pool_changed_at) VALUES (:id, :name, 'active', :pool, true, now(), :c, :c)"
            ),
            {"id": zone_id, "name": name, "pool": pool, "c": created},
        )


async def pools(engine: AsyncEngine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT name, pool_state FROM cf_zones"))).all()
    return {r.name: r.pool_state for r in rows}


async def alerts(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT payload FROM outbox WHERE kind = 'alert' ORDER BY id"))).all()
    return [str(row.payload["text"]) for row in rows]


async def look(engine: AsyncEngine, zone_id: str, answer: NsAnswer, at: datetime) -> dg.Outcome:
    async with engine.begin() as conn:
        out = await dg.record_lookup(conn, zone_id, answer, at, at)
    assert out is not None
    return out


async def world(engine: AsyncEngine) -> datetime:
    """Active a.com, standby b.com (both in the pool for a day), the owner's chat paired."""
    start = datetime.now(UTC) - timedelta(hours=3)
    await add_zone(engine, ZA, "a.com", "active", created=start - timedelta(days=1))
    await add_zone(engine, ZB, "b.com", "standby", created=start - timedelta(hours=23))
    async with engine.begin() as conn:
        await tg_store.set_owner_chat(conn, 424242, datetime.now(UTC))
    return start


@pytest.mark.db
async def test_a_lost_active_domain_is_replaced_and_comes_back_when_renewed(engine: AsyncEngine) -> None:
    t = await world(engine)
    assert (await look(engine, ZA, OK, t)).verdict == "ok"  # remembers its Cloudflare pair
    single = await look(engine, ZA, GONE, t + timedelta(minutes=60))
    assert (single.verdict, single.taken_out) == ("unknown", False)
    assert await pools(engine) == {"a.com": "active", "b.com": "standby"}
    lost = await look(engine, ZA, GONE, t + timedelta(minutes=91))
    assert (lost.verdict, lost.taken_out, lost.replacement) == ("lost", True, "b.com")
    assert await pools(engine) == {"a.com": "none", "b.com": "active"}
    (alert,) = await alerts(engine)
    assert "<b>a.com</b>" in alert and "<b>b.com</b> جایش فعال شد" in alert and "NXDOMAIN" in alert
    assert "دامنه‌ی ذخیره‌ای نمانده" in alert  # b.com was the last standby
    async with engine.connect() as conn:
        log = (await conn.execute(text("SELECT action, target, reasons FROM actions_log ORDER BY id"))).all()
        standby_alert = (await conn.execute(text("SELECT standby_alert_at FROM cf_state"))).scalar_one()
    moves = [(a.target, a.reasons["reasons"]) for a in log if a.action == "cloudflare_pool_change"]
    assert moves == [("a.com", ["delegation_lost"]), ("b.com", ["replaces_delegation_lost"])]
    assert [a.reasons["reasons"][0] for a in log if a.action == "domain_delegation"] == [
        "ok",
        "unknown",
        "lost",
    ]
    assert standby_alert is not None
    # It is never deleted from Cloudflare for this: it is not "filtered".
    assert (await pools(engine))["a.com"] != "filtered"

    # Renewed: two good lookups 30 minutes apart put it back as a standby, by itself.
    t2 = t + timedelta(minutes=121)
    assert not (await look(engine, ZA, OK, t2)).restored
    back = await look(engine, ZA, OK, t2 + timedelta(minutes=31))
    assert back.restored and await pools(engine) == {"a.com": "standby", "b.com": "active"}
    assert "دوباره روی Cloudflare است" in (await alerts(engine))[-1]


@pytest.mark.db
async def test_a_zone_the_owner_moved_is_never_brought_back(engine: AsyncEngine) -> None:
    t = await world(engine)
    await look(engine, ZA, GONE, t)
    await look(engine, ZA, GONE, t + timedelta(minutes=31))
    assert (await pools(engine))["a.com"] == "none"
    async with engine.begin() as conn:  # the owner retires it in the dashboard
        await conn.execute(
            text("UPDATE cf_zones SET pool_state = 'retired', pool_changed_at = now() WHERE id = :z"),
            {"z": ZA},
        )
    await look(engine, ZA, OK, t + timedelta(minutes=70))
    out = await look(engine, ZA, OK, t + timedelta(minutes=101))
    assert not out.restored and (await pools(engine))["a.com"] == "retired"


@pytest.mark.db
async def test_a_lost_standby_leaves_the_pool_and_a_moved_one_counts_as_lost(engine: AsyncEngine) -> None:
    t = await world(engine)
    await add_zone(engine, ZC, "c.com", "standby", created=t - timedelta(hours=22))
    await look(engine, ZB, OK, t)
    await look(
        engine,
        ZB,
        NsAnswer(0, ("hera.ns.cloudflare.com", "elliott.ns.cloudflare.com")),
        t + timedelta(minutes=40),
    )
    out = await look(engine, ZB, MOVED, t + timedelta(minutes=80))
    assert out.taken_out and out.replacement is None
    assert await pools(engine) == {"a.com": "active", "b.com": "none", "c.com": "standby"}
    (alert,) = await alerts(engine)
    assert "دامنه‌ی ذخیره‌ی <b>b.com</b>" in alert and "دامنه‌ی ذخیره‌ای نمانده" not in alert


@pytest.mark.db
async def test_a_standby_already_lost_is_never_promoted(engine: AsyncEngine) -> None:
    t = await world(engine)
    await add_zone(engine, ZC, "c.com", "standby", created=t - timedelta(hours=22))
    await look(engine, ZB, GONE, t)  # b.com: one lost lookup, the next one is not due yet
    await look(engine, ZA, GONE, t + timedelta(minutes=1))
    async with engine.begin() as conn:  # b.com's latest verdict is "lost" already
        await conn.execute(
            text("UPDATE domain_delegation_checks SET verdict = 'lost' WHERE zone_id = :z"), {"z": ZB}
        )
    out = await look(engine, ZA, GONE, t + timedelta(minutes=32))
    assert out.replacement == "c.com"
    assert await pools(engine) == {"a.com": "none", "b.com": "standby", "c.com": "active"}


@pytest.mark.db
async def test_the_loop_stores_an_unreachable_resolver_as_an_error_and_moves_nothing(
    engine: AsyncEngine,
) -> None:
    await world(engine)
    created: list[DohClient] = []

    def factory() -> DohClient:
        client = DohClient(f"http://127.0.0.1:{closed_port()}/dns-query")
        created.append(client)
        return client

    rec = dg.DelegationReconciler(client_factory=factory)
    assert await rec.try_run_once(engine)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT zone_id, result, verdict, rcode FROM domain_delegation_checks"))
        ).all()
    assert sorted((r.zone_id, r.result, r.verdict, r.rcode) for r in rows) == [
        (ZA, "error", "unknown", None),
        (ZB, "error", "unknown", None),
    ]
    assert await pools(engine) == {"a.com": "active", "b.com": "standby"}
    assert await rec.try_run_once(engine)  # nothing is due a minute later: no client at all
    assert len(created) == 1
    assert await alerts(engine) == []


@pytest.mark.db
async def test_the_loop_does_nothing_without_pool_domains(engine: AsyncEngine) -> None:
    await add_zone(engine, ZA, "a.com", "none", created=datetime.now(UTC))
    calls: list[Any] = []
    rec = dg.DelegationReconciler(client_factory=lambda: calls.append(1) or DohClient())  # type: ignore[func-returns-value]
    assert await rec.try_run_once(engine)
    assert calls == []


def test_worker_runs_the_delegation_loop() -> None:
    from proxyhub.worker import reconcilers

    loops = {r.name: r for r in reconcilers()}
    assert (
        isinstance(loops["delegation"], dg.DelegationReconciler)
        and loops["delegation"].interval_seconds == 60
    )


@pytest.mark.globalping
async def test_real_lookups_at_cloudflares_resolver() -> None:
    async with DohClient() as doh:
        good = await doh.ns("discord.com")
        gone = await doh.ns("nonexistent-proxyhub-zz9q.com")
        other = await doh.ns("google.com")
    print("\nREAL DoH:", good, gone, other)
    assert classify(good, None) == "ok" and classify(gone, None) == "nxdomain"
    assert classify(other, None) == "not_cloudflare"
