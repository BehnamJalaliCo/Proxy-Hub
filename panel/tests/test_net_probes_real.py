"""Real Globalping and check-host measurements from Iran (opt-in: ``PROXYHUB_GLOBALPING_TESTS=1``).

A well-known public host (1.1.1.1, port 443) stands in for a node: the full reconciler runs two
real rounds against it, 75 seconds apart, and stores real results and a real verdict: TLS handshakes
from MCI, Rightel and other Iranian networks (what decides), TCP information. Each run costs about
40 Globalping tests of the free 250 per hour; the candidate-site round below about 20 more. The
probes in Iran come and go (an eyeball probe may be offline), so the assertions accept "unknown"
only together with the reason the logic gave.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import assert_absent_from_database
from test_net_support import clean_net_tables, make_node

from proxyhub.probes import service
from proxyhub.probes.globalping import GlobalpingClient, dns_body
from proxyhub.probes.settings import DEFAULT_ASNS, MCI_ASN, RIGHTEL_ASN, ProbeSettings
from proxyhub.reconcilers.probes import ProbesReconciler
from proxyhub.targets.service import measure_round, net_results, target_locations

pytestmark = [pytest.mark.globalping]

PUBLIC_HOST = "1.1.1.1"


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_net_tables(eng)
    yield eng
    await clean_net_tables(eng)
    await eng.dispose()


@pytest.mark.db
async def test_real_rounds_from_iran_against_a_public_host(engine: AsyncEngine) -> None:
    node_id = await make_node(engine, state="ready", ipv4=PUBLIC_HOST)
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO settings (key, value) VALUES ('probes', CAST(:v AS jsonb))"),
            {"v": json.dumps({"icmp": True, "dns": False, "max_nodes_per_tick": 1})},
        )
    rec = ProbesReconciler()
    for i in range(2):
        if i:
            await asyncio.sleep(75)  # check-host throttles a target checked again within a minute
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE probe_node_state SET last_round_at = now() - interval '1 day'"))
            await rec.run_once(conn)
    async with engine.connect() as conn:
        summary = await service.summary(conn)
        measurements = (
            await conn.execute(
                text("SELECT source, kind, role, status, ref, note FROM probe_measurements ORDER BY id")
            )
        ).all()
        rounds = (
            await conn.execute(
                text("SELECT observations, verdict, verdict_reasons FROM probe_rounds ORDER BY id")
            )
        ).all()
    print("\nREAL PROBE MEASUREMENTS:", [tuple(m) for m in measurements])
    print("REAL ROUNDS:", [(r.observations, r.verdict, r.verdict_reasons) for r in rounds])
    print("REAL SUMMARY:", json.dumps(summary, ensure_ascii=False, indent=1))
    assert len(rounds) == 2
    (target,) = [t for t in summary["targets"] if t["node_id"] == str(node_id)]
    assert target["verdict"] in ("ok", "suspect", "unknown", "blocked")
    if target["verdict"] == "unknown":
        assert target["verdict_reasons"], "unknown must always say why"
    else:
        # The TLS control worked in both rounds, so the probes themselves were proven.
        for r in rounds:
            assert r.observations["https"]["state"] != "invalid"
    tls = {n["asn"] for n in target["networks"] if n["kind"] == "https"}
    assert {MCI_ASN, RIGHTEL_ASN} <= tls, "both key networks are recorded (with or without a probe)"
    assert {n["port"] for n in target["networks"]} == {443}  # no Telemt config: 443 only
    # Rule 7 sanity: the addresses of check-host's own nodes are never stored.
    async with httpx.AsyncClient(timeout=20) as client:
        nodes = (
            await client.get("https://check-host.net/nodes/hosts", headers={"Accept": "application/json"})
        ).json()
    for info in nodes["nodes"].values():
        if info["location"][0] == "ir":
            await assert_absent_from_database(engine, info["ip"])


async def test_real_dns_hijack_signature_from_iran() -> None:
    """youtube.com is filtered in Iran: ISP resolvers answer from 10.10.34.0/24 (docs/10 3.10)."""
    asns = [*DEFAULT_ASNS, 202468]  # plus ArvanCloud, a datacenter network with a probe
    async with GlobalpingClient() as gp:
        m, _ = await gp.run(
            dns_body("youtube.com", asns), kind="dns", role="control", target="youtube.com", port=None,
            requested_asns=asns,
        )  # fmt: skip
    print("\nREAL DNS:", m.status, [(n.network, n.asn, n.state, n.detail) for n in m.networks])
    answered = [n for n in m.networks if n.state != "nodata"]
    if m.status != "ok" or not answered:
        pytest.skip(f"no Iranian probe answered (status {m.status})")
    assert any(n.detail.get("hijacked") for n in answered)


async def test_real_round_of_candidate_sites_from_iran() -> None:
    """A real candidate round (proxyhub/targets): github.com as the control from MCI and Rightel,
    two sites on its probes. P15 measured www.apple.com healthy on MCI and Rightel."""
    probes = ProbeSettings()
    async with GlobalpingClient() as gp:
        rnd = await measure_round(
            gp, ["github.com", "www.apple.com"], probes.tls_control_host, target_locations(probes)
        )
    print("\nREAL TARGET ROUND:", rnd.control.status, {h: (m.status, m.ref) for h, m in rnd.results.items()})
    if rnd.control.status != "ok":
        pytest.skip(f"Globalping gave no usable control (status {rnd.control.status})")
    assert rnd.results["github.com"].ref == rnd.control.ref  # the control's own result is reused
    apple = net_results(rnd.results["www.apple.com"], rnd.control)
    print("REAL www.apple.com:", {k: (r.state, r.ms) for k, r in apple.items()})
    valid = [r for r in apple.values() if r.state != "nodata"]
    if not valid:
        pytest.skip("no Iranian key network had a probe whose control worked")
    assert all(r.network for r in valid)
