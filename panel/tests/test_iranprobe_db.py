"""P15 on real PostgreSQL: the TLS cycle's budget, the location scores the fleet plans with, and
the daily "every location closed" alert. Globalping answers are the real recorded shapes in
tests/test_net_support.py (D10); the loops are pointed at a closed local port where a call would
leave the sandbox.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_net_probes import real_cycle, target_of
from test_net_support import (
    clean_net_tables,
    make_node,
)

from proxyhub.fleet.alerts import Finding, raise_alerts
from proxyhub.fleet.locations import load_location_reach, locations_summary
from proxyhub.fleet.settings_store import save_fleet_settings
from proxyhub.fleet.snapshot import load_snapshot
from proxyhub.probes import service
from proxyhub.probes.globalping import GlobalpingClient, RateLimit
from proxyhub.probes.settings import ProbeSettings
from proxyhub.reconcilers.probes import ProbesReconciler
from proxyhub.settings_schema import FleetSettings
from proxyhub.telegram.store import set_owner_chat

pytestmark = [pytest.mark.db]

KEYS = ["asn:197207", "asn:57218"]
KEY_ASNS = [197207, 57218]
CLOSED = "http://127.0.0.1:9"


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean(eng)
    yield eng
    await clean(eng)
    await eng.dispose()


async def clean(eng: AsyncEngine) -> None:
    await clean_net_tables(eng)
    async with eng.begin() as conn:
        await conn.execute(
            text("DELETE FROM settings WHERE key IN ('reality', 'mtproto', 'fleet', 'telegram.owner')")
        )
        await conn.execute(text("DELETE FROM fleet_alerts"))
        await conn.execute(text("DELETE FROM outbox"))


def closed_gp() -> GlobalpingClient:
    return GlobalpingClient(base_url=CLOSED, poll_seconds=0.01, max_wait_seconds=0.1)


# --- the TLS cycle's budget -------------------------------------------------------------------------


async def test_a_cycle_takes_only_the_nodes_the_allowance_covers(engine: AsyncEngine) -> None:
    ids = [await make_node(engine, state="ready", ipv4=f"203.0.113.{50 + i}") for i in range(3)]
    async with engine.connect() as conn:
        targets = await service.due_targets(conn, ProbeSettings(), datetime.now(UTC), None)
    assert {t.node_id for t in targets} == set(ids) and all(t.tls_ports == (443,) for t in targets)
    window = datetime.now(UTC) + timedelta(minutes=30)
    s = ProbeSettings().model_copy(update={"checkhost": False})
    # 8 probes at most: the control and one node need 16; 15 left covers nothing, so nothing is called.
    none = await service.run_cycle(targets, s, gp_factory=closed_gp, gp_rate=RateLimit(250, 15, window))
    assert none is None
    # With 3 probes measured last time: control 3 + 3 per node; 9 left covers two nodes.
    result = await service.run_cycle(
        targets, s, gp_factory=closed_gp, gp_rate=RateLimit(250, 9, window), tls_probes=3
    )
    assert result is not None and [t.node_id for t in result.measured] == [t.node_id for t in targets[:2]]
    assert result.tls_control is not None and result.tls_control.status == "error"
    tls = [m for m in result.nodes if m.kind == "https"]
    assert len(tls) == 2 and {m.status for m in tls} == {"error"}  # measured on the fallback locations
    async with engine.begin() as conn:
        verdicts = await service.store_cycle(conn, result, result.measured, s)
    assert set(verdicts) == {t.node_id for t in targets[:2]} and all(
        v[0] == "unknown" for v in verdicts.values()
    )
    async with engine.connect() as conn:
        due = await service.due_targets(conn, s, datetime.now(UTC), None)
    assert [t.node_id for t in due] == [targets[2].node_id]  # the third stays due


async def test_the_loop_waits_for_the_allowance(engine: AsyncEngine) -> None:
    await make_node(engine, state="ready", ipv4="203.0.113.60")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO probe_sources (name, ok, note, checked_at, ratelimit_limit, "
                "ratelimit_remaining, ratelimit_reset_at, tls_probes) "
                "VALUES ('globalping', true, 'x', now(), 250, 5, "
                "now() + interval '20 minutes', 3)"
            )
        )
    rec = ProbesReconciler(gp_factory=closed_gp)
    async with engine.begin() as conn:
        await rec.run_once(conn)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM probe_cycles"))).scalar_one() == 0


# --- location scores ------------------------------------------------------------------------------------


async def set_location(engine: AsyncEngine, node_id: uuid.UUID, location: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE nodes SET location = :l WHERE id = :n"), {"l": location, "n": node_id}
        )


async def test_rounds_score_the_locations_the_fleet_plans_with(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    fsn = await make_node(engine, state="ready", ipv4="203.0.113.70")
    hel = await make_node(engine, state="ready", ipv4="203.0.113.71")
    await set_location(engine, hel, "hel1")
    for node_id, ip, location, up in (
        (fsn, "203.0.113.70", "fsn1", False),
        (hel, "203.0.113.71", "hel1", True),
    ):
        t = dataclasses.replace(target_of(node_id, ip), location=location)
        for minutes in (20, 10):
            async with engine.begin() as conn:
                await service.store_cycle(
                    conn,
                    real_cycle(node_id, ip, at=now - timedelta(minutes=minutes), node_up=up),
                    [t],
                    ProbeSettings(),
                )
    async with engine.begin() as conn:
        await save_fleet_settings(conn, FleetSettings(locations=["fsn1", "nbg1", "hel1"]))
    async with engine.connect() as conn:
        reach = await load_location_reach(conn, now)
        snap = await load_snapshot(conn, now)
        table = await locations_summary(conn, now)
    assert reach["fsn1"].classify(KEYS) == "bad" and reach["hel1"].classify(KEYS) == "good"
    assert dict(snap.location_classes) == {"fsn1": "bad", "nbg1": "unknown", "hel1": "good"}
    assert [(x["location"], x["class"]) for x in table["locations"]] == [
        ("fsn1", "bad"),
        ("nbg1", "unknown"),
        ("hel1", "good"),
    ]
    fsn_nets = {n["key"]: n for n in table["locations"][0]["networks"]}
    assert fsn_nets["asn:197207"]["state"] == "closed" and fsn_nets["asn:197207"]["nodes_closed"] == 1
    assert fsn_nets["asn:57218"]["key_network"] is True
    # Older than the window: forgotten.
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE probe_location_reach SET at = now() - interval '8 days'"))
    async with engine.connect() as conn:
        assert await load_location_reach(conn, now) == {}
        assert dict((await load_snapshot(conn, now)).location_classes) == {
            "fsn1": "unknown",
            "nbg1": "unknown",
            "hel1": "unknown",
        }


async def test_every_location_closed_is_told_once_a_day(engine: AsyncEngine) -> None:
    now = datetime.now(UTC)
    found = [Finding("all_locations_closed", "⚠️ ناوگان: همه‌ی locationها بسته‌اند.", {"slots": [1]})]
    async with engine.begin() as conn:
        await set_owner_chat(conn, 424242, now)
    sent: list[list[str]] = []
    for at in (
        now,
        now + timedelta(minutes=10),
        now + timedelta(hours=13),
        now + timedelta(hours=25, minutes=10),
    ):
        async with engine.begin() as conn:
            sent.append(await raise_alerts(conn, found, at))
            if at == now:  # it has lasted long enough
                await conn.execute(text("UPDATE fleet_alerts SET since = since - interval '6 minutes'"))
    assert sent == [[], ["all_locations_closed"], [], ["all_locations_closed"]]
    async with engine.connect() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM outbox WHERE kind = 'alert'"))
        ).scalar_one() == 2
    # Cleared and back within the day: still only once a day.
    async with engine.begin() as conn:
        assert await raise_alerts(conn, [], now + timedelta(hours=26)) == []
        assert await raise_alerts(conn, found, now + timedelta(hours=27)) == []
        assert await raise_alerts(conn, found, now + timedelta(hours=27, minutes=10)) == []
