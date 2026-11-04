"""The planner's live-fleet rules (round 3), pure: verdicts, repair window, backoff, burned IPs,
duplicates and nodes without a server. Plus a property test over random fleets with every new
field: no allowed creation ever breaks a cap, and no allowed deletion ever skips the delete gate.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_fleet_planner import NOW, account, creates, full_fleet, node, offers_for, open_settings, plan_for
from test_fleet_support import LOCATIONS, server

from proxyhub.fleet.lifecycle import LIVE_STATES, NodeState
from proxyhub.fleet.planner import (
    CREATION_WINDOW,
    REPAIR_WINDOW,
    REPLACEMENT_SETTLE,
    REQUEST_BACKOFF,
    SPARE_SLOTS,
    VERDICT_MAX_AGE,
    FleetPlan,
    FleetSnapshot,
    NodeSnapshot,
    build_plan,
    counts_as_creation,
)
from proxyhub.safety.gates import DELETABLE_ROLES
from proxyhub.settings_schema import FleetSettings

S = NodeState


def with_(n: NodeSnapshot, **kw: object) -> NodeSnapshot:
    return dataclasses.replace(n, **kw)  # type: ignore[arg-type]


def deletes(plan: FleetPlan) -> list:  # type: ignore[type-arg]
    return [a for a in plan.actions if a.action == "delete"]


def servers_for(nodes: list[NodeSnapshot]) -> dict[tuple[str, int], object]:
    return {(n.account_id, n.hetzner_id): server(n.hetzner_id) for n in nodes if n.hetzner_id is not None}


def test_a_fresh_blocked_verdict_is_planned_like_a_burn_an_old_one_is_not() -> None:
    nodes = full_fleet()
    nodes[0] = with_(nodes[0], verdict="blocked", verdict_at=NOW - timedelta(minutes=5))
    (c,) = creates(plan_for(open_settings(node_cap_total=10), nodes=tuple(nodes)))
    assert c.target == "slot-1" and c.allowed and c.context["kind"] == "replace"
    assert c.context["location"] != nodes[0].location and c.context["avoid"] == [nodes[0].location]
    nodes[0] = with_(nodes[0], verdict_at=NOW - VERDICT_MAX_AGE - timedelta(minutes=1))
    assert creates(plan_for(open_settings(), nodes=tuple(nodes))) == []
    nodes[0] = with_(nodes[0], verdict="suspect", verdict_at=NOW)
    assert creates(plan_for(open_settings(), nodes=tuple(nodes))) == []


def test_an_unhealthy_node_waits_out_its_repair_window() -> None:
    nodes = full_fleet()
    nodes[1] = with_(nodes[1], state=S.UNHEALTHY, state_since=NOW - REPAIR_WINDOW + timedelta(minutes=1))
    plan = plan_for(open_settings(), nodes=tuple(nodes))
    assert creates(plan) == [] and plan.actions[1].reasons == ("slot_unhealthy",)
    nodes[1] = with_(nodes[1], state_since=NOW - REPAIR_WINDOW - timedelta(minutes=1))
    plan = plan_for(open_settings(), nodes=tuple(nodes), servers=servers_for(nodes))
    (c,) = creates(plan)
    assert c.target == "slot-2" and c.allowed and c.context["location"] != nodes[1].location
    assert deletes(plan) == []  # it served before: it stays until the replacement has settled


def test_a_node_that_never_served_leaves_when_its_window_is_over() -> None:
    nodes = full_fleet()
    nodes[2] = with_(nodes[2], state=S.UNHEALTHY, ready_at=None, state_since=NOW - REPAIR_WINDOW * 2)
    plan = plan_for(open_settings(), nodes=tuple(nodes), servers=servers_for(nodes))
    (d,) = deletes(plan)
    assert d.target == nodes[2].name and d.allowed and d.context["why"] == "unhealthy"
    assert [c.target for c in creates(plan)] == ["slot-3"]


def test_two_ready_nodes_in_a_slot_keep_the_newest_once_settled() -> None:
    nodes = full_fleet()
    newer = node(20, slot=1, location="nbg1", ready_min_ago=REPLACEMENT_SETTLE.total_seconds() / 120)
    plan = plan_for(open_settings(), nodes=(*nodes, newer), servers=servers_for([*nodes, newer]))
    assert deletes(plan) == [] and plan.actions[0].reasons == ("replacement_settling",)
    settled = with_(newer, ready_at=NOW - timedelta(minutes=10))
    plan = plan_for(open_settings(), nodes=(*nodes, settled), servers=servers_for([*nodes, settled]))
    (d,) = deletes(plan)
    assert d.target == nodes[0].name and d.context["why"] == "duplicate" and d.allowed


def test_a_refused_request_backs_off_and_is_no_creation() -> None:
    refused = with_(
        node(30, slot=1, state=S.DELETED, created_min_ago=3),
        hetzner_id=None,
        create_attempted=False,
        deleted_at=NOW - timedelta(minutes=2),
    )
    assert not counts_as_creation(refused, NOW)
    attempted = with_(refused, create_attempted=True)
    assert counts_as_creation(attempted, NOW)  # a create call was made: it may have cost money
    plan = plan_for(open_settings(target_slots=2, creations_per_hour_cap=1), nodes=(refused,))
    assert plan.actions[0].reasons == ("request_backoff",)
    (c,) = creates(plan)
    assert c.target == "slot-2" and c.allowed  # the hourly cap of 1 is still free
    later = with_(refused, deleted_at=NOW - REQUEST_BACKOFF - timedelta(seconds=1))
    assert [c.target for c in creates(plan_for(open_settings(target_slots=1), nodes=(later,)))] == ["slot-1"]


def test_a_location_that_gave_a_burned_ip_is_tried_last() -> None:
    gone = with_(
        node(31, slot=1, state=S.DELETED, location="fsn1", created_min_ago=10),
        retire_reason="burned_ip",
        deleted_at=NOW - timedelta(minutes=5),
    )
    (c,) = creates(plan_for(open_settings(target_slots=1), nodes=(gone,)))
    assert c.allowed and c.context["location"] != "fsn1" and c.context["avoid"] == ["fsn1"]
    # Every location gave a burned IP within the hour: try again anyway (ip_history decides).
    others = [
        with_(gone, id=f"x{i}", name=f"x{i}", location=loc, hetzner_id=2000 + i)
        for i, loc in enumerate(LOCATIONS)
    ]
    (c,) = creates(plan_for(open_settings(target_slots=1), nodes=tuple(others)))
    assert c.allowed and c.context["location"] == "fsn1"
    old = [with_(o, deleted_at=NOW - CREATION_WINDOW - timedelta(minutes=1)) for o in others]
    (c,) = creates(plan_for(open_settings(target_slots=1), nodes=tuple(old)))
    assert c.context["avoid"] == []


def test_a_burned_slot_never_reuses_its_location_even_if_none_is_left() -> None:
    nodes = [node(1, slot=1, state=S.BURNED, location="fsn1")]
    (c,) = creates(plan_for(open_settings(target_slots=1, locations=["fsn1"]), nodes=tuple(nodes)))
    assert not c.allowed and "no_other_location" in c.reasons


def test_a_node_without_a_server_is_closed_without_the_delete_gate() -> None:
    requested = with_(node(40, slot=12, state=S.REQUESTED, created_min_ago=1), hetzner_id=None)
    creating = with_(node(41, slot=13, state=S.CREATING, created_min_ago=1), hetzner_id=None)
    ready_no_id = with_(node(42, slot=14, state=S.READY), hetzner_id=None)
    plan = plan_for(open_settings(), nodes=(*full_fleet(), requested, creating, ready_no_id))
    by_target = {d.target: d for d in deletes(plan)}
    assert (
        by_target["n40"].allowed and by_target["n41"].allowed and by_target["n40"].context["why"] == "excess"
    )
    assert not by_target["n42"].allowed and "no_hetzner_id" in by_target["n42"].reasons
    plan = plan_for(open_settings(kill_switch=True), nodes=(requested,))
    assert not deletes(plan)[0].allowed and "kill_switch_on" in deletes(plan)[0].reasons


# --- property: caps and the delete gate, over random fleets with every round-3 field -----------------


@st.composite
def live_snapshots(draw: st.DrawFn) -> FleetSnapshot:
    now = NOW + timedelta(days=draw(st.integers(0, 27)), hours=draw(st.integers(0, 23)))
    nodes: list[NodeSnapshot] = []
    for i in range(draw(st.integers(0, 12))):
        state = draw(st.sampled_from(list(NodeState)))
        base = node(
            i,
            slot=draw(st.one_of(st.none(), st.integers(1, 11))),
            state=state,
            location=draw(st.sampled_from(LOCATIONS)),
            created_min_ago=draw(st.one_of(st.floats(61, 60 * 24 * 40), st.floats(0, 60))),
            ready_min_ago=draw(st.one_of(st.none(), st.floats(0, 120))),
            is_test=draw(st.booleans()),
            now=now,
        )
        nodes.append(
            with_(
                base,
                hetzner_id=draw(st.one_of(st.none(), st.just(base.hetzner_id))),
                create_attempted=draw(st.booleans()),
                verdict=draw(st.sampled_from([None, "ok", "blocked", "suspect", "unknown"])),
                verdict_at=now - timedelta(minutes=draw(st.floats(0, 120))),
                state_since=now - timedelta(minutes=draw(st.floats(0, 60))),
                retire_reason=draw(st.sampled_from([None, "burned_ip", "burned", "unhealthy"])),
                deleted_at=(now - timedelta(minutes=draw(st.floats(0, 90)))) if state == S.DELETED else None,
            )
        )
    observed = {
        ("acc-a", n.hetzner_id): server(
            n.hetzner_id,
            managed=draw(st.booleans()),
            role=draw(st.sampled_from(["node", "builder", "panel"])),
            protected=draw(st.booleans()),
        )
        for n in nodes
        if n.hetzner_id is not None and draw(st.booleans())
    }
    fleet = FleetSettings(
        kill_switch=draw(st.sampled_from([False, False, False, True])),
        dry_run=False,
        budget_cap_eur=draw(st.decimals(Decimal("1"), Decimal("200"), places=2)),
        node_cap_total=draw(st.integers(0, 20)),
        creations_per_hour_cap=draw(st.integers(1, 6)),
        target_slots=draw(st.integers(1, 10)),
        allowed_server_types=["cpx02"],
    )
    return FleetSnapshot(
        now=now,
        settings=fleet,
        accounts=(account(now=now),),
        nodes=tuple(nodes),
        offers=offers_for("acc-a"),
        servers=observed,  # type: ignore[arg-type]
    )


STATS: Counter[str] = Counter()


@settings(max_examples=1500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(snap=live_snapshots())
def test_live_fleet_never_breaks_a_cap_or_skips_the_delete_gate(snap: FleetSnapshot) -> None:
    plan = build_plan(snap, dry_run=False)
    s = snap.settings
    live = [n for n in snap.nodes if n.state in LIVE_STATES]
    allowed = [a for a in plan.actions if a.action == "create" and a.allowed]
    STATS["examples"] += 1
    if allowed:
        STATS["creates"] += 1
        assert not s.kill_switch and s.budget_cap_eur is not None and s.node_cap_total is not None
        assert len(live) + len(allowed) <= s.node_cap_total
        assert plan.projected_after_eur <= s.budget_cap_eur
        assert sum(1 for n in snap.nodes if counts_as_creation(n, snap.now)) + len(allowed) <= (
            s.creations_per_hour_cap
        )
        assert sum(1 for n in live if not n.is_test) + len(allowed) <= s.target_slots + SPARE_SLOTS
    by_name = {n.name: n for n in snap.nodes}
    for d in deletes(plan):
        if not d.allowed:
            continue
        STATS["deletes"] += 1
        n = by_name[d.target]
        assert not s.kill_switch and not n.is_test and n.state != S.DELETED
        if n.hetzner_id is None:
            assert n.state in (S.REQUESTED, S.CREATING, S.RETIRING)
            continue
        srv = snap.servers[(n.account_id, n.hetzner_id)]
        assert srv.labels.get("managed-by") == "proxy-hub" and srv.labels.get("role") in DELETABLE_ROLES
        assert not srv.delete_protected


def test_the_live_property_really_exercised_both_paths() -> None:
    if STATS["examples"] == 0:
        pytest.skip("the property test did not run in this session")
    assert STATS["creates"] * 20 >= STATS["examples"] and STATS["deletes"] * 20 >= STATS["examples"], STATS
