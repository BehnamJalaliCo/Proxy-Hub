"""P15 (pure): TLS handshakes from Iran decide, and location scores steer the fleet planner.
Globalping answers are the real recorded shapes in tests/test_net_support.py (decision D10); nothing
imitates a service.
"""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from test_fleet_planner import NOW, account, creates, node, offers_for, open_settings, plan_for
from test_net_support import (
    ARVAN_PROBE,
    GP_HTTPS_APPLE_REUSED,
    GP_HTTPS_GITHUB,
    GP_HTTPS_GITHUB_MCI,
    GP_HTTPS_PANEL_443,
    GP_HTTPS_PANEL_8443,
    GP_HTTPS_PANEL_BLOCKED,
    HTTP_RESET_ARVAN,
    HTTP_TLS_TIMEOUT_RIGHTEL_443,
    PANEL_IP,
    deep,
    gp_https,
)

from proxyhub.fleet.alerts import DAILY, findings
from proxyhub.fleet.locations import CLASS_RANK, ReachRow, aggregate, classify, location_classes, reach_json
from proxyhub.fleet.planner import FleetSnapshot, build_plan
from proxyhub.probes import service
from proxyhub.probes.globalping import RateLimit, https_body, location_asns, parse_http
from proxyhub.probes.service import Target, node_network_states, tls_cost, tls_fit
from proxyhub.probes.settings import ProbeLocation, ProbeSettings
from proxyhub.probes.types import Measurement, NetworkObs
from proxyhub.probes.verdict import TLS_SOURCES, RoundObs, SourceObservation, decide, observe_tls

KEYS = ["asn:197207", "asn:57218"]
KEY_ASNS = [197207, 57218]
T0 = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)


# --- Globalping HTTPS: request and parser over real answers --------------------------------------


def test_https_body_matches_the_published_request_schema() -> None:
    locations = ProbeSettings().tls_location_bodies
    assert locations == [
        {"asn": 197207, "limit": 3},
        {"asn": 57218, "limit": 2},
        {"country": "IR", "limit": 3},
    ]
    body = https_body("slot1.example.com", 8443, locations)
    assert body == {
        "type": "http",
        "target": "slot1.example.com",
        "locations": locations,
        "measurementOptions": {"protocol": "HTTPS", "port": 8443, "request": {"method": "HEAD", "path": "/"}},
    }
    assert body["locations"] is not locations  # copies, never the settings' own objects
    assert (
        https_body("x.example.com", 443, "2ezQw50QA0u8rglRt00021Rtv")["locations"]
        == "2ezQw50QA0u8rglRt00021Rtv"
    )
    assert location_asns(locations) == KEY_ASNS


def test_parse_http_real_answers() -> None:
    nets = {n.network_key: n for n in parse_http(GP_HTTPS_GITHUB, KEY_ASNS)}
    rightel = nets["asn:57218"]
    assert (rightel.network, rightel.ok, rightel.state, rightel.rtt_ms) == ("رایتل", 1, "up", 548.0)
    assert rightel.detail == {"code": 200, "tls_ms": 130.0, "total_ms": 548.0, "tls": "TLSv1.3"}
    assert nets["asn:202468"].state == "up" and nets["asn:59441"].state == "up"
    mci = nets["asn:197207"]
    assert not mci.had_probe and mci.detail == {"no_probe": True}  # no MCI probe online (recorded)

    panel = {n.network_key: n for n in parse_http(GP_HTTPS_PANEL_443, KEY_ASNS, PANEL_IP)}
    # Rightel "connected" over TCP in 34 ms (its own answer) and the handshake timed out: closed.
    assert panel["asn:57218"].state == "down"
    assert panel["asn:57218"].detail == {"failure": "tls_timeout", "tcp_ms": 34.0}
    assert {k for k, n in panel.items() if n.state == "up"} == {"asn:202468", "asn:34918", "asn:206065"}

    port = {n.network_key: n for n in parse_http(GP_HTTPS_PANEL_8443, KEY_ASNS, PANEL_IP)}
    assert port["asn:57218"].detail["failure"] == "tls_timeout"
    assert port["asn:202468"].detail == {"failure": "tcp_timeout", "tcp_ms": None}  # no fake answer here

    reset = gp_https("2XYEmeCg0yax8G3S900021Rtw", PANEL_IP, 443, [(ARVAN_PROBE, HTTP_RESET_ARVAN)], at="x")
    (r,) = parse_http(reset, (), PANEL_IP)
    assert r.state == "down" and r.detail["failure"] == "reset"

    apple = {n.network_key: n for n in parse_http(GP_HTTPS_APPLE_REUSED, KEY_ASNS)}
    assert apple["asn:57218"].state == "up" and apple["asn:57218"].detail["tls"] == "TLSv1.3"


def test_the_real_p15_block_is_blocked_and_never_from_tcp() -> None:
    """Real answers of 2026-11-05: the panel (fsn1) timed out in the TLS handshake from MCI and from
    Rightel, after both had "connected" over TCP in about 60 ms, while github.com answered 200 from
    both. Two such rounds are "blocked"."""
    node = Measurement(
        "globalping", "https", "node", "panel.beyra-ai.com", 443, "ok", T0,
        networks=tuple(parse_http(GP_HTTPS_PANEL_BLOCKED, KEY_ASNS, PANEL_IP)),
    )  # fmt: skip
    nets = {n.network_key: n for n in node.networks}
    assert nets["asn:197207"].detail == {"failure": "tls_timeout", "tcp_ms": 64.0}  # TCP "worked"
    assert nets["asn:57218"].detail == {"failure": "tls_timeout", "tcp_ms": 52.0}
    control_nets = [
        *parse_http(GP_HTTPS_GITHUB_MCI, [197207]),
        *(n for n in parse_http(GP_HTTPS_GITHUB, [57218]) if n.network_key == "asn:57218"),
    ]
    control = Measurement(
        "globalping", "https", "control", "github.com", 443, "ok", T0, networks=tuple(control_nets)
    )
    obs = observe_tls([node], control, KEYS)
    assert (obs.state, obs.valid_networks, obs.up_networks) == ("unreachable", 2, 0)
    rounds = [RoundObs(T0 + timedelta(minutes=m), {"https": obs}) for m in (0, 30)]
    assert decide(rounds, now=T0 + timedelta(minutes=31), sources=TLS_SOURCES) == (
        "blocked",
        ["all_unreachable"],
    )


def test_parse_http_gives_no_data_for_what_says_nothing_about_the_node() -> None:
    # The name resolved to another address than the node's (a hijacked or stale answer).
    elsewhere = {n.network_key: n for n in parse_http(GP_HTTPS_PANEL_443, KEY_ASNS, "203.0.113.10")}
    assert all(n.state == "nodata" for k, n in elsewhere.items() if n.had_probe)
    assert elsewhere["asn:57218"].detail == {"failure": "resolved_elsewhere"}
    raw = deep(GP_HTTPS_PANEL_443)
    # A failure on the probe's side, and a target failure without any address (DNS gave nothing).
    raw["results"][0]["result"] = {"status": "failed", "rawOutput": "x", "failureSource": "internal"}
    raw["results"][1]["result"] = {**HTTP_TLS_TIMEOUT_RIGHTEL_443, "resolvedAddress": None}
    raw["results"][2]["result"] = {"status": "offline", "rawOutput": ""}
    nets = {n.network_key: n for n in parse_http(raw, KEY_ASNS, PANEL_IP)}
    assert nets["asn:57218"].state == "nodata" and nets["asn:57218"].detail == {"failure": "internal"}
    assert nets["asn:202468"].state == "nodata" and nets["asn:34918"].detail == {"failure": "offline"}
    assert nets["asn:206065"].state == "up"


def test_probe_settings_tls_defaults_and_validation() -> None:
    s = ProbeSettings()
    assert (s.interval_s, s.max_round_age_s, s.tls_control_host) == (1800, 5400, "github.com")
    assert s.tls_probe_limit == 8 and s.key_networks == KEYS and s.verdict_asns == KEY_ASNS
    with pytest.raises(ValidationError):
        ProbeLocation.model_validate({"asn": 197207, "country": "IR"})
    with pytest.raises(ValidationError):
        ProbeLocation.model_validate({"limit": 2})
    with pytest.raises(ValidationError):
        ProbeLocation.model_validate({"country": "Iran"})
    with pytest.raises(ValidationError):
        ProbeSettings.model_validate({"verdict_asns": [197207, 197207]})
    with pytest.raises(ValidationError):
        ProbeSettings.model_validate({"tls_control_host": "1.1.1.1/x"})
    assert ProbeLocation.model_validate({"country": "ir", "limit": 2}).body() == {"country": "IR", "limit": 2}


# --- the TLS observation and verdict ----------------------------------------------------------------


def tls(port: int, nets: dict[str, str], *, role: str = "node", status: str = "ok") -> Measurement:
    def obs(key: str, state: str) -> NetworkObs:
        counts = {"up": {"ok": 1}, "down": {"fail": 1}, "nodata": {"nodata": 1}}[state]
        return NetworkObs(key, key, None, **counts)

    return Measurement(
        "globalping", "https", role, "slot1.example.com", port, status, T0,  # type: ignore[arg-type]
        networks=tuple(obs(k, v) for k, v in nets.items()),
    )  # fmt: skip


CONTROL = tls(443, {"asn:197207": "up", "asn:57218": "up", "asn:202468": "up"}, role="control")


def test_observe_tls_needs_both_key_networks_on_every_port_for_unreachable() -> None:
    both_down = {"asn:197207": "down", "asn:57218": "down", "asn:202468": "up"}
    o = observe_tls([tls(443, both_down), tls(8443, both_down)], CONTROL, KEYS)
    assert (o.state, o.valid_networks, o.up_networks) == ("unreachable", 4, 0)
    assert o.cells == {
        "443": {"asn:197207": "down", "asn:57218": "down", "asn:202468": "up"},
        "8443": {"asn:197207": "down", "asn:57218": "down", "asn:202468": "up"},
    }
    # 443 open from MCI, 8443 closed (hil on MCI, P15): not an address block.
    hil = observe_tls(
        [tls(443, {"asn:197207": "up", "asn:57218": "down"}), tls(8443, both_down)], CONTROL, KEYS
    )
    assert hil.state == "partial"
    assert (
        observe_tls([tls(443, {"asn:197207": "up", "asn:57218": "up"})], CONTROL, KEYS).state == "reachable"
    )
    # MCI without a probe: reachable from what was measured, never blocked from half the picture.
    assert observe_tls([tls(443, {"asn:57218": "up"})], CONTROL, KEYS).state == "reachable"
    half = observe_tls([tls(443, {"asn:57218": "down"})], CONTROL, KEYS)
    assert (half.state, half.reason) == ("invalid", "incomplete")
    # Only where the control worked from the same network does a cell count.
    no_mci = tls(443, {"asn:197207": "down", "asn:57218": "up"}, role="control")
    assert observe_tls([tls(443, both_down)], no_mci, KEYS).reason == "incomplete"
    assert observe_tls(
        [tls(443, both_down)], tls(443, {"asn:197207": "down"}, role="control"), KEYS
    ).reason == ("control_failed")
    assert observe_tls([tls(443, both_down)], None, KEYS).reason == "source_missing"
    assert observe_tls([], CONTROL, KEYS).reason == "source_missing"
    assert (
        observe_tls([tls(443, both_down, status="rate_limited")], CONTROL, KEYS).reason == "node_rate_limited"
    )
    limited = tls(443, {}, role="control", status="rate_limited")
    assert observe_tls([tls(443, both_down)], limited, KEYS).reason == "control_rate_limited"
    assert observe_tls([tls(443, {"asn:202468": "down"})], CONTROL, KEYS).reason == "no_valid_networks"


def test_source_observation_round_trips_its_cells() -> None:
    o = observe_tls([tls(443, {"asn:197207": "down", "asn:57218": "down"})], CONTROL, KEYS)
    back = SourceObservation.from_json(o.to_json())
    assert back == o and back.cells["443"]["asn:57218"] == "down"
    assert "cells" not in SourceObservation("reachable").to_json()
    assert SourceObservation.from_json({"state": "reachable", "cells": "junk"}).cells == {}


_cell = st.sampled_from(["up", "down", "nodata"])


@settings(max_examples=300)
@given(st.lists(st.tuples(_cell, _cell, _cell, _cell), min_size=1, max_size=4))
def test_property_blocked_needs_tls_failing_from_mci_and_rightel_in_every_round(
    rounds: list[tuple[str, str, str, str]],
) -> None:
    """Rounds of (MCI 443, Rightel 443, MCI 8443, Rightel 8443): blocked only if every round has all
    four down; ok only if no valid cell of the latest two rounds is down."""
    obs: list[RoundObs] = []
    for i, (m4, r4, m8, r8) in enumerate(rounds):
        o = observe_tls(
            [tls(443, {"asn:197207": m4, "asn:57218": r4}), tls(8443, {"asn:197207": m8, "asn:57218": r8})],
            CONTROL,
            KEYS,
        )
        obs.append(RoundObs(T0 + timedelta(minutes=i), {"https": o}))
    verdict, _ = decide(obs, now=T0 + timedelta(minutes=10), sources=TLS_SOURCES)
    latest = rounds[-2:]
    if verdict == "blocked":
        assert len(rounds) >= 2 and all(set(r) == {"down"} for r in latest)
    if verdict == "ok":
        assert len(rounds) >= 2 and all("down" not in r and "up" in r for r in latest)


# --- the Globalping budget ---------------------------------------------------------------------------


def target(n: int, ports: tuple[int, ...] = (443, 8443)) -> Target:
    return Target(uuid.uuid4(), f"n{n}", f"203.0.113.{n}", 8443, None, "schedule", None, "hel1", ports)


def test_tls_budget() -> None:
    nine = [target(i) for i in range(9)]
    assert tls_cost(nine, 5) == 5 * 19
    window = T0 + timedelta(minutes=30)
    assert tls_fit(nine, 5, None, T0) == 9  # unknown allowance: the headers will tell
    assert tls_fit(nine, 5, RateLimit(250, 0, T0 - timedelta(seconds=1)), T0) == 9  # window reset
    assert tls_fit(nine, 5, RateLimit(250, 95, window), T0) == 9
    assert tls_fit(nine, 5, RateLimit(250, 94, window), T0) == 8
    assert tls_fit(nine, 5, RateLimit(250, 14, window), T0) == 0
    assert tls_fit(nine, 5, RateLimit(250, 15, window), T0) == 1
    assert tls_fit(nine, 5, RateLimit(250, 25, window), T0, reserve=10) == 1
    assert tls_fit([target(1, (443,)), *nine], 5, RateLimit(250, 10, window), T0) == 1  # one port: 5
    assert service.probes_used(tls(443, {"a": "up", "b": "down", "c": "nodata"})) == 3


# --- location scores and the planner -------------------------------------------------------------------


def reach(location: str, node: str, key: str, state: str, hours_ago: float = 1) -> ReachRow:
    return ReachRow(
        location,
        uuid.uuid5(uuid.NAMESPACE_DNS, node),
        key,
        key,
        None,
        state,
        T0 - timedelta(hours=hours_ago),  # type: ignore[arg-type]
    )


def test_location_scores() -> None:
    rows = [
        reach("fsn1", "a", "asn:197207", "closed"),
        reach("fsn1", "a", "asn:57218", "closed"),
        reach("hel1", "b", "asn:197207", "open"),
        reach("hel1", "b", "asn:57218", "open"),
        reach("nbg1", "c", "asn:57218", "open"),
        reach("nbg1", "d", "asn:197207", "closed"),  # one burned address ...
        reach("nbg1", "e", "asn:197207", "open"),  # ... does not close the location
        reach("ash", "f", "asn:57218", "open"),
        reach("ash", "f", "asn:197207", "closed"),
    ]
    agg = aggregate(rows)
    assert {loc: r.classify(KEYS) for loc, r in agg.items()} == {
        "fsn1": "bad",
        "hel1": "good",
        "nbg1": "good",
        "ash": "mixed",
    }
    mci = agg["nbg1"].networks["asn:197207"]
    assert (mci.state, mci.nodes_open, mci.nodes_closed) == ("open", 1, 1)
    assert (
        classify([None, None]) == "unknown"
        and classify(["open", None]) == "mixed"
        and classify([]) == "unknown"
    )
    assert location_classes(agg, ["fsn1", "hil"], KEYS) == {"fsn1": "bad", "hil": "unknown"}
    table = reach_json(agg, ["hel1", "fsn1"], KEYS)
    assert [t["location"] for t in table] == ["hel1", "fsn1", "ash", "nbg1"]  # allowed first, then the rest
    assert [t["allowed"] for t in table] == [True, True, False, False]
    assert [n["key"] for n in table[0]["networks"]] == KEYS and table[1]["class"] == "bad"
    assert CLASS_RANK["good"] < CLASS_RANK["mixed"] == CLASS_RANK["unknown"] < CLASS_RANK["bad"]


def plan_with(classes: dict[str, str], **settings_kw: Any) -> list[Any]:
    return creates(plan_for(open_settings(**settings_kw), location_classes=classes))


def test_planner_spreads_over_open_locations_and_skips_closed_ones() -> None:
    cs = plan_with({"fsn1": "bad", "nbg1": "mixed", "hel1": "good"}, target_slots=3)
    assert [c.context["location"] for c in cs] == ["hel1", "hel1", "hel1"] and all(c.allowed for c in cs)
    assert all(c.context["location_class"] == "good" for c in cs)  # half-closed comes after
    # P16, the owner: "other locations too, not only hel1": a location without data is used as well.
    cs = plan_with({"fsn1": "bad", "nbg1": "unknown", "hel1": "good"}, target_slots=3)
    assert [c.context["location"] for c in cs] == ["nbg1", "hel1", "hel1"] and all(c.allowed for c in cs)
    cs = plan_with({"fsn1": "bad"}, target_slots=4)  # evenly over the open ones
    assert [c.context["location"] for c in cs] == ["nbg1", "hel1", "hel1", "nbg1"]
    cs = plan_with({"fsn1": "good", "nbg1": "good", "hel1": "bad"}, target_slots=3)
    assert [c.context["location"] for c in cs] == ["fsn1", "nbg1", "fsn1"]
    # Nothing known yet: the slots spread over the owner's locations exactly as before.
    assert [c.context["location"] for c in plan_with({}, target_slots=3)] == ["fsn1", "nbg1", "hel1"]
    # Never outside the owner's list, even when a location outside it is open.
    cs = plan_with({"fsn1": "mixed", "hel1": "good"}, target_slots=1, locations=["fsn1"])
    assert [c.context["location"] for c in cs] == ["fsn1"] and cs[0].allowed


def test_a_replacement_goes_to_an_open_location() -> None:
    burned = dataclasses.replace(node(1, slot=1, location="nbg1"), verdict="blocked", verdict_at=NOW)
    plan = plan_for(
        open_settings(target_slots=1, node_cap_total=10),
        nodes=(burned,),
        location_classes={"fsn1": "mixed", "hel1": "good", "nbg1": "good"},
    )
    (c,) = creates(plan)
    assert c.allowed and c.context["location"] == "hel1" and c.context["avoid"] == ["nbg1"]
    plan = plan_for(
        open_settings(target_slots=1, node_cap_total=10, locations=["nbg1", "fsn1"]),
        nodes=(burned,),
        location_classes={"fsn1": "bad"},
    )
    (c,) = creates(plan)  # the only other location is closed from Iran: no other location
    assert not c.allowed and "no_other_location" in c.reasons


def test_every_allowed_location_closed_builds_nothing_and_tells_the_owner() -> None:
    classes = {"fsn1": "bad", "nbg1": "bad", "hel1": "bad", "hil": "good"}
    plan = plan_for(open_settings(target_slots=2), location_classes=classes)
    cs = creates(plan)
    assert len(cs) == 2 and all(not c.allowed and "all_locations_closed" in c.reasons for c in cs)
    assert [c.context["location"] for c in cs] == ["fsn1", "nbg1"]  # where it would have gone (shown)
    snap = FleetSnapshot(now=NOW, settings=open_settings(target_slots=2), location_classes=classes)
    (f,) = [f for f in findings(plan, snap) if f.code == "all_locations_closed"]
    assert f.detail == {"slots": [1, 2]} and "همراه اول و رایتل" in f.text and "fsn1، nbg1، hel1" in f.text
    assert "all_locations_closed" in DAILY


def test_planner_never_plans_a_closed_location() -> None:
    locs = ["fsn1", "nbg1", "hel1"]
    for combo in itertools.product(["good", "mixed", "unknown", "bad"], repeat=3):
        classes = dict(zip(locs, combo, strict=True))
        for c in creates(build_plan(FleetSnapshot(**{**_snap_kw(), "location_classes": classes}))):
            if c.allowed:
                assert classes[c.context["location"]] != "bad" and c.context["location"] in locs
            if all(v == "bad" for v in combo):
                assert not c.allowed and "all_locations_closed" in c.reasons


def _snap_kw() -> dict[str, Any]:
    return {
        "now": NOW,
        "settings": open_settings(target_slots=4),
        "accounts": (account(),),
        "offers": offers_for("acc-a"),
    }


def test_node_network_states_per_round() -> None:
    cells = {
        "443": {"asn:197207": "up", "asn:57218": "down", "asn:202468": "invalid"},
        "8443": {"asn:197207": "down", "asn:57218": "down"},
    }
    assert node_network_states(cells) == {"asn:197207": "open", "asn:57218": "closed"}
