"""Probes from Iran: parsers (real Globalping and check-host answers as data), verdict logic over
rounds/sources/control, storage, API, and the reconciler on real PostgreSQL. Real API calls are in
tests/test_net_probes_real.py.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import ADMIN, CSRF
from test_net_support import (
    ARVAN_PROBE,
    CH_LIMIT_EXCEEDED,
    CH_NODES,
    CH_TCP_CREATED,
    CH_TCP_RESULT_FAIL,
    CH_TCP_RESULT_OK,
    CH_TCP_RESULT_PENDING,
    GP_DNS_HIJACKED,
    GP_TCP_CLOSED_PORT,
    GP_TCP_HETZNER,
    GP_TCP_ONE,
    HTTP_OK_GITHUB_RIGHTEL,
    HTTP_OK_PANEL_ARVAN,
    HTTP_TLS_TIMEOUT_RIGHTEL_443,
    MCI_PROBE,
    RIGHTEL_PROBE,
    actions,
    clean_net_tables,
    deep,
    gp_https,
    make_node,
)

from proxyhub.api.deps import require_admin
from proxyhub.cloudflare.service import NodeNames
from proxyhub.probes import service
from proxyhub.probes.checkhost import CheckHostClient, classify_start, parse_nodes, parse_tcp_result
from proxyhub.probes.globalping import (
    GlobalpingClient,
    RateLimit,
    dns_body,
    parse_dns,
    parse_http,
    parse_ping,
    parse_rate_limit,
    tcp_body,
)
from proxyhub.probes.settings import ProbeSettings
from proxyhub.probes.types import Measurement, NetworkObs
from proxyhub.probes.verdict import RoundObs, SourceObservation, decide, observe
from proxyhub.reconcilers.probes import ProbesReconciler

ASNS = [197207, 57218, 44244, 58224]
PROBE_PATHS = {("GET", "/api/probes/summary"), ("POST", "/api/probes/run")}
T0 = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)


# --- parsers over real answers -------------------------------------------------------------------


def test_parse_tcp_ping_real_measurement() -> None:
    nets = {n.network_key: n for n in parse_ping(GP_TCP_HETZNER, ASNS)}
    assert set(nets) == {"asn:197207", "asn:57218", "asn:44244", "asn:58224"}
    mci, rightel = nets["asn:197207"], nets["asn:57218"]
    assert (mci.network, mci.ok, mci.fail, mci.nodata, mci.state) == ("همراه اول", 0, 0, 1, "nodata")
    assert mci.detail["failure"] == "internal"  # a probe-side failure is never a "fail"
    assert (rightel.network, rightel.ok, rightel.fail, rightel.rtt_ms, rightel.state) == (
        "رایتل",
        3,
        0,
        108.8,
        "up",
    )
    for asn in (44244, 58224):  # Irancell and TCI had no probe (recorded, not counted)
        n = nets[f"asn:{asn}"]
        assert not n.had_probe and n.detail == {"no_probe": True} and n.state == "nodata"
    (closed,) = parse_ping(GP_TCP_CLOSED_PORT)
    assert (closed.ok, closed.fail, closed.state, closed.rtt_ms) == (0, 3, "down", None)
    target_fail = deep(GP_TCP_CLOSED_PORT)
    target_fail["results"][0]["result"] = {"status": "failed", "rawOutput": "x", "failureSource": "target"}
    assert parse_ping(target_fail)[0].state == "down"


def test_parse_dns_hijack_signature() -> None:
    nets = {n.network_key: n for n in parse_dns(GP_DNS_HIJACKED, None)}
    assert nets["asn:57218"].state == "down" and nets["asn:57218"].detail["hijacked"] is True
    assert nets["asn:202468"].detail["answers"] == ["10.10.34.36"]
    clean = deep(GP_DNS_HIJACKED)
    clean["results"][0]["result"]["answers"][0]["value"] = "203.0.113.10"
    clean["results"][1]["result"]["answers"] = []
    nets = {n.network_key: n for n in parse_dns(clean, "203.0.113.10")}
    assert nets["asn:57218"].state == "up" and nets["asn:57218"].detail["matches"] is True
    assert nets["asn:202468"].state == "nodata"


def test_check_host_parsers() -> None:
    nodes = parse_nodes(CH_NODES)
    assert [(n.short, n.city, n.asn) for n in nodes] == [
        ("ir1", "Tehran", 47430),
        ("ir2", "Isfahan", 209279),
        ("ir3", "Shiraz", 213953),
    ]
    assert all("ip" not in vars(n) for n in nodes)  # the node's own address is not kept
    ok = {n.network_key: n for n in parse_tcp_result(CH_TCP_RESULT_OK, nodes)}
    assert ok["ch:ir1"].state == "up" and ok["ch:ir1"].rtt_ms == 87.3 and ok["ch:ir3"].state == "up"
    fail = parse_tcp_result(CH_TCP_RESULT_FAIL, nodes[:2])
    assert [n.state for n in fail] == ["down", "down"] and fail[0].detail["error"] == "Connection timed out"
    pending = parse_tcp_result(CH_TCP_RESULT_PENDING, nodes[:2])
    assert [n.state for n in pending] == ["nodata", "down"]
    assert classify_start(200, CH_TCP_CREATED) == ("ok", "555dc965k7ff")
    assert classify_start(200, CH_LIMIT_EXCEEDED) == ("rate_limited", None)
    assert classify_start(429, None) == ("rate_limited", None)
    assert classify_start(503, None) == ("error", None)
    assert classify_start(200, {"ok": 1, "request_id": "../x"}) == ("error", None)


def test_request_bodies_and_rate_limit_headers() -> None:
    body = tcp_body("203.0.113.10", 443, ASNS)
    assert body["measurementOptions"] == {"protocol": "TCP", "port": 443, "packets": 3}
    assert body["locations"][0] == {"asn": 197207, "limit": 1}
    assert (
        tcp_body("203.0.113.10", 443, "2sAWRLjWFTTMzEwt600021REC")["locations"] == "2sAWRLjWFTTMzEwt600021REC"
    )
    assert dns_body("slot1.example.com", ASNS)["measurementOptions"] == {"query": {"type": "A"}}
    # Header names exactly as api.globalping.io sent them on 2026-11-03.
    rate = parse_rate_limit(
        {"x-ratelimit-limit": "250", "x-ratelimit-remaining": "245", "x-ratelimit-reset": "1395"}, T0
    )
    assert rate == RateLimit(250, 245, T0 + timedelta(seconds=1395))
    assert parse_rate_limit({}, T0) is None


# --- verdict logic -------------------------------------------------------------------------------


def m(source: str, networks: list[NetworkObs], *, role: str = "node", status: str = "ok") -> Measurement:
    return Measurement(source, "tcp", role, "x", 443, status, T0, networks=tuple(networks))  # type: ignore[arg-type]


def up(key: str) -> NetworkObs:
    return NetworkObs(key, key, None, ok=3)


def down(key: str) -> NetworkObs:
    return NetworkObs(key, key, None, fail=3)


def nodata(key: str) -> NetworkObs:
    return NetworkObs(key, key, None, nodata=1)


def test_observe_pairs_each_network_with_its_own_control() -> None:
    control = m("globalping", [up("a"), down("b"), nodata("c")], role="control")
    # Only network a counts: b could not reach the control, c gave no data.
    assert observe(m("globalping", [up("a"), down("b"), down("c")]), control).state == "reachable"
    assert observe(m("globalping", [down("a"), up("b")]), control).state == "unreachable"
    both = m("globalping", [up("a"), up("b")], role="control")
    assert observe(m("globalping", [up("a"), down("b")]), both) == SourceObservation("partial", None, 2, 1)
    assert (
        observe(m("globalping", [up("a")]), m("globalping", [down("a")], role="control")).reason
        == "control_failed"
    )
    assert observe(
        m("globalping", [up("a")]), m("globalping", [], role="control", status="rate_limited")
    ).reason == ("control_rate_limited")
    assert observe(m("globalping", [up("a")], status="error"), both).reason == "node_error"
    assert observe(None, both).reason == "source_missing"
    assert observe(m("globalping", [nodata("a")]), both).reason == "no_valid_networks"


def rnd(minutes: int, gp: str, ch: str) -> RoundObs:
    def obs(state: str) -> SourceObservation:
        return SourceObservation(state, "control_failed" if state == "invalid" else None)  # type: ignore[arg-type]

    return RoundObs(T0 + timedelta(minutes=minutes), {"globalping": obs(gp), "check-host": obs(ch)})


NOW = T0 + timedelta(minutes=30)


@pytest.mark.parametrize(
    ("rounds", "expected"),
    [
        ([rnd(20, "reachable", "reachable")], "unknown"),  # one round is never enough
        ([rnd(10, "reachable", "reachable"), rnd(20, "reachable", "reachable")], "ok"),
        ([rnd(10, "unreachable", "unreachable"), rnd(20, "unreachable", "unreachable")], "blocked"),
        ([rnd(10, "unreachable", "reachable"), rnd(20, "unreachable", "reachable")], "suspect"),
        ([rnd(10, "reachable", "reachable"), rnd(20, "unreachable", "unreachable")], "suspect"),
        ([rnd(10, "partial", "partial"), rnd(20, "partial", "partial")], "suspect"),
        (
            [rnd(10, "unreachable", "unreachable"), rnd(20, "unreachable", "invalid")],
            "unknown",
        ),  # control failed
        ([rnd(10, "reachable", "invalid"), rnd(20, "reachable", "reachable")], "unknown"),
        ([rnd(-60, "unreachable", "unreachable"), rnd(20, "unreachable", "unreachable")], "unknown"),  # stale
        (
            [
                rnd(0, "unreachable", "unreachable"),
                rnd(10, "reachable", "reachable"),
                rnd(20, "reachable", "reachable"),
            ],
            "ok",  # only the latest rounds count
        ),
    ],
)
def test_decide(rounds: list[RoundObs], expected: str) -> None:
    verdict, reasons = decide(rounds, now=NOW, min_rounds=2, max_age=timedelta(hours=1))
    assert verdict == expected, reasons
    assert reasons


def test_decide_never_accepts_fewer_than_two_rounds() -> None:
    assert decide([rnd(20, "unreachable", "unreachable")], now=NOW, min_rounds=1)[0] == "unknown"
    missing_source = RoundObs(T0 + timedelta(minutes=20), {"globalping": SourceObservation("unreachable")})
    verdict, reasons = decide([missing_source, missing_source], now=NOW)
    assert verdict == "unknown" and "round0:check-host:source_missing" in reasons


_states = st.sampled_from(["reachable", "unreachable", "partial", "invalid"])


@settings(max_examples=400)
@given(st.lists(st.tuples(st.integers(0, 29), _states, _states), max_size=6), st.integers(1, 4))
def test_property_ok_or_blocked_needs_both_sources_in_two_valid_rounds(
    spec: list[tuple[int, str, str]], min_rounds: int
) -> None:
    rounds = [rnd(t, gp, ch) for t, gp, ch in spec]
    verdict, _ = decide(rounds, now=NOW, min_rounds=min_rounds)
    if verdict in ("ok", "blocked"):
        latest = sorted(rounds, key=lambda r: r.at, reverse=True)[: max(min_rounds, 2)]
        assert len(latest) >= 2
        wanted = "reachable" if verdict == "ok" else "unreachable"
        assert all(o.state == wanted for r in latest for o in r.observations.values())
        assert all(set(r.observations) == {"globalping", "check-host"} for r in latest)


def test_globalping_budget() -> None:
    s = ProbeSettings()
    one = [service.Target(uuid.uuid4(), "n", "203.0.113.1", 443, "slot1.example.com", "schedule")]
    assert service.globalping_plan(s, one, None, T0) == (True, True, True)
    plenty = RateLimit(250, 200, T0 + timedelta(minutes=30))
    assert service.globalping_plan(s, one, plenty, T0) == (True, True, True)
    # core = 4 x (control + node) = 8; +4 ICMP; +4 DNS
    assert service.globalping_plan(s, one, RateLimit(250, 13, T0 + timedelta(minutes=5)), T0) == (
        True,
        True,
        False,
    )
    assert service.globalping_plan(s, one, RateLimit(250, 9, T0 + timedelta(minutes=5)), T0) == (
        True,
        False,
        False,
    )
    assert service.globalping_plan(s, one, RateLimit(250, 7, T0 + timedelta(minutes=5)), T0) == (
        False,
        False,
        False,
    )
    # An expired window means the limit has been reset.
    assert service.globalping_plan(s, one, RateLimit(250, 0, T0 - timedelta(seconds=1)), T0) == (
        True,
        True,
        True,
    )


# --- database --------------------------------------------------------------------------------------


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_net_tables(eng)
    yield eng
    await clean_net_tables(eng)
    await eng.dispose()


KEY_ASNS = [197207, 57218]


def tls_up(ip: str) -> dict[str, Any]:
    """A real finished HTTPS test (200 after a TLS 1.3 handshake), as if to ``ip``."""
    return {**HTTP_OK_PANEL_ARVAN, "resolvedAddress": ip}


def tls_timeout(ip: str) -> dict[str, Any]:
    """A real failed HTTPS test: TCP "connected" (MCI/Rightel's own answer), the handshake timed out."""
    return {**HTTP_TLS_TIMEOUT_RIGHTEL_443, "resolvedAddress": ip}


def https_m(
    raw: dict[str, Any],
    *,
    role: str,
    target: str,
    port: int,
    at: datetime,
    ip: str | None,
    node_id: Any = None,
) -> Measurement:
    return Measurement(
        "globalping", "https", role, target, port, "ok", at, ref=raw["id"],  # type: ignore[arg-type]
        networks=tuple(parse_http(raw, KEY_ASNS, ip)), node_id=node_id,
    )  # fmt: skip


def tls_control_raw(*, mci: bool = True, rightel: bool = True) -> dict[str, Any]:
    """github.com from MCI and Rightel: real result objects (Rightel's own 200, or the TLS timeout)."""
    ok, bad = HTTP_OK_GITHUB_RIGHTEL, {**HTTP_TLS_TIMEOUT_RIGHTEL_443, "resolvedAddress": "140.82.121.4"}
    return gp_https(
        "2ezQw50QA0u8rglRt00021Rtv",
        "github.com",
        443,
        [(MCI_PROBE, ok if mci else bad), (RIGHTEL_PROBE, ok if rightel else bad)],
        at="2026-11-05T16:15:37.146Z",
    )


def node_raw(ip: str, port: int, *, mci: bool, rightel: bool) -> dict[str, Any]:
    return gp_https(
        "2hlXQWvTwWkidFxHE00021Rtv",
        "slot1.example.com",
        port,
        [
            (MCI_PROBE, tls_up(ip) if mci else tls_timeout(ip)),
            (RIGHTEL_PROBE, tls_up(ip) if rightel else tls_timeout(ip)),
            (ARVAN_PROBE, tls_up(ip)),  # a datacenter network: information only
        ],
        at="2026-11-05T16:16:08.167Z",
    )


def real_cycle(
    node_id: uuid.UUID,
    ip: str,
    *,
    at: datetime,
    node_up: bool,
    tcp_up: bool | None = None,
    control: dict[str, Any] | None = None,
    ports: tuple[int, ...] = (443, 8443),
) -> service.CycleResult:
    """A cycle assembled from the real answers above. TLS (decides): the control is github.com from
    MCI and Rightel; the node's 443 and 8443 either answer 200 or time out in the TLS handshake on
    both key networks. TCP (information): the control is www.hetzner.com:443; the node connects like
    1.1.1.1:443 or fails like 1.1.1.1:9 (``tcp_up``, default: like TLS); check-host likewise."""
    tcp = node_up if tcp_up is None else tcp_up
    ch_nodes = parse_nodes(CH_NODES)[:2]
    tls_ctrl = https_m(
        control or tls_control_raw(), role="control", target="github.com", port=443, at=at, ip=None
    )
    gp_ctrl = Measurement(
        "globalping", "tcp", "control", "www.hetzner.com", 443, "ok", at,
        ref=GP_TCP_HETZNER["id"], networks=tuple(parse_ping(GP_TCP_HETZNER, ASNS)),
    )  # fmt: skip
    ch_ctrl = Measurement(
        "check-host", "tcp", "control", "www.hetzner.com", 443, "ok", at,
        ref="555dc965k7ff", networks=tuple(parse_tcp_result(CH_TCP_RESULT_OK, ch_nodes)),
    )  # fmt: skip
    gp_raw = GP_TCP_ONE if tcp else GP_TCP_CLOSED_PORT
    ch_raw = CH_TCP_RESULT_OK if tcp else CH_TCP_RESULT_FAIL
    nodes = [
        https_m(
            node_raw(ip, p, mci=node_up, rightel=node_up), role="node", target="slot1.example.com", port=p,
            at=at, ip=ip, node_id=node_id,
        )
        for p in ports
    ]  # fmt: skip
    nodes += [
        Measurement(
            "globalping", "tcp", "node", ip, 8443, "ok", at, ref=gp_raw["id"],
            networks=tuple(parse_ping(gp_raw, ASNS)), node_id=node_id,
        ),
        Measurement(
            "check-host", "tcp", "node", ip, 8443, "ok", at, ref="555dcbc8k213",
            networks=tuple(parse_tcp_result(ch_raw, ch_nodes)), node_id=node_id,
        ),
    ]  # fmt: skip
    return service.CycleResult(
        started_at=at,
        finished_at=at + timedelta(seconds=40),
        controls={"globalping": gp_ctrl, "check-host": ch_ctrl},
        nodes=nodes,
        gp_rate=RateLimit(250, 240, at + timedelta(minutes=50)),
        tls_control=tls_ctrl,
        tls_probes=2,
    )


def target_of(node_id: uuid.UUID, ip: str) -> service.Target:
    return service.Target(
        node_id, "net-test", ip, 8443, None, "schedule", "slot1.example.com", "fsn1", (443, 8443)
    )


async def store(engine: AsyncEngine, result: service.CycleResult, target: service.Target) -> dict[Any, Any]:
    async with engine.begin() as conn:
        return await service.store_cycle(conn, result, [target], ProbeSettings())


@pytest.mark.db
async def test_verdicts_over_rounds_are_stored_and_logged_on_change(engine: AsyncEngine) -> None:
    node_id = await make_node(engine, state="ready", ipv4="203.0.113.10")
    target = target_of(node_id, "203.0.113.10")
    now = datetime.now(UTC)
    v1 = await store(
        engine, real_cycle(node_id, target.ip, at=now - timedelta(minutes=20), node_up=False), target
    )
    assert v1[node_id][0] == "unknown" and v1[node_id][1] == ["not_enough_rounds:1/2"]
    v2 = await store(
        engine, real_cycle(node_id, target.ip, at=now - timedelta(minutes=10), node_up=False), target
    )
    assert v2[node_id] == ("blocked", ["all_unreachable"])
    v3 = await store(engine, real_cycle(node_id, target.ip, at=now, node_up=True), target)
    assert v3[node_id][0] == "suspect"  # recovered once: not yet ok
    v4 = await store(
        engine, real_cycle(node_id, target.ip, at=now + timedelta(seconds=30), node_up=True), target
    )
    assert v4[node_id] == ("ok", ["all_reachable"])
    log = [
        (a["action"], a["reasons"]["context"]["from"], a["reasons"]["context"]["to"])
        for a in await actions(engine)
    ]
    assert log == [
        ("probe_verdict", "unknown", "blocked"),
        ("probe_verdict", "blocked", "suspect"),
        ("probe_verdict", "suspect", "ok"),
    ]
    first = (await actions(engine))[0]["reasons"]["context"]
    assert first["tls_target"] == "slot1.example.com" and first["tls_ports"] == [443, 8443]
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT count(*) FROM probe_rounds WHERE node_id = :n"), {"n": node_id})
        ).scalar_one()
        nets = (await conn.execute(text("SELECT count(*) FROM probe_network_results"))).scalar_one()
        obs = (
            await conn.execute(
                text("SELECT observations FROM probe_rounds WHERE node_id = :n ORDER BY started_at LIMIT 1"),
                {"n": node_id},
            )
        ).scalar_one()
        reach = (
            await conn.execute(
                text("SELECT network_key, location, state FROM probe_location_reach WHERE node_id = :n"),
                {"n": node_id},
            )
        ).all()
        summary = await service.summary(conn)
    assert rows == 4 and nets > 0
    assert obs["https"]["state"] == "unreachable" and obs["https"]["cells"]["8443"]["asn:197207"] == "down"
    # AbrArvan had no control result (the control ran on MCI and Rightel only): recorded, invalid.
    assert obs["https"]["cells"]["443"]["asn:202468"] == "invalid"
    assert obs["globalping"]["state"] == "unreachable"  # the TCP information is still there
    assert sorted(tuple(r) for r in reach) == [("asn:197207", "fsn1", "open"), ("asn:57218", "fsn1", "open")]
    (t,) = [t for t in summary["targets"] if t["node_id"] == str(node_id)]
    assert t["verdict"] == "ok" and t["ipv4"] == "203.0.113.10" and t["location"] == "fsn1"
    assert t["tls_target"] == "slot1.example.com"
    by_net = {(n["port"], n["network"]): n for n in t["networks"]}
    assert by_net[(8443, "رایتل")]["ok"] == 1 and by_net[(443, "همراه اول")]["ok"] == 1
    assert all(n["kind"] == "https" and n["source"] == "globalping" for n in t["networks"])
    tcp = {(n["source"], n["network"]): n for n in t["tcp_networks"]}
    assert tcp[("globalping", "ایرانسل")]["probe"] is False  # no probe there: recorded as such
    assert tcp[("check-host", "check-host ir1 (Tehran)")]["ok"] == 1
    sources = {s["name"]: s for s in summary["sources"]}
    assert sources["globalping"]["ok"] and "TLS" in sources["globalping"]["note"]
    assert sources["check-host"]["ok"]
    assert all(s["checked"] is True for s in sources.values())
    assert summary["last_run_at"].endswith("Z")
    async with engine.connect() as conn:
        assert await service.last_tls_probes(conn) == 2


@pytest.mark.db
async def test_tcp_answers_never_make_a_blocked_node_reachable(engine: AsyncEngine) -> None:
    """P15: MCI and Rightel answer a TCP SYN themselves for blocked addresses. Every TCP probe
    connects, every TLS handshake times out: the node is blocked."""
    node_id = await make_node(engine, state="ready", ipv4="203.0.113.12")
    target = target_of(node_id, "203.0.113.12")
    now = datetime.now(UTC)
    for minutes in (20, 10):
        cycle = real_cycle(
            node_id, target.ip, at=now - timedelta(minutes=minutes), node_up=False, tcp_up=True
        )
        verdicts = await store(engine, cycle, target)
    assert verdicts[node_id] == ("blocked", ["all_unreachable"])
    async with engine.connect() as conn:
        obs = (
            await conn.execute(
                text(
                    "SELECT observations FROM probe_rounds WHERE node_id = :n "
                    "ORDER BY started_at DESC LIMIT 1"
                ),
                {"n": node_id},
            )
        ).scalar_one()
    assert obs["globalping"]["state"] == "reachable" and obs["check-host"]["state"] == "reachable"
    assert obs["https"]["state"] == "unreachable"


@pytest.mark.db
async def test_a_failed_control_gives_unknown_never_blocked(engine: AsyncEngine) -> None:
    node_id = await make_node(engine, state="ready", ipv4="203.0.113.11")
    target = target_of(node_id, "203.0.113.11")
    now = datetime.now(UTC)
    for minutes in (20, 10):
        # The TLS control could not be reached from any network either: the probes prove nothing.
        cycle = real_cycle(
            node_id, target.ip, at=now - timedelta(minutes=minutes), node_up=False,
            control=tls_control_raw(mci=False, rightel=False),
        )  # fmt: skip
        verdicts = await store(engine, cycle, target)
    assert verdicts[node_id][0] == "unknown"
    assert "round0:https:control_failed" in verdicts[node_id][1]
    async with engine.connect() as conn:
        sources = {s["name"]: s for s in (await service.summary(conn))["sources"]}
        reach = (await conn.execute(text("SELECT count(*) FROM probe_location_reach"))).scalar_one()
    assert sources["globalping"]["ok"] is False and "کنترل" in sources["globalping"]["note"]
    assert reach == 0  # nothing valid: no location record either


@pytest.mark.db
async def test_one_key_network_open_is_never_blocked(engine: AsyncEngine) -> None:
    """Closed from MCI but open from Rightel (or a key network without a valid probe): never blocked."""
    node_id = await make_node(engine, state="ready", ipv4="203.0.113.13")
    target = target_of(node_id, "203.0.113.13")
    now = datetime.now(UTC)
    for minutes in (20, 10):
        cycle = real_cycle(node_id, target.ip, at=now - timedelta(minutes=minutes), node_up=False)
        for i, m in enumerate(cycle.nodes):
            if m.kind == "https":
                raw = gp_https(
                    "2hlXQWvTwWkidFxHE00021Rtv", "slot1.example.com", m.port or 443,
                    [(MCI_PROBE, tls_timeout(target.ip)), (RIGHTEL_PROBE, tls_up(target.ip))],
                    at="2026-11-05T16:16:08.167Z",
                )  # fmt: skip
                cycle.nodes[i] = https_m(
                    raw,
                    role="node",
                    target=m.target,
                    port=m.port or 443,
                    at=m.at,
                    ip=target.ip,
                    node_id=node_id,
                )
        verdicts = await store(engine, cycle, target)
    assert verdicts[node_id][0] == "suspect"
    # MCI had no probe at all (Rightel down): "incomplete", so unknown, not blocked.
    node2 = await make_node(engine, state="ready", ipv4="203.0.113.14")
    t2 = target_of(node2, "203.0.113.14")
    for minutes in (20, 10):
        cycle = real_cycle(node2, t2.ip, at=now - timedelta(minutes=minutes), node_up=False)
        for i, m in enumerate(cycle.nodes):
            if m.kind == "https":
                raw = gp_https(
                    "2BxvEpnwulLDMY5XJ00021Rtw", "slot1.example.com", m.port or 443,
                    [(RIGHTEL_PROBE, tls_timeout(t2.ip))], at="2026-11-05T16:16:39.086Z",
                )  # fmt: skip
                cycle.nodes[i] = https_m(
                    raw, role="node", target=m.target, port=m.port or 443, at=m.at, ip=t2.ip, node_id=node2
                )
        verdicts = await store(engine, cycle, t2)
    assert verdicts[node2][0] == "unknown" and "round0:https:incomplete" in verdicts[node2][1]


@pytest.mark.db
async def test_due_targets(engine: AsyncEngine) -> None:
    ready = await make_node(engine, state="ready", ipv4="203.0.113.20", slot=2)
    await make_node(engine, state="booting", ipv4="203.0.113.21")
    await make_node(engine, state="ready", ipv4=None)
    asked = await make_node(engine, state="unhealthy", ipv4="203.0.113.22")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO telemt_node_configs (node_id, ports, tls_domain, mode_classic, mode_secure, "
                "mode_tls) VALUES (:n, '{8443,443}', 'a.example', false, false, true)"
            ),
            {"n": ready},
        )
        await conn.execute(
            text(
                "INSERT INTO probe_node_state (node_id, run_requested_at, verdict, verdict_reasons) "
                "VALUES (:n, now(), 'unknown', '[]')"
            ),
            {"n": asked},
        )
    s = ProbeSettings()
    now = datetime.now(UTC)
    names = NodeNames(zone="example.com", records={"slot2.example.com": ("203.0.113.20", ready)})
    async with engine.connect() as conn:
        targets = await service.due_targets(conn, s, now, names)
    by_id = {t.node_id: t for t in targets}
    assert set(by_id) == {ready, asked}
    assert by_id[asked].trigger == "manual" and targets[0].node_id == asked  # owner requests first
    r = by_id[ready]
    assert (r.port, r.dns_name, r.trigger, r.tls_name, r.tls_host, r.tls_ports, r.location) == (
        8443,
        "slot2.example.com",
        "schedule",
        "slot2.example.com",  # its own name: the SNI its links use
        "slot2.example.com",
        (443,),  # the MTProto port is not measured by TLS (P16: Telemt answers only its own domain)
        "fsn1",
    )
    a = by_id[asked]
    assert (a.tls_name, a.tls_host, a.tls_ports) == (None, "203.0.113.22", (443,))  # no name, no Telemt
    moved = NodeNames(zone="example.com", records={"slot2.example.com": ("203.0.113.99", ready)})
    async with engine.connect() as conn:
        (r2,) = [t for t in await service.due_targets(conn, s, now, moved) if t.node_id == ready]
    assert r2.tls_name is None and r2.tls_host == "203.0.113.20"  # the name points elsewhere: the IP
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO probe_node_state (node_id, last_round_at, verdict, verdict_reasons) "
                "VALUES (:n, now(), 'ok', '[]')"
            ),
            {"n": ready},
        )
    async with engine.connect() as conn:
        assert {t.node_id for t in await service.due_targets(conn, s, now, None)} == {asked}
        later = now + timedelta(seconds=s.interval_s + 1)
        assert {t.node_id for t in await service.due_targets(conn, s, later, None)} == {ready, asked}
    # A node that became READY a minute ago waits for its configs (P16); the owner's request does not.
    fresh = await make_node(engine, state="ready", ipv4="203.0.113.23")
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE nodes SET ready_at = :t WHERE id = :n"), {"t": now, "n": fresh})
    async with engine.connect() as conn:
        assert fresh not in {
            t.node_id for t in await service.due_targets(conn, s, now + timedelta(minutes=1), None)
        }
        grace = now + service.READY_GRACE
        assert fresh in {t.node_id for t in await service.due_targets(conn, s, grace, None)}


@pytest.mark.db
async def test_reconciler_does_nothing_without_ready_nodes(engine: AsyncEngine) -> None:
    await make_node(engine, state="booting", ipv4="203.0.113.30")
    rec = ProbesReconciler(
        gp_factory=lambda: GlobalpingClient(base_url="http://127.0.0.1:9"),
        ch_factory=lambda: CheckHostClient(base_url="http://127.0.0.1:9"),
    )
    async with engine.begin() as conn:
        await rec.run_once(conn)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM probe_cycles"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT count(*) FROM probe_sources"))).scalar_one() == 0


@pytest.mark.db
async def test_reconciler_with_unreachable_sources_records_unknown(engine: AsyncEngine) -> None:
    """Both services unreachable (a closed local port): the round is stored, every measurement says
    'error', and the verdict stays unknown. Nothing is concluded from a failure of the probes."""
    node_id = await make_node(engine, state="ready", ipv4="203.0.113.31")
    rec = ProbesReconciler(
        gp_factory=lambda: GlobalpingClient(base_url="http://127.0.0.1:9"),
        ch_factory=lambda: CheckHostClient(base_url="http://127.0.0.1:9", poll_seconds=0.01),
    )
    for _ in range(2):
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE probe_node_state SET last_round_at = now() - interval '1 day'"))
            await rec.run_once(conn)
    async with engine.connect() as conn:
        statuses = (
            (await conn.execute(text("SELECT DISTINCT status FROM probe_measurements"))).scalars().all()
        )
        state = (
            await conn.execute(
                text("SELECT verdict, verdict_reasons FROM probe_node_state WHERE node_id = :n"),
                {"n": node_id},
            )
        ).one()
        summary = await service.summary(conn)
    assert set(statuses) == {"error"}
    assert state.verdict == "unknown" and any("https:control_error" in r for r in state.verdict_reasons)
    assert all(s["ok"] is False for s in summary["sources"])
    assert await actions(engine) == []  # unknown -> unknown is not a change


def test_worker_registers_the_net_loops() -> None:
    from proxyhub.worker import reconcilers

    loops = {r.name: r for r in reconcilers()}
    assert {"dns", "probes", "targets"} <= set(loops)
    assert loops["probes"].interval_seconds == 60 and loops["dns"].interval_seconds == 60
    assert loops["targets"].interval_seconds == 60


# --- API ------------------------------------------------------------------------------------------


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


@pytest.mark.db
@pytest.mark.redis
async def test_every_probe_route_requires_admin(engine: AsyncEngine) -> None:
    from proxyhub.api import routes_probes
    from proxyhub.api.app import app, lifespan

    found = set()
    for route in routes_probes.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods:
            found.add((method, route.path))
            assert require_admin in {d.call for d in route.dependant.dependencies}
    assert found == PROBE_PATHS
    app.dependency_overrides.pop(require_admin, None)
    async with lifespan(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers=CSRF) as c:
            for method, path in sorted(PROBE_PATHS):
                r = await c.request(method, path, json={"node_id": str(uuid.uuid4())})
                assert r.status_code == 401 and r.json()["error"] == "unauthenticated"
            r = await c.post("/api/probes/run", json={"node_id": str(uuid.uuid4())})
            r2 = await c.post("/api/probes/run", json={}, headers={"X-Requested-With": ""})
            assert r.status_code == 401 and r2.status_code == 403 and r2.json()["error"] == "csrf"


@pytest.mark.db
@pytest.mark.redis
async def test_summary_and_run_over_http(client: AsyncClient, engine: AsyncEngine) -> None:
    r = await client.get("/api/probes/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["last_run_at"] is None and [s["name"] for s in body["sources"]] == [
        "globalping",
        "check-host",
    ]
    assert all(s["ok"] is False and s["note"] and s["checked"] is False for s in body["sources"])
    node = await make_node(engine, state="ready", ipv4="203.0.113.40")
    no_ip = await make_node(engine, state="booting", ipv4=None)
    gone = await make_node(engine, state="deleted", ipv4="203.0.113.41")
    targets = (await client.get("/api/probes/summary")).json()["targets"]
    (t,) = [t for t in targets if t["node_id"] == str(node)]
    assert t == {
        "node_id": str(node),
        "name": t["name"],
        "ipv4": "203.0.113.40",
        "location": "fsn1",
        "verdict": "unknown",
        "verdict_at": None,
        "verdict_reasons": [],
        "networks": [],
        "tls_target": None,
        "tcp_networks": [],
    }
    locations = (await client.get("/api/probes/summary")).json()["locations"]
    assert locations["window_days"] == 7 and locations["key_networks"] == ["asn:197207", "asn:57218"]
    assert [x["location"] for x in locations["locations"]] == ["fsn1", "nbg1", "hel1"]  # the defaults
    assert all(
        x["class"] == "unknown" and x["allowed"] and x["networks"] == [] for x in locations["locations"]
    )
    assert str(gone) not in {x["node_id"] for x in targets}
    r = await client.post("/api/probes/run", json={"node_id": str(node)})
    assert r.status_code == 202 and r.json() == {}
    for missing in (uuid.uuid4(), gone):
        r = await client.post("/api/probes/run", json={"node_id": str(missing)})
        assert r.status_code == 404 and r.json()["error"] == "not_found" and r.json()["message_fa"]
    r = await client.post("/api/probes/run", json={"node_id": str(no_ip)})
    assert r.status_code == 409 and r.json()["error"] == "node_has_no_ip"
    r = await client.post("/api/probes/run", json={"node_id": "not-a-uuid"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    async with engine.connect() as conn:
        targets = await service.due_targets(conn, ProbeSettings(), datetime.now(UTC), None)
    assert [(x.node_id, x.trigger) for x in targets] == [(node, "manual")]
    assert [a["action"] for a in await actions(engine)] == ["probe_run_requested"]
