"""The download check (decision D61): two numbers per node and hour, the judge, and the owner's alert."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_reality_support import World, world_fixture  # noqa: F401 - the `world` fixture

from proxyhub.fleet.alerts import findings
from proxyhub.fleet.lifecycle import NodeState
from proxyhub.fleet.planner import FleetPlan, FleetSnapshot, NodeSnapshot
from proxyhub.reality import download
from proxyhub.reality.download import MB, Suspect, judge, judged_hours
from proxyhub.reality.report_schema import XrayCounters
from proxyhub.reality.store import store_xray_report
from proxyhub.settings_schema import FleetSettings

NOW = datetime(2026, 11, 8, 12, 20, tzinfo=UTC)
H1, H2 = judged_hours(NOW)


def test_the_judged_hours_are_the_last_two_complete_ones() -> None:
    assert (H1, H2) == (datetime(2026, 11, 8, 10, tzinfo=UTC), datetime(2026, 11, 8, 11, tzinfo=UTC))


def test_a_node_whose_users_get_little_back_is_a_suspect_compared_with_the_others() -> None:
    healthy = {H1: (20 * MB, 400 * MB), H2: (30 * MB, 500 * MB)}  # about 17 times what they send
    finland = {H1: (40 * MB, 6 * MB), H2: (35 * MB, 5 * MB)}  # "upload but no download"
    traffic = {"us-1": healthy, "us-2": healthy, "fi": finland}
    (s,) = judge(traffic, [H1, H2])
    assert (s.node_id, s.up, s.down) == ("fi", 75 * MB, 11 * MB) and s.peers_ratio is not None
    # Never a suspect while its users get back twice what they send, whatever the others do.
    browsing = {H1: (10 * MB, 25 * MB), H2: (10 * MB, 25 * MB)}
    video = {H1: (10 * MB, 900 * MB), H2: (10 * MB, 900 * MB)}
    assert judge({"a": browsing, "b": video, "c": video}, [H1, H2]) == []
    # Both hours must look blocked, each with enough traffic to judge.
    one_hour = {H1: (40 * MB, 1 * MB), H2: (40 * MB, 300 * MB)}
    quiet = {H1: (1 * MB, 0), H2: (1 * MB, 0)}
    assert judge({"x": one_hour, "y": quiet, "us": healthy}, [H1, H2]) == []
    # Alone, only users who got back less than they sent make a suspect.
    assert [s.node_id for s in judge({"fi": finland}, [H1, H2])] == ["fi"]
    assert judge({"ok": {H1: (10 * MB, 15 * MB), H2: (10 * MB, 12 * MB)}}, [H1, H2]) == []
    assert judge({"fi": finland}, []) == []


def test_the_report_carries_both_totals_or_neither() -> None:
    base = {
        "enabled": True,
        "up": True,
        "version": "26.10.31",
        "uptime_s": 1.0,
        "target_ok": None,
        "checked_target": None,
        "checked_server_name": None,
        "apply_error": None,
        "users": [],
    }
    x = XrayCounters.model_validate({**base, "uplink_octets": 300, "downlink_octets": 9000})
    assert (x.uplink_octets, x.downlink_octets) == (300, 9000)
    assert XrayCounters.model_validate(base).uplink_octets is None  # an older agent
    for bad in ({"uplink_octets": 1}, {"downlink_octets": 1}, {"uplink_octets": -1, "downlink_octets": 1}):
        with pytest.raises(ValidationError):
            XrayCounters.model_validate({**base, **bad})


def test_a_restart_seen_in_one_direction_resets_both() -> None:
    assert download.deltas((None, None), 1000, 5000) == (0, 0)  # the first totals teach nothing yet
    assert download.deltas((1000, 5000), 50 * MB, 40 * MB) == (50 * MB - 1000, 40 * MB - 5000)
    # Xray started again: up fell, down has already passed its old value. Both began at 0.
    assert download.deltas((50 * MB, 40 * MB), 10 * MB, 45 * MB) == (10 * MB, 45 * MB)
    assert download.deltas((50 * MB, 40 * MB), 60 * MB, 5 * MB) == (60 * MB, 5 * MB)


async def _report(world: World, node: uuid.UUID, up: int, down: int, at: datetime) -> None:
    async with world.engine.begin() as conn:
        await store_xray_report(
            conn,
            node,
            enabled=True,
            up=True,
            version="26.10.31",
            uptime_s=60.0,
            target_ok=None,
            apply_error=None,
            users=[],
            now=at,
            traffic=(up, down),
        )


async def test_totals_become_bytes_per_node_and_hour(world: World) -> None:
    node = await world.node(slot=3, location="hel1")
    at = datetime.now(UTC).replace(minute=5, second=0, microsecond=0)
    await _report(world, node, 1000, 5000, at)  # the first totals teach nothing yet
    await _report(world, node, 41 * MB, 7 * MB, at + timedelta(minutes=10))
    await _report(world, node, 3 * MB, 1 * MB, at + timedelta(minutes=20))  # Xray started again
    rows = await world.rows("SELECT hour, up, down FROM reality_traffic_hours WHERE node_id = :n", n=node)
    assert [(r.hour, r.up, r.down) for r in rows] == [
        (download.hour_of(at), 41 * MB - 1000 + 3 * MB, 8 * MB - 5000)
    ]
    # Kept KEEP long; older hours go with the next report.
    await world.rows(
        "INSERT INTO reality_traffic_hours (node_id, hour, up, down) VALUES (:n, :h, 1, 1)",
        n=node,
        h=download.hour_of(at) - download.KEEP - timedelta(hours=1),
    )
    await _report(world, node, 4 * MB, 2 * MB, at + timedelta(minutes=30))
    assert len(await world.rows("SELECT 1 FROM reality_traffic_hours WHERE node_id = :n", n=node)) == 1


async def test_suspects_are_read_for_live_nodes(world: World) -> None:
    fi = await world.node(slot=3, location="hel1", ipv4="203.0.113.30")
    us = await world.node(slot=1, location="ash", ipv4="203.0.113.31")
    now = datetime.now(UTC)
    h1, h2 = judged_hours(now)
    for node, (up, down) in ((fi, (40 * MB, 2 * MB)), (us, (20 * MB, 300 * MB))):
        for h in (h1, h2):
            await world.rows(
                "INSERT INTO reality_traffic_hours (node_id, hour, up, down) VALUES (:n, :h, :u, :d)",
                n=node,
                h=h,
                u=up,
                d=down,
            )
    async with world.engine.connect() as conn:
        assert [s.node_id for s in await download.suspects(conn, now)] == [str(fi)]
    await world.rows("UPDATE nodes SET state = 'deleted' WHERE id = :n", n=fi)
    async with world.engine.connect() as conn:
        assert await download.suspects(conn, now) == []


def test_the_owner_is_told_with_the_numbers_and_what_to_do() -> None:
    node = NodeSnapshot(
        id="node-3",
        name="n3",
        account_id="acc-a",
        server_type="cpx11",
        location="hel1",
        state=NodeState.READY,
        hetzner_id=1003,
        created_at=NOW - timedelta(days=1),
        deleted_at=None,
        slot=3,
    )
    snap = FleetSnapshot(
        now=NOW,
        settings=FleetSettings(),
        nodes=(node,),
        download_suspects=(Suspect("node-3", 75 * MB, 11 * MB, 16.7),),
    )
    plan = FleetPlan(
        generated_at=NOW, dry_run=True, actions=(), projected_month_eur=0, projected_after_eur=0, live_nodes=1
    )
    (f,) = [f for f in findings(plan, snap) if f.code == "download_suspect"]
    assert "جایگاه ۳ (hel1)" in f.text and "۷۵ مگ فرستادند" in f.text and "۱۱ مگ گرفتند" in f.text
    assert "۱۷ برابر" in f.text and "«تنظیمات ناوگان»" in f.text and "خودش جایگزین نمیکند" in f.text
    assert f.detail == {"nodes": [{"node_id": "node-3", "up_mb": 75.0, "down_mb": 11.0, "peers_ratio": 16.7}]}
