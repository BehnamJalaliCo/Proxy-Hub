"""Fleet planner (9 + 1 slots, D14) with live-price data shaped exactly as Hetzner returns it.

Central claim (CLAUDE.md rule 8): no creation is ever "allowed" when a cap is missing or would be
exceeded, whatever the fleet state; checked on hand-made scenarios and by a property test.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_fleet_support import LOCATIONS, live_offers, server

from proxyhub.fleet.lifecycle import LIVE_STATES, NodeState
from proxyhub.fleet.planner import (
    OBSERVATION_MAX_AGE,
    REPLACEMENT_SETTLE,
    SPARE_SLOTS,
    AccountOffer,
    AccountSnapshot,
    FleetPlan,
    FleetSnapshot,
    NodeSnapshot,
    build_plan,
)
from proxyhub.safety.budget import Price, rest_of_month_cost
from proxyhub.settings_schema import FleetSettings

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
CPX02 = Price(hourly=Decimal("0.0104"), monthly=Decimal("6.49"))
OFFERS = live_offers()
TYPES = ["cx23", "cpx11", "cpx02", "cpx12"]


def account(
    id_: str = "acc-a",
    *,
    state: str = "active",
    age_days: int = 10,
    observed_min_ago: float | None = 1,
    guard_ok: bool | None = True,
    servers_total: int | None = 0,
    limit: int | None = None,
    now: datetime = NOW,
) -> AccountSnapshot:
    return AccountSnapshot(
        id=id_,
        name=f"name-{id_}",
        state=state,
        created_at=now - timedelta(days=age_days),
        learned_server_limit=limit,
        observed_at=None if observed_min_ago is None else now - timedelta(minutes=observed_min_ago),
        guard_ok=guard_ok,
        servers_total=servers_total,
    )


def node(
    n: int,
    *,
    slot: int | None,
    state: NodeState = NodeState.READY,
    location: str | None = None,
    account_id: str = "acc-a",
    server_type: str = "cpx02",
    created_min_ago: float = 3 * 24 * 60,
    ready_min_ago: float | None = 3 * 24 * 60 - 5,
    is_test: bool = False,
    now: datetime = NOW,
) -> NodeSnapshot:
    return NodeSnapshot(
        id=f"node-{n}",
        name=f"n{n}",
        account_id=account_id,
        server_type=server_type,
        location=location or LOCATIONS[((slot or 1) - 1) % 3],
        state=state,
        hetzner_id=1000 + n,
        created_at=now - timedelta(minutes=created_min_ago),
        deleted_at=None,
        slot=slot,
        is_test=is_test,
        ready_at=None if ready_min_ago is None else now - timedelta(minutes=ready_min_ago),
    )


def offers_for(*account_ids: str) -> tuple[AccountOffer, ...]:
    return tuple(AccountOffer(a, o) for a in account_ids for o in OFFERS)


def open_settings(**kw: object) -> FleetSettings:
    base: dict[str, object] = {
        "kill_switch": False,
        "budget_cap_eur": Decimal("1000"),
        "node_cap_total": 20,
        "creations_per_hour_cap": 20,
        "allowed_server_types": TYPES,
    }
    base.update(kw)
    return FleetSettings.model_validate(base)


def plan_for(settings_: FleetSettings, **kw: object) -> FleetPlan:
    snap_kw: dict[str, object] = {"accounts": (account(),), "offers": offers_for("acc-a")}
    snap_kw.update(kw)
    return build_plan(FleetSnapshot(now=NOW, settings=settings_, **snap_kw))  # type: ignore[arg-type]


def creates(plan: FleetPlan) -> list:  # type: ignore[type-arg]
    return [a for a in plan.actions if a.action == "create"]


# --- fresh panel: nothing may happen ----------------------------------------------------------


def test_fresh_defaults_block_every_creation_with_all_reasons() -> None:
    plan = plan_for(FleetSettings())
    cs = creates(plan)
    assert [a.target for a in cs] == [f"slot-{i}" for i in range(1, 10)]
    for a in cs:
        assert not a.allowed
        assert {"kill_switch_on", "budget_cap_not_set", "node_cap_not_set"} <= set(a.reasons)
    assert plan.dry_run is True


def test_missing_caps_block_even_with_kill_switch_off() -> None:
    for budget, cap in [(None, 10), (Decimal("100"), None), (None, None)]:
        plan = plan_for(open_settings(budget_cap_eur=budget, node_cap_total=cap))
        assert creates(plan) and not any(a.allowed for a in creates(plan))


# --- caps applied cumulatively ----------------------------------------------------------------


def test_budget_cap_counts_every_planned_creation() -> None:
    # Early in the month a new CPX02 costs its full monthly price (6.49): 3 fit into 20 EUR, not 4.
    assert rest_of_month_cost(CPX02, NOW) == Decimal("6.49")
    plan = plan_for(open_settings(budget_cap_eur=Decimal("20")))
    allowed = [a.allowed for a in creates(plan)]
    assert allowed == [True] * 3 + [False] * 6
    assert all("budget_cap_reached" in a.reasons for a in creates(plan)[3:])
    assert plan.projected_after_eur == Decimal("19.47") <= Decimal("20")


def test_node_cap_counts_existing_and_planned_nodes() -> None:
    existing = (node(1, slot=1), node(2, slot=2))
    plan = plan_for(open_settings(node_cap_total=5), nodes=existing)
    cs = creates(plan)
    assert [a.target for a in cs] == [f"slot-{i}" for i in range(3, 10)]
    assert [a.allowed for a in cs] == [True] * 3 + [False] * 4
    assert all("node_cap_reached" in a.reasons for a in cs[3:])


def test_hourly_creation_cap_counts_recent_creations() -> None:
    recent = (node(1, slot=1, state=NodeState.BOOTING, created_min_ago=10, ready_min_ago=None),)
    plan = plan_for(open_settings(creations_per_hour_cap=3), nodes=recent)
    cs = creates(plan)
    assert [a.allowed for a in cs] == [True, True] + [False] * 6
    assert all("creation_rate_cap_reached" in a.reasons for a in cs[2:])


def test_test_nodes_count_against_node_cap_and_budget() -> None:
    tests = tuple(node(50 + i, slot=None, is_test=True) for i in range(3))
    plan = plan_for(open_settings(node_cap_total=4), nodes=tests)
    assert sum(a.allowed for a in creates(plan)) == 1
    assert len(creates(plan)) == 9  # test nodes never occupy a slot


# --- placement --------------------------------------------------------------------------------


def test_slots_spread_over_locations_with_cheapest_orderable_type() -> None:
    plan = plan_for(open_settings())
    cs = creates(plan)
    assert all(a.allowed for a in cs)
    assert Counter(a.context["location"] for a in cs) == {"fsn1": 3, "nbg1": 3, "hel1": 3}
    # cx23 (cheaper, but available=false) and cpx11 (deprecated) are never chosen.
    assert {a.context["server_type"] for a in cs} == {"cpx02"}
    assert cs[0].context["price_monthly_eur"] == "6.4900"


def test_youngest_active_account_first_then_next_when_full() -> None:
    young = account("young", age_days=5, servers_total=2, limit=4)
    old = account("old", age_days=40)
    plan = plan_for(open_settings(), accounts=(young, old), offers=offers_for("young", "old"))
    placed = [a.context["account_id"] for a in creates(plan)]
    assert placed[:2] == ["young", "young"]  # 2 + 2 = learned limit 4
    assert set(placed[2:]) == {"old"}
    assert all(a.allowed for a in creates(plan))


def test_stale_or_red_guard_moves_to_next_account_or_blocks() -> None:
    stale = account("stale", age_days=1, observed_min_ago=OBSERVATION_MAX_AGE.total_seconds() / 60 + 1)
    red = account("red", age_days=2, guard_ok=False)
    plan = plan_for(open_settings(), accounts=(stale, red), offers=offers_for("stale", "red"))
    for a in creates(plan):
        assert not a.allowed
        assert "account_guard_failed" in a.reasons
    assert "account_observation_stale" in creates(plan)[0].reasons

    good = account("good", age_days=30)
    plan = plan_for(open_settings(), accounts=(stale, red, good), offers=offers_for("stale", "red", "good"))
    assert all(a.allowed and a.context["account_id"] == "good" for a in creates(plan))


def test_no_active_account_and_no_price_block() -> None:
    plan = plan_for(open_settings(), accounts=(account(state="draining"),))
    assert all("no_active_account" in a.reasons and not a.allowed for a in creates(plan))
    plan = plan_for(open_settings(), offers=())
    assert all("no_live_price" in a.reasons and not a.allowed for a in creates(plan))


def test_live_node_without_live_price_blocks_all_creation() -> None:
    odd = (node(1, slot=1, server_type="ccx99"),)
    plan = plan_for(open_settings(), nodes=odd)
    assert plan.missing_prices == ("n1",)
    assert creates(plan) and all("projection_incomplete" in a.reasons for a in creates(plan))


# --- make-before-break (D14) ------------------------------------------------------------------


def full_fleet(**overrides: dict[str, object]) -> list[NodeSnapshot]:
    nodes = [node(i, slot=i) for i in range(1, 10)]
    for i, kw in overrides.items():
        idx = int(i.removeprefix("s")) - 1
        nodes[idx] = node(idx + 1, slot=idx + 1, **kw)  # type: ignore[arg-type]
    return nodes


def test_healthy_full_fleet_plans_nothing() -> None:
    plan = plan_for(open_settings(), nodes=tuple(full_fleet()))
    assert not creates(plan) and all(a.reasons == ("slot_healthy",) for a in plan.actions)


def test_burned_slot_gets_replacement_in_another_location_first() -> None:
    nodes = full_fleet(s1={"state": NodeState.BURNED})
    plan = plan_for(open_settings(node_cap_total=10), nodes=tuple(nodes))
    (c,) = creates(plan)
    assert c.target == "slot-1" and c.allowed and c.context["kind"] == "replace"
    assert c.context["location"] != "fsn1"
    assert not [a for a in plan.actions if a.action == "delete"]  # old node stays until replaced


def test_only_one_spare_at_a_time() -> None:
    nodes = full_fleet(s1={"state": NodeState.BURNED}, s2={"state": NodeState.BURNED})
    cs = creates(plan_for(open_settings(), nodes=tuple(nodes)))
    assert [a.allowed for a in cs] == [True, False]
    assert "spare_in_use" in cs[1].reasons
    assert len(nodes) + sum(a.allowed for a in cs) <= 9 + SPARE_SLOTS


def test_burned_node_deleted_only_after_replacement_settled() -> None:
    burned = node(1, slot=1, state=NodeState.BURNED)
    fresh = node(10, slot=1, location="nbg1", ready_min_ago=REPLACEMENT_SETTLE.total_seconds() / 60 / 2)
    rest = full_fleet()[1:]
    servers = {("acc-a", 1001): server(1001)}
    plan = plan_for(open_settings(), nodes=(burned, fresh, *rest), servers=servers)
    assert not [a for a in plan.actions if a.action == "delete"]
    assert plan.actions[0].reasons == ("replacement_settling",)

    settled = node(10, slot=1, location="nbg1", ready_min_ago=10)
    plan = plan_for(open_settings(), nodes=(burned, settled, *rest), servers=servers)
    (d,) = [a for a in plan.actions if a.action == "delete"]
    assert d.target == "n1" and d.allowed and d.context["why"] == "replaced_burned"

    plan = plan_for(open_settings(kill_switch=True), nodes=(burned, settled, *rest), servers=servers)
    (d,) = [a for a in plan.actions if a.action == "delete"]
    assert not d.allowed and "kill_switch_on" in d.reasons


def test_delete_needs_label_and_live_observation() -> None:
    retiring = node(1, slot=1, state=NodeState.RETIRING)
    rest = full_fleet()[1:]
    no_label = {("acc-a", 1001): server(1001, managed=False)}
    plan = plan_for(open_settings(), nodes=(retiring, *rest), servers=no_label)
    (d,) = [a for a in plan.actions if a.action == "delete"]
    assert not d.allowed and "label_managed_by_missing" in d.reasons

    plan = plan_for(open_settings(), nodes=(retiring, *rest), servers={})
    (d,) = [a for a in plan.actions if a.action == "delete"]
    assert not d.allowed and "server_not_observed" in d.reasons

    protected = {("acc-a", 1001): server(1001, protected=True)}
    plan = plan_for(open_settings(), nodes=(retiring, *rest), servers=protected)
    (d,) = [a for a in plan.actions if a.action == "delete"]
    assert not d.allowed and "delete_protection_on" in d.reasons


def test_orphan_server_is_reported_never_deleted() -> None:
    plan = plan_for(open_settings(), nodes=tuple(full_fleet()), servers={("acc-a", 777): server(777)})
    orphan = [a for a in plan.actions if a.target == "server:777"]
    assert orphan and orphan[0].action == "none" and orphan[0].reasons == ("orphan_server_not_in_db",)


def test_fingerprint_ignores_cost_drift_but_not_decisions() -> None:
    a = plan_for(open_settings(budget_cap_eur=Decimal("20")))
    later = build_plan(
        FleetSnapshot(
            now=NOW + timedelta(hours=3),
            settings=open_settings(budget_cap_eur=Decimal("20")),
            accounts=(account(observed_min_ago=-179),),
            offers=offers_for("acc-a"),
        )
    )
    assert a.fingerprint() == later.fingerprint()
    assert a.fingerprint() != plan_for(open_settings(budget_cap_eur=Decimal("30"))).fingerprint()


# --- property tests: caps are never exceeded --------------------------------------------------
# Two generators: "any" explores every combination (most of them blocked); "open" always has the
# kill switch off, both caps set and one healthy, freshly observed, priced active account, so a
# real share of its examples (about 10 %) contains allowed creations, asserted by the last test.

STATES = ["verifying", "preparing", "active", "draining", "retired", "locked"]


@st.composite
def snapshots(draw: st.DrawFn, open_fleet: bool = False) -> FleetSnapshot:
    now = NOW + timedelta(days=draw(st.integers(0, 27)), hours=draw(st.integers(0, 23)))
    n_accounts = draw(st.integers(1 if open_fleet else 0, 3))
    accts: list[AccountSnapshot] = []
    for i in range(n_accounts):
        forced = open_fleet and i == 0
        accts.append(
            account(
                f"acc-{i}",
                state="active" if forced else draw(st.sampled_from(STATES)),
                age_days=draw(st.integers(0, 90)),
                observed_min_ago=draw(st.floats(0, 25))
                if forced
                else draw(st.one_of(st.none(), st.floats(0, 60))),
                guard_ok=True if forced else draw(st.one_of(st.none(), st.booleans())),
                servers_total=draw(st.one_of(st.none(), st.integers(0, 12))),
                limit=draw(st.one_of(st.none(), st.integers(0, 14))),
                now=now,
            )
        )
    ids = [a.id for a in accts] or ["acc-x"]
    priced = [a for i, a in enumerate(ids) if (open_fleet and i == 0) or draw(st.booleans())]
    types = (
        st.sampled_from(["cpx02"] * 6 + ["cpx12"] * 3 + ["ccx99"])
        if open_fleet
        else st.sampled_from(["cpx02", "cpx12", "ccx99"])
    )
    nodes = tuple(
        node(
            i,
            slot=draw(st.one_of(st.none(), st.integers(1, 13))),
            state=draw(st.sampled_from(list(NodeState))),
            location=draw(st.sampled_from(LOCATIONS)),
            account_id=draw(st.sampled_from(priced if open_fleet else ids)),
            server_type=draw(types),
            created_min_ago=draw(st.one_of(st.floats(61, 60 * 24 * 40), st.floats(0, 60))),
            ready_min_ago=draw(st.one_of(st.none(), st.floats(0, 60))),
            is_test=draw(st.booleans()),
            now=now,
        )
        for i in range(draw(st.integers(0, 12)))
    )
    if open_fleet:
        fleet = FleetSettings(
            kill_switch=False,
            budget_cap_eur=draw(st.decimals(Decimal("1"), Decimal("200"), places=2)),
            node_cap_total=draw(st.integers(0, 14)),
            creations_per_hour_cap=draw(st.integers(1, 6)),
            target_slots=draw(st.integers(1, 12)),
            allowed_server_types=["cpx02", *draw(st.lists(st.sampled_from(TYPES), max_size=3, unique=True))],
        )
    else:
        fleet = FleetSettings(
            kill_switch=draw(st.booleans()),
            budget_cap_eur=draw(st.one_of(st.none(), st.decimals(Decimal("0.01"), Decimal("200"), places=2))),
            node_cap_total=draw(st.one_of(st.none(), st.integers(0, 14))),
            creations_per_hour_cap=draw(st.integers(0, 6)),
            target_slots=draw(st.integers(0, 12)),
            allowed_server_types=draw(st.lists(st.sampled_from(TYPES), min_size=1, max_size=4, unique=True)),
        )
    return FleetSnapshot(
        now=now, settings=fleet, accounts=tuple(accts), nodes=nodes, offers=offers_for(*priced)
    )


PROPERTY_STATS: Counter[str] = Counter()


def check_caps(snap: FleetSnapshot) -> int:
    """Assert every cap for the plan's allowed creations; returns how many were allowed."""
    plan = build_plan(snap)
    s = snap.settings
    allowed = [a for a in plan.actions if a.action == "create" and a.allowed]
    if not allowed:
        return 0
    live = [n for n in snap.nodes if n.state in LIVE_STATES]
    assert not s.kill_switch
    assert s.budget_cap_eur is not None and s.node_cap_total is not None
    assert len(live) + len(allowed) <= s.node_cap_total
    assert plan.projected_after_eur <= s.budget_cap_eur
    assert not plan.missing_prices
    recent = sum(1 for n in snap.nodes if n.created_at > snap.now - timedelta(hours=1))
    assert recent + len(allowed) <= s.creations_per_hour_cap
    fleet_live = sum(1 for n in live if not n.is_test)
    assert fleet_live + len(allowed) <= s.target_slots + SPARE_SLOTS
    by_id = {a.id: a for a in snap.accounts}
    for acc_id, count in Counter(a.context["account_id"] for a in allowed).items():
        acc = by_id[acc_id]
        assert acc.state == "active" and acc.guard_ok is True
        assert acc.observed_at is not None and snap.now - acc.observed_at <= OBSERVATION_MAX_AGE
        if acc.learned_server_limit is not None:
            in_db = sum(1 for n in live if n.account_id == acc_id)
            assert max(acc.servers_total or 0, in_db) + count <= acc.learned_server_limit
    for a in allowed:
        assert a.context["server_type"] in s.allowed_server_types
        assert a.context["server_type"] not in ("cx23", "cpx11")  # unavailable / deprecated
    return len(allowed)


@settings(max_examples=2000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(snap=snapshots())
def test_no_allowed_creation_ever_breaks_a_cap_any_state(snap: FleetSnapshot) -> None:
    check_caps(snap)


@settings(max_examples=2000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(snap=snapshots(open_fleet=True))
def test_no_allowed_creation_ever_breaks_a_cap_open_fleet(snap: FleetSnapshot) -> None:
    PROPERTY_STATS["examples"] += 1
    if check_caps(snap):
        PROPERTY_STATS["with_allowed"] += 1


def test_property_test_really_exercised_allowed_creations() -> None:
    """Guard against a vacuous property: many open-fleet examples must contain allowed creations."""
    if PROPERTY_STATS["examples"] == 0:
        pytest.skip("the open-fleet property test did not run in this session")
    assert PROPERTY_STATS["with_allowed"] * 20 >= PROPERTY_STATS["examples"], PROPERTY_STATS
